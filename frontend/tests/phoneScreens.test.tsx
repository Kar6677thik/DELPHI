import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ProbeRequest } from '../src/api/phone';
import { DecisionScreen } from '../src/phone/DecisionScreen';
import { PhoneFrame } from '../src/phone/PhoneFrame';
import { ProbeScreen } from '../src/phone/ProbeScreens';

/** #99 acceptance: a component test for each probe `ui` type (docs/08 §4). */

function probe(overrides: Partial<ProbeRequest> = {}): ProbeRequest {
  return {
    session_id: 's-1',
    probe_run_id: 'r-1',
    probe_key: 'SPELL_BREAKER_DIGITAL_ARREST',
    ui: 'QUESTION',
    language: 'en',
    text: 'Is anyone on a call with you asking you to make this payment?',
    options: [
      { key: 'YES', text: 'Yes' },
      { key: 'NO', text: 'No' },
    ],
    timeout_s: 60,
    ...overrides,
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('QUESTION', () => {
  it('shows the server’s text and options and answers with the option key', async () => {
    const onAnswer = vi.fn();
    render(<ProbeScreen probe={probe()} onAnswer={onAnswer} />);
    expect(screen.getByRole('heading', { name: /Is anyone on a call/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'No' }));
    const [answer, signals] = onAnswer.mock.calls[0] as [unknown, Record<string, number>];
    expect(answer).toEqual({ option_key: 'NO' });
    expect(signals.answered_at_ms).toBeGreaterThanOrEqual(signals.shown_at_ms ?? 0);
    expect(signals).toMatchObject({ visibility_hidden_count: 0, paste_count: 0 });
  });

  it('counts leaving the app while the question is up', async () => {
    const onAnswer = vi.fn();
    render(<ProbeScreen probe={probe()} onAnswer={onAnswer} />);
    const state = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    document.dispatchEvent(new Event('visibilitychange'));
    state.mockReturnValue('visible');
    await userEvent.click(screen.getByRole('button', { name: 'Yes' }));
    expect(onAnswer.mock.calls[0]?.[1]).toMatchObject({ visibility_hidden_count: 1 });
  });
});

describe('OTP', () => {
  const otp = () => probe({ ui: 'OTP', probe_key: 'OTP_STEP_UP', text: null, options: [] });

  it('takes six digits, counts a paste, and says how many tries are left', async () => {
    const onAnswer = vi.fn();
    render(<ProbeScreen probe={otp()} onAnswer={onAnswer} attemptsRemaining={2} />);
    const input = screen.getByLabelText('One-time code');
    const confirm = screen.getByRole('button', { name: 'Confirm' });
    expect(confirm).toBeDisabled();
    fireEvent.paste(input);
    await userEvent.type(input, '12a34567');
    expect(input).toHaveValue('123456');
    expect(screen.getByRole('alert')).toHaveTextContent('That code is wrong. 2 tries left.');
    await userEvent.click(confirm);
    expect(onAnswer.mock.calls[0]?.[0]).toEqual({ otp: '123456' });
    expect(onAnswer.mock.calls[0]?.[1]).toMatchObject({ paste_count: 1 });
  });
});

describe('BIOMETRIC', () => {
  it('answers MATCH from the sensor and CANCEL from cancel', async () => {
    const onAnswer = vi.fn();
    render(<ProbeScreen probe={probe({ ui: 'BIOMETRIC', options: [] })} onAnswer={onAnswer} />);
    await userEvent.click(screen.getByRole('button', { name: 'Touch the sensor' }));
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onAnswer.mock.calls.map((c) => c[0] as unknown)).toEqual([
      { biometric: 'MATCH' },
      { biometric: 'CANCEL' },
    ]);
  });
});

describe('COOLING_OFF', () => {
  const pause = () => probe({ ui: 'COOLING_OFF', options: [], text: null, timeout_s: 30 });

  it('counts each press and sends them with a cancel', async () => {
    const onAnswer = vi.fn();
    render(<ProbeScreen probe={pause()} onAnswer={onAnswer} />);
    await userEvent.click(screen.getByRole('button', { name: 'Send now' }));
    await userEvent.click(screen.getByRole('button', { name: 'Send now' }));
    await userEvent.click(screen.getByRole('button', { name: 'Pay another way' }));
    await userEvent.click(screen.getByRole('button', { name: 'Increase my limit' }));
    expect(screen.getByText('Limits can’t change during the pause.')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel payment' }));
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer.mock.calls[0]?.[0]).toEqual({
      cooling_off: {
        cancelled: true,
        other_channel_attempts: 1,
        limit_increase_requests: 1,
        send_now_presses: 2,
      },
    });
  });

  it('lets the payment go when the pause runs out, once', () => {
    vi.useFakeTimers();
    const onAnswer = vi.fn();
    render(<ProbeScreen probe={pause()} onAnswer={onAnswer} />);
    expect(screen.getByText('30')).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(31_000);
    });
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer.mock.calls[0]?.[0]).toMatchObject({ cooling_off: { cancelled: false } });
  });
});

describe('decision screens (docs/08 §7)', () => {
  it.each([
    ['RELEASE', 'Payment sent', null],
    ['HOLD_FOR_REVIEW', 'We’re checking this payment', 'Most checks finish within 4 hours.'],
    ['PROTECTIVE_HOLD', 'Hang up.', 'No police, CBI, court or RBI officer will ever ask you'],
    ['DECLINE', 'This payment could not be completed', 'Please call 1800-XXX.'],
    [null, 'Payment cancelled', 'No money has left your account.'],
  ])('%s shows its own screen', async (decision, title, body) => {
    const onDone = vi.fn();
    render(<DecisionScreen decision={decision} onDone={onDone} />);
    expect(screen.getByRole('heading', { name: title })).toBeInTheDocument();
    if (body) expect(screen.getByText(new RegExp(body))).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(onDone).toHaveBeenCalled();
  });
});

describe('PhoneFrame', () => {
  it('drops the mock SMS with the code in from the top', () => {
    render(
      <PhoneFrame clock="18:10" sms="482913">
        <p>screen</p>
      </PhoneFrame>,
    );
    const phone = screen.getByRole('region', { name: 'Customer’s phone' });
    expect(phone).toHaveTextContent('18:10');
    expect(screen.getByRole('status')).toHaveTextContent('482913 is your one-time code');
  });
});
