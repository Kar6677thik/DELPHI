import { act, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import type { Persona, ProbeRequest } from '../src/api/phone';
import type { GuardianSessionView, KbVersionView } from '../src/api/types';
import { FakeEventSource } from './fakeEventSource';
import { MOCK_KB_CONTENT, problem, setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

/** The mock phone's presenter view (docs/18 §2 `/phone`, #99) on the docs/08 §4 contract. */

const SESSION = '01930000-0000-7000-8000-0000000000b9';
const RUN = '01930000-0000-7000-8000-0000000000e9';

const PERSONA: Persona = {
  customer_id: 'c-ramesh',
  name: 'Ramesh Rao',
  language: 'te',
  vulnerable: true,
  accounts: [{ id: 'a-ramesh', label: 'Savings ·· 4821', balance_paise: 1_250_000_00 }],
  payees: [
    { account_id: 'a-cbi', label: 'CBI Safe Account', note: 'New today' },
    { account_id: 'a-anita', label: 'Anita Rao', note: 'Saved 2 years ago' },
  ],
  devices: [{ id: 'd-ramesh', label: 'Ramesh’s phone' }],
};

function view(overrides: Partial<GuardianSessionView> = {}): GuardianSessionView {
  return {
    id: SESSION,
    payment: {
      id: 'p-1',
      payer_account_id: 'a-ramesh',
      payee_account_id: 'a-cbi',
      amount_paise: 4_80_000_00,
      channel: 'UPI_PUSH',
      initiated_at: '2026-11-22T10:00:00Z',
      device_id: 'd-ramesh',
      status: 'PENDING_GUARDIAN',
    },
    status: 'AWAITING_CUSTOMER',
    decision: null,
    terminal_reason: null,
    kb_version_id: 1,
    leading_hypothesis: 'APP_DIGITAL_ARREST',
    posterior: { APP_DIGITAL_ARREST: 0.62, GENUINE: 0.3, MULE: 0.08 },
    unexplained: false,
    baselines: null,
    started_at: '2026-11-22T10:00:00Z',
    decided_at: null,
    case_id: null,
    dd_pack_complete: false,
    version: 2,
    prior: {
      probs: { GENUINE: 0.9, APP_DIGITAL_ARREST: 0.1 },
      p_model: null,
      anomaly_percentile: null,
      open_mass: null,
      top_features: [],
    },
    risk_by_decision: null,
    probe_runs: [
      {
        id: RUN,
        seq: 1,
        probe_key: 'SPELL_BREAKER_DIGITAL_ARREST',
        mode: 'ACTIVE',
        status: 'AWAITING_CUSTOMER',
        selected_by: 'GREEDY',
        propensity: null,
        outcome: null,
        posterior_before: null,
        posterior_after: null,
        info_yield_bits: null,
        voi_myopic_paise: 120_000,
        net_value_paise: 90_000,
        cost_paise: 30_000,
        candidates: [
          {
            source_key: 'SPELL_BREAKER_DIGITAL_ARREST',
            eligible: true,
            cost_paise: 30_000,
            voi_myopic_paise: 120_000,
            net_value_paise: 90_000,
            settle_pct: 0.64,
            selected: true,
          },
        ],
        started_at: '2026-11-22T10:00:01Z',
        completed_at: null,
      },
    ],
    ...overrides,
  };
}

const DECIDED = view({
  status: 'DECIDED',
  decision: 'PROTECTIVE_HOLD',
  terminal_reason: 'STOP',
  posterior: { APP_DIGITAL_ARREST: 0.91, GENUINE: 0.05, MULE: 0.04 },
  risk_by_decision: { RELEASE: 4_10_000_00, PROTECTIVE_HOLD: 12_000 },
});

const KB: KbVersionView = {
  id: 1,
  version: 1,
  status: 'ACTIVE',
  content: MOCK_KB_CONTENT,
} as unknown as KbVersionView;

const QUESTION: ProbeRequest = {
  session_id: SESSION,
  probe_run_id: RUN,
  probe_key: 'SPELL_BREAKER_DIGITAL_ARREST',
  ui: 'QUESTION',
  language: 'te',
  text: 'Is anyone on a video call telling you to make this payment?',
  options: [
    { key: 'YES', text: 'Yes' },
    { key: 'NO', text: 'No' },
  ],
  timeout_s: 60,
};

function phoneStream(): FakeEventSource {
  const source = FakeEventSource.instances.findLast((s) => s.url.includes('/phone/stream'));
  if (!source) throw new Error('the phone opened no stream');
  return source;
}

function mockApi() {
  let current = view();
  const sent: { payment?: unknown; answer?: unknown } = {};
  server.use(
    http.get('/api/v1/phone/personas', () => HttpResponse.json([PERSONA])),
    http.get('/api/v1/kb/versions/1', () => HttpResponse.json(KB)),
    http.get('/api/v1/kb/versions', () => HttpResponse.json({ items: [KB], next_cursor: null })),
    http.post('/api/v1/guardian/sessions', async ({ request }) => {
      sent.payment = await request.json();
      return HttpResponse.json(current, { status: 201 });
    }),
    http.get(`/api/v1/guardian/sessions/${SESSION}`, () => HttpResponse.json(current)),
    http.post(`/api/v1/guardian/sessions/${SESSION}/probe-responses`, async ({ request }) => {
      sent.answer = await request.json();
      current = DECIDED;
      return HttpResponse.json(current);
    }),
  );
  return sent;
}

describe('/phone', () => {
  it('pays, asks the question the server sends, and shows the protective screen', async () => {
    setMockSession('investigator');
    const sent = mockApi();
    const { user } = renderApp('/phone');

    expect(await screen.findByRole('heading', { name: 'Mock phone' })).toBeInTheDocument();
    const phone = await screen.findByRole('region', { name: 'Customer’s phone' });
    expect(within(phone).getByText('₹12,50,000.00')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'What DELPHI weighs' })).toBeInTheDocument();
    expect(screen.getByText('Yes: losses weigh more')).toBeInTheDocument();

    await user.type(within(phone).getByLabelText('Amount'), '480000');
    await user.click(within(phone).getByRole('button', { name: 'Pay ₹4,80,000.00' }));
    expect(sent.payment).toEqual({
      payment: {
        payer_account_id: 'a-ramesh',
        payee_account_id: 'a-cbi',
        amount_paise: 4_80_000_00,
        channel: 'UPI_PUSH',
        device_id: 'd-ramesh',
        telemetry: {},
      },
    });
    expect(await within(phone).findByText('Checking this payment')).toBeInTheDocument();
    // The DELPHI side: the leading hypothesis leads, and why this question.
    const belief = await screen.findByRole('heading', { name: 'What DELPHI believes' });
    const beliefCard = belief.closest('section');
    expect(beliefCard).toHaveTextContent(/62%\s*Coerced victim — digital arrest/);
    expect(beliefCard).toHaveTextContent('₹4,80,000.00 · UPI');
    expect(screen.getByRole('heading', { name: 'Why this question' })).toBeInTheDocument();

    act(() => {
      phoneStream().emitData('probe.requested', QUESTION);
    });
    await user.click(await within(phone).findByRole('button', { name: 'No' }));
    expect(sent.answer).toMatchObject({ probe_run_id: RUN, answer: { option_key: 'NO' } });

    expect(await within(phone).findByRole('heading', { name: 'Hang up.' })).toBeInTheDocument();
    expect(
      await screen.findByRole('heading', { name: 'Expected loss by decision' }),
    ).toBeInTheDocument();
    await user.click(within(phone).getByRole('button', { name: 'Done' }));
    expect(within(phone).getByLabelText('Amount')).toHaveValue('');
  });

  it('shows the mock SMS and a wrong-code result for an OTP', async () => {
    setMockSession('investigator');
    mockApi();
    const { user } = renderApp('/phone');
    const phone = await screen.findByRole('region', { name: 'Customer’s phone' });
    await user.type(within(phone).getByLabelText('Amount'), '500');
    await user.click(within(phone).getByRole('button', { name: /Pay/ }));
    await within(phone).findByText('Checking this payment');
    const otp: ProbeRequest = { ...QUESTION, ui: 'OTP', probe_key: 'OTP_STEP_UP', options: [] };
    act(() => {
      phoneStream().emitData('probe.requested', otp);
      phoneStream().emitData('otp.sms', { session_id: SESSION, probe_run_id: RUN, code: '482913' });
    });
    expect(await within(phone).findByText(/482913 is your one-time code/)).toBeInTheDocument();
    act(() => {
      phoneStream().emitData('probe.attempt_result', {
        session_id: SESSION,
        probe_run_id: RUN,
        attempts_remaining: 1,
      });
    });
    expect(within(phone).getByRole('alert')).toHaveTextContent('1 try left');
  });

  it('ignores events for another session', async () => {
    setMockSession('investigator');
    mockApi();
    renderApp('/phone');
    const phone = await screen.findByRole('region', { name: 'Customer’s phone' });
    act(() => {
      phoneStream().emitData('probe.requested', { ...QUESTION, session_id: 'other' });
    });
    expect(within(phone).getByLabelText('Amount')).toBeInTheDocument();
  });

  it('goes phone only for the projector, and back with Escape', async () => {
    setMockSession('investigator');
    mockApi();
    const { user } = renderApp('/phone');
    await user.click(await screen.findByRole('button', { name: 'Phone only' }));
    expect(screen.queryByRole('heading', { name: 'Mock phone' })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Customer’s phone' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(await screen.findByRole('heading', { name: 'Mock phone' })).toBeInTheDocument();
  });

  it('says the phone needs the Guardian API while its endpoints are missing', async () => {
    setMockSession('investigator');
    server.use(
      http.get('/api/v1/phone/personas', ({ request }) =>
        problem(404, 'Not Found', 'NOT_FOUND', 'No such route.', request.url),
      ),
    );
    renderApp('/phone');
    expect(await screen.findByText(/arrive with the Guardian API \(#98\)/)).toBeInTheDocument();
  });
});
