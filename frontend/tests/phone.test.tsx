import { act, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import {
  CALM,
  MAX_SESSION_S,
  type Persona,
  type ProbeRequest,
  situationOf,
  telemetryFor,
} from '../src/api/phone';
import type { GuardianSessionView, JobView, KbVersionView } from '../src/api/types';
import { FakeEventSource } from './fakeEventSource';
import { MOCK_KB_CONTENT, problem, setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

/** The mock phone's presenter view (docs/18 §2 `/phone`, #99) on the docs/08 §4 contract. */

const SESSION = '01930000-0000-7000-8000-0000000000b9';
const RUN = '01930000-0000-7000-8000-0000000000e9';

const PERSONA: Persona = {
  customer_id: 'c-ramesh',
  persona_key: 'RAMESH',
  name: 'Ramesh Rao',
  language: 'te',
  vulnerable: true,
  accounts: [
    {
      id: 'a-ramesh',
      label: 'Savings ·· 4821',
      vpa: 'customer004821@dlph',
      balance_paise: 1_250_000_00,
    },
  ],
  payees: [
    {
      account_id: 'a-cbi',
      label: 'CBI Safe Account',
      vpa: 'person000042@xbk2',
      added_at: '2026-11-22T09:00:00Z',
    },
    {
      account_id: 'a-anita',
      label: 'Anita Rao',
      vpa: null,
      added_at: '2024-11-22T09:00:00Z',
    },
  ],
  devices: [
    {
      id: 'd-ramesh',
      label: 'Ramesh’s phone',
      os: 'ANDROID',
      first_seen_at: '2023-01-01T00:00:00Z',
    },
  ],
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
  const sent: { payment?: unknown; answer?: unknown; idempotencyKey?: string | null } = {};
  server.use(
    http.get('/api/v1/phone/personas', () => HttpResponse.json([PERSONA])),
    http.get('/api/v1/kb/versions/1', () => HttpResponse.json(KB)),
    http.get('/api/v1/kb/versions', () => HttpResponse.json({ items: [KB], next_cursor: null })),
    http.post('/api/v1/guardian/sessions', async ({ request }) => {
      sent.payment = await request.json();
      sent.idempotencyKey = request.headers.get('Idempotency-Key');
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
    expect(sent.payment).toMatchObject({
      payment: {
        payer_account_id: 'a-ramesh',
        payee_account_id: 'a-cbi',
        amount_paise: 4_80_000_00,
        channel: 'UPI_PUSH',
        device_id: 'd-ramesh',
        // Calm by default: no call, no shared screen, natural typing.
        telemetry: {
          on_call: false,
          call_counterparty_known: null,
          remote_access_active: false,
          screen_share_active: false,
          paste_on_account_field: false,
        },
      },
    });
    expect(sent.idempotencyKey).toMatch(/^[0-9a-f-]{36}$/);
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

  it('sends what the presenter set around the customer as the telemetry', async () => {
    setMockSession('investigator');
    const sent = mockApi();
    const { user } = renderApp('/phone');
    const phone = await screen.findByRole('region', { name: 'Customer’s phone' });
    const around = screen.getByRole('group', { name: 'Around the customer' });
    await user.click(within(around).getByRole('button', { name: 'On a call, unknown number' }));
    await user.click(within(around).getByLabelText('Screen shared or remote access'));
    await user.click(within(around).getByRole('button', { name: 'Not their rhythm' }));
    await user.type(within(phone).getByLabelText('Amount'), '480000');
    await user.click(within(phone).getByRole('button', { name: /Pay/ }));
    await within(phone).findByText('Checking this payment');
    expect(sent.payment).toMatchObject({
      payment: {
        telemetry: {
          on_call: true,
          call_counterparty_known: false,
          remote_access_active: true,
          screen_share_active: true,
          paste_on_account_field: false,
        },
      },
    });
  });

  it('puts each typing preset on the intended side of the KB thresholds', () => {
    // kb/engine.yaml classifiers: dwell > 20 s or a paste → hesitant; cadence z > 3.0 → mismatch.
    const natural = telemetryFor(CALM, 42);
    expect(natural.paste_on_account_field || natural.max_field_dwell_s > 20).toBe(false);
    expect(natural.typing_cadence_z).toBeLessThanOrEqual(3);
    expect(natural.session_duration_s).toBe(42);
    const hesitant = telemetryFor({ ...CALM, typing: 'HESITANT' }, 0.2);
    expect(hesitant.paste_on_account_field && hesitant.max_field_dwell_s > 20).toBe(true);
    // A session lasts at least its longest dwell, and at most the API's 24 h bound.
    expect(hesitant.session_duration_s).toBe(hesitant.max_field_dwell_s);
    expect(telemetryFor(CALM, 2 * MAX_SESSION_S).session_duration_s).toBe(MAX_SESSION_S);
    expect(
      telemetryFor({ ...CALM, typing: 'NOT_THEIR_RHYTHM' }, 5).typing_cadence_z,
    ).toBeGreaterThan(3);
    const contact = telemetryFor({ ...CALM, call: 'CONTACT' }, 5);
    expect([contact.on_call, contact.call_counterparty_known]).toEqual([true, true]);
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

  it('shows the problem when the customers cannot load', async () => {
    setMockSession('investigator');
    server.use(
      http.get('/api/v1/phone/personas', ({ request }) =>
        problem(
          503,
          'Service Unavailable',
          'NOT_INITIALISED',
          'DELPHI is still initialising.',
          request.url,
        ),
      ),
    );
    renderApp('/phone');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The phone could not load its customers');
    expect(alert).toHaveTextContent('DELPHI is still initialising.');
  });
});

describe('/phone demo stories', () => {
  const JOB = '01930000-0000-7000-8000-0000000000d1';
  const STORY_TELEMETRY = {
    typing_cadence_z: 0.3,
    paste_on_account_field: false,
    max_field_dwell_s: 6,
    remote_access_active: false,
    screen_share_active: false,
    on_call: true,
    call_counterparty_known: false,
    session_duration_s: 95,
  };

  function storyJob(overrides: Partial<JobView> = {}): JobView {
    return {
      id: JOB,
      type: 'DEMO_STORY',
      status: 'SUCCEEDED',
      progress: 1,
      result: {
        story: 'S1',
        mode: 'interactive',
        customer_id: 'c-ramesh',
        payment: {
          payer_account_id: 'a-ramesh',
          payee_account_id: 'a-story-payee',
          amount_paise: 4_80_000_00,
          channel: 'IMPS',
          device_id: 'd-story',
          telemetry: STORY_TELEMETRY,
        },
      },
      error: null,
      attempts: 1,
      created_at: '2026-11-24T10:00:00Z',
      finished_at: '2026-11-24T10:00:01Z',
      ...overrides,
    };
  }

  it('reads where a story starts from its scripted telemetry', () => {
    expect(situationOf(undefined)).toEqual(CALM);
    expect(situationOf(STORY_TELEMETRY)).toEqual({
      call: 'UNKNOWN_NUMBER',
      screenShared: false,
      typing: 'NATURAL',
    });
    expect(
      situationOf({
        ...STORY_TELEMETRY,
        on_call: false,
        call_counterparty_known: null,
        remote_access_active: true,
        typing_cadence_z: 4.1,
      }),
    ).toEqual({ call: 'NONE', screenShared: true, typing: 'NOT_THEIR_RHYTHM' });
    expect(situationOf({ ...STORY_TELEMETRY, max_field_dwell_s: 34 }).typing).toBe('HESITANT');
  });

  it('stages an interactive story from ?story=, then pays its payment', async () => {
    setMockSession('investigator');
    const sent = mockApi();
    const runs: unknown[] = [];
    server.use(
      http.post('/api/v1/admin/demo/scenarios/S1/run', async ({ request }) => {
        runs.push(await request.json());
        return HttpResponse.json(storyJob({ status: 'QUEUED', result: null }), { status: 202 });
      }),
      http.get(`/api/v1/jobs/${JOB}`, () => HttpResponse.json(storyJob())),
    );
    const { user, router } = renderApp('/phone?story=S1');
    expect(await screen.findByText(/S-1 Kamala is staged/)).toBeVisible();
    expect(runs).toEqual([{ mode: 'interactive' }]);
    expect(router.state.location.search).toBe('');

    const around = screen.getByRole('group', { name: 'Around the customer' });
    expect(
      within(around).getByRole('button', { name: 'On a call, unknown number' }),
    ).toHaveAttribute('aria-pressed', 'true');
    const phone = screen.getByRole('region', { name: 'Customer’s phone' });
    expect(within(phone).getByRole('radio', { name: 'New payee' })).toBeChecked();
    expect(within(phone).getByLabelText('Amount')).toHaveValue('480000');

    await user.click(within(phone).getByRole('button', { name: /Pay/ }));
    await within(phone).findByText('Checking this payment');
    expect(sent.payment).toMatchObject({
      payment: {
        payer_account_id: 'a-ramesh',
        payee_account_id: 'a-story-payee',
        amount_paise: 4_80_000_00,
        channel: 'IMPS',
        device_id: 'd-story',
        telemetry: { on_call: true, call_counterparty_known: false },
      },
    });
  });

  it('clears the story for free play', async () => {
    setMockSession('investigator');
    mockApi();
    server.use(
      http.post('/api/v1/admin/demo/scenarios/S1/run', () =>
        HttpResponse.json(storyJob({ status: 'QUEUED', result: null }), { status: 202 }),
      ),
      http.get(`/api/v1/jobs/${JOB}`, () => HttpResponse.json(storyJob())),
    );
    const { user } = renderApp('/phone?story=S1');
    await screen.findByText(/S-1 Kamala is staged/);
    await user.selectOptions(screen.getByLabelText('Story'), '');
    const phone = screen.getByRole('region', { name: 'Customer’s phone' });
    expect(within(phone).queryByRole('radio', { name: 'New payee' })).toBeNull();
    expect(within(phone).getByLabelText('Amount')).toHaveValue('');
    const around = screen.getByRole('group', { name: 'Around the customer' });
    expect(within(around).getByRole('button', { name: 'No call' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('says when the story could not be staged', async () => {
    setMockSession('investigator');
    mockApi();
    server.use(
      http.post('/api/v1/admin/demo/scenarios/S2/run', () =>
        HttpResponse.json(storyJob({ status: 'QUEUED', result: null }), { status: 202 }),
      ),
      http.get(`/api/v1/jobs/${JOB}`, () =>
        HttpResponse.json(
          storyJob({ status: 'FAILED', result: null, error: 'the bank holds no dataset' }),
        ),
      ),
    );
    const { user } = renderApp('/phone');
    await user.selectOptions(await screen.findByLabelText('Story'), 'S2');
    expect(await screen.findByRole('alert')).toHaveTextContent('the bank holds no dataset');
  });
});
