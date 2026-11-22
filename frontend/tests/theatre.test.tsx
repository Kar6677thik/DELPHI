import { act, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { caption, holdMs, leading, S1 } from '../src/theatre/replay';
import { setMockSession } from './mocks/handlers';
import { renderApp } from './renderApp';

/** The Theatre (`/theatre`): the S-1 story told from the engine's recorded run. jsdom has no
 * WebGL, so the Oracle's canvas is replaced by a stand-in that reports what it was given. */
vi.mock('../src/theatre/Oracle', () => ({
  Oracle: ({
    posterior,
    decision,
  }: {
    posterior: Record<string, number>;
    decision: string | null;
  }) => (
    <div data-testid="oracle">
      {leading(posterior)[0]} {decision ?? 'deciding'}
    </div>
  ),
}));

afterEach(() => {
  vi.useRealTimers();
});

describe('the S-1 replay', () => {
  it('tells the ENG-S1 story: genuine, the call, the coached answer, the spell breaks', () => {
    const kinds = S1.steps.map((s) => s.kind);
    expect(kinds[0]).toBe('prior');
    expect(kinds.at(-1)).toBe('decision');
    expect(S1.steps.at(-1)?.decision).toBe('PROTECTIVE_HOLD');
    const keys = S1.steps.map((s) => s.probe_key);
    expect(keys).toContain('CALL_STATUS');
    expect(leading(S1.steps.at(-1)?.posterior ?? {})[0]).toBe('APP_DIGITAL_ARREST');
    for (const step of S1.steps) {
      expect(caption(step)).not.toBe('');
      expect(holdMs(step)).toBeGreaterThan(1000);
    }
  });
});

describe('/theatre', () => {
  it('opens at a step, shows the question on the phone and what DELPHI weighs', async () => {
    setMockSession('viewer');
    const at = S1.steps.findIndex((s) => s.probe_key === 'SPELL_BREAKER_DIGITAL_ARREST');
    renderApp(`/theatre?step=${String(at)}&paused`);
    const phone = await screen.findByRole('region', { name: 'Customer’s phone' });
    expect(within(phone).getByText(/claiming to be from the police, CBI/)).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: 'Which question is worth asking?' }),
    ).toBeInTheDocument();
    expect(screen.getByText(/One question breaks the spell/)).toBeInTheDocument();
    expect(await screen.findByTestId('oracle')).toHaveTextContent('APP_DIGITAL_ARREST deciding');
  });

  it('steps with the arrow keys and ends on the protective hold', async () => {
    setMockSession('viewer');
    const { user } = renderApp(`/theatre?step=${String(S1.steps.length - 2)}&paused`);
    await screen.findByRole('region', { name: 'Customer’s phone' });
    await user.keyboard('{ArrowRight}');
    const phone = screen.getByRole('region', { name: 'Customer’s phone' });
    expect(within(phone).getByRole('heading', { name: 'Hang up.' })).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: 'Expected loss of each decision' }),
    ).toBeInTheDocument();
    expect(await screen.findByTestId('oracle')).toHaveTextContent('PROTECTIVE_HOLD');
    await user.keyboard('{ArrowLeft}');
    expect(
      screen.getByRole('heading', { name: 'Which question is worth asking?' }),
    ).toBeInTheDocument();
  });

  it('plays the story on its own', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    setMockSession('viewer');
    renderApp('/theatre');
    await screen.findByRole('region', { name: 'Customer’s phone' });
    expect(screen.getByRole('button', { name: /Step 1:/ })).toHaveAttribute('aria-current', 'step');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(
        holdMs(S1.steps[0] ?? { kind: 'prior', posterior: {}, entropy_bits: 0 }) + 50,
      );
    });
    expect(screen.getByRole('button', { name: /Step 2:/ })).toHaveAttribute('aria-current', 'step');
  });
});
