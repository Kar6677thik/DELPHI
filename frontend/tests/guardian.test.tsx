import { act, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import type {
  GuardianSessionSummary,
  GuardianSessionView,
  KbVersionView,
  ProbeRunView,
} from '../src/api/types';
import { FakeEventSource } from './fakeEventSource';
import { problem, setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

const SESSION_ID = '01930000-0000-7000-8000-0000000000bb';

function summary(overrides: Partial<GuardianSessionSummary> = {}): GuardianSessionSummary {
  return {
    id: SESSION_ID,
    payment: {
      id: '01930000-0000-7000-8000-0000000000aa',
      payer_account_id: '01930000-0000-7000-8000-0000000000a1',
      payee_account_id: '01930000-0000-7000-8000-0000000000a2',
      amount_paise: 48_000_000,
      channel: 'UPI_PUSH',
      initiated_at: '2026-11-14T18:10:00Z',
      device_id: null,
      status: 'HELD',
    },
    status: 'DECIDED',
    decision: 'PROTECTIVE_HOLD',
    terminal_reason: 'STOP',
    kb_version_id: 1,
    leading_hypothesis: 'APP_DIGITAL_ARREST',
    posterior: { APP_DIGITAL_ARREST: 0.71, GENUINE: 0.2, ATO: 0.02, UNEXPLAINED: 0.07 },
    unexplained: false,
    baselines: { B1: 'HOLD_FOR_REVIEW', B2: 'OTP_STEP_UP' },
    started_at: '2026-11-14T18:10:00Z',
    decided_at: '2026-11-14T18:11:30Z',
    case_id: '01930000-0000-7000-8000-0000000000cc',
    dd_pack_complete: true,
    version: 7,
    ...overrides,
  };
}

function run(overrides: Partial<ProbeRunView> = {}): ProbeRunView {
  return {
    id: '01930000-0000-7000-8000-0000000000d1',
    seq: 1,
    probe_key: 'CALL_STATUS',
    mode: 'PASSIVE',
    status: 'COMPLETED',
    selected_by: null,
    propensity: null,
    outcome: 'ON_CALL_UNKNOWN_NUMBER',
    posterior_before: null,
    posterior_after: { APP_DIGITAL_ARREST: 0.6, GENUINE: 0.4 },
    info_yield_bits: 0.4,
    voi_myopic_paise: null,
    net_value_paise: null,
    cost_paise: null,
    candidates: [],
    started_at: '2026-11-14T18:10:01Z',
    completed_at: '2026-11-14T18:10:02Z',
    ...overrides,
  };
}

function view(overrides: Partial<GuardianSessionView> = {}): GuardianSessionView {
  return {
    ...summary(),
    prior: {
      probs: { GENUINE: 0.9, APP_DIGITAL_ARREST: 0.1 },
      p_model: { GENUINE: 0.9 },
      anomaly_percentile: 0.95,
      open_mass: 0.11,
      top_features: [],
    },
    risk_by_decision: { RELEASE: 43_210_000, HOLD_FOR_REVIEW: 632_300, PROTECTIVE_HOLD: 176_100 },
    probe_runs: [
      run(),
      run({
        id: '01930000-0000-7000-8000-0000000000d2',
        seq: 2,
        probe_key: 'SPELL_BREAKER_POLICE',
        mode: 'ACTIVE',
        selected_by: 'EXPLORE',
        propensity: 0.15,
        outcome: 'HESITATION_OR_APP_SWITCH',
        posterior_after: { APP_DIGITAL_ARREST: 0.71, GENUINE: 0.2, ATO: 0.02, UNEXPLAINED: 0.07 },
        candidates: [
          {
            source_key: 'SPELL_BREAKER_POLICE',
            eligible: true,
            net_value_paise: 12_341_600,
            selected: true,
          },
          { source_key: 'OTP_STEP_UP', eligible: false, ineligible_reason: 'COERCION_DEFEATS' },
        ],
      }),
    ],
    ...overrides,
  };
}

const KB: KbVersionView = {
  id: 1,
  status: 'ACTIVE',
  parent_id: null,
  content_sha256: 'abc',
  change_summary: 'KB v1',
  created_by: null,
  created_at: '2026-11-01T00:00:00Z',
  activated_at: '2026-11-01T00:00:00Z',
  approved_by: null,
  retired_at: null,
  rollback_reason: null,
  content: {
    hypotheses: {
      APP_DIGITAL_ARREST: { display_name: 'Coerced victim — digital arrest', color: '#C2410C' },
      GENUINE: { display_name: 'Genuine', color: '#15803D' },
      ATO: { display_name: 'Account takeover', color: '#7C3AED' },
    },
  },
} as unknown as KbVersionView;

function serveKb() {
  server.use(http.get('/api/v1/kb/versions/1', () => HttpResponse.json(KB)));
}

describe('Guardian monitor (docs/18 §2 /guardian)', () => {
  it('lists the sessions with their hypothesis, decision and baselines', async () => {
    setMockSession('viewer');
    serveKb();
    server.use(
      http.get('/api/v1/guardian/sessions', () =>
        HttpResponse.json({
          items: [
            summary(),
            summary({
              id: '01930000-0000-7000-8000-0000000000b2',
              decision: 'RELEASE',
              leading_hypothesis: 'GENUINE',
              unexplained: true,
              baselines: { B1: 'RELEASE', B2: 'RELEASE' },
              payment: { ...summary().payment, amount_paise: 200_000 },
            }),
          ],
          next_cursor: null,
        }),
      ),
    );
    renderApp('/guardian');
    expect(await screen.findByRole('heading', { name: 'Guardian monitor' })).toBeInTheDocument();
    const rows = within(await screen.findByRole('table'))
      .getAllByRole('row')
      .slice(1);
    expect(rows).toHaveLength(2);
    expect(
      await within(rows[0] as HTMLElement).findByText('Coerced victim — digital arrest'),
    ).toBeInTheDocument();
    expect(rows[0]).toHaveTextContent('₹4,80,000.00');
    expect(rows[0]).toHaveTextContent('Protective hold');
    expect(rows[0]).toHaveTextContent('Hold for review / Otp step up');
    expect(within(rows[0] as HTMLElement).getByRole('link')).toHaveAttribute(
      'href',
      `/guardian/${SESSION_ID}`,
    );
    expect(rows[1]).toHaveTextContent('Unexplained');
    expect(rows[1]).toHaveTextContent('₹2,000.00');
  });

  it('sends the filters to the api', async () => {
    setMockSession('viewer');
    const seen: string[] = [];
    server.use(
      http.get('/api/v1/guardian/sessions', ({ request }) => {
        seen.push(new URL(request.url).search);
        return HttpResponse.json({ items: [], next_cursor: null });
      }),
    );
    const { user } = renderApp('/guardian');
    expect(await screen.findByText('No Guardian sessions match.')).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Decision'), 'PROTECTIVE_HOLD');
    await user.click(screen.getByLabelText('Unexplained only'));
    await screen.findByText('No Guardian sessions match.');
    expect(seen.at(-1)).toBe('?limit=50&decision=PROTECTIVE_HOLD&unexplained=true');
  });

  it('re-fetches when a session is decided (SSE, docs/18 §1)', async () => {
    setMockSession('viewer');
    serveKb();
    let calls = 0;
    server.use(
      http.get('/api/v1/guardian/sessions', () => {
        calls += 1;
        return HttpResponse.json({
          items: calls === 1 ? [] : [summary()],
          next_cursor: null,
        });
      }),
    );
    renderApp('/guardian');
    expect(await screen.findByText('No Guardian sessions match.')).toBeInTheDocument();
    act(() => {
      FakeEventSource.latest().emit('guardian.session.decided', '9');
    });
    expect(await screen.findByText('Protective hold')).toBeInTheDocument();
    expect(calls).toBe(2);
  });

  it('shows the problem when the list cannot load', async () => {
    setMockSession('viewer');
    server.use(
      http.get('/api/v1/guardian/sessions', ({ request }) =>
        problem(503, 'Service Unavailable', 'NOT_INITIALISED', 'Not ready.', request.url),
      ),
    );
    renderApp('/guardian');
    const alert = await screen.findByRole('alert', {}, { timeout: 5000 });
    expect(alert).toHaveTextContent('Could not load the sessions');
  });
});

describe('Guardian session detail (docs/18 §2 /guardian/:id)', () => {
  it('shows the timeline, the risk table, the baselines, the candidates and the DD pack link', async () => {
    setMockSession('viewer');
    serveKb();
    server.use(
      http.get(`/api/v1/guardian/sessions/${SESSION_ID}`, () => HttpResponse.json(view())),
    );
    renderApp(`/guardian/${SESSION_ID}`);
    expect(await screen.findByRole('heading', { name: 'Guardian session' })).toBeInTheDocument();
    const bars = await screen.findAllByRole('img');
    expect(bars.map((b) => b.getAttribute('aria-label')?.split(':')[0])).toEqual([
      'Prior',
      '1. Call status',
      '2. Spell breaker police',
    ]);
    expect(await screen.findByText('Coerced victim — digital arrest 71%')).toBeInTheDocument();
    const risk = screen.getByRole('table', { name: 'Expected loss by decision' });
    expect(within(risk).getAllByRole('row')[1]).toHaveTextContent('Protective hold(recommended)');
    expect(screen.getByText('OTP step-up (released if the OTP passes)')).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'Step 2 candidates' })).toHaveTextContent(
      'EXPLORE · p=0.15',
    );
    expect(screen.getByRole('link', { name: 'Open the DD pack' })).toHaveAttribute(
      'href',
      `/api/v1/guardian/sessions/${SESSION_ID}/dd-pack?format=html`,
    );
    expect(screen.getByText(/\(complete\)/)).toBeInTheDocument();
  });

  it('shows the problem for an unknown session', async () => {
    setMockSession('viewer');
    server.use(
      http.get('/api/v1/guardian/sessions/nope', ({ request }) =>
        problem(404, 'Not Found', 'NOT_FOUND', 'No Guardian session with this id.', request.url),
      ),
    );
    renderApp('/guardian/nope');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load the session');
    expect(alert).toHaveTextContent('NOT_FOUND');
  });
});
