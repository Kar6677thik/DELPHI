import { http, HttpResponse } from 'msw';

import { CSRF_HEADER } from '../../src/api/client';
import type {
  CaseDetail,
  CasePage,
  CaseView,
  EvidenceRecord,
  LoginRequest,
  Me,
  MetricsSummary,
  Permission,
  StatusView,
} from '../../src/api/types';

/**
 * The auth API (docs/16 §2) as the tests see it: the real one's answers, in the real one's
 * types, so components can be tested without a backend. `pnpm dev` talks to the real api.
 * Every mock user signs in with this password.
 */
export const MOCK_PASSWORD = 'mock';

const WRITE: Permission[] = ['view', 'demo:drive', 'case:write', 'chase:write'];

export const MOCK_USERS: Record<string, Me> = {
  viewer: {
    id: '00000000-0000-4000-8000-000000000001',
    username: 'viewer',
    display_name: 'Guest Viewer',
    role: 'VIEWER',
    permissions: ['view'],
  },
  investigator: {
    id: '00000000-0000-4000-8000-000000000002',
    username: 'investigator',
    display_name: 'Ivy Investigator',
    role: 'INVESTIGATOR',
    permissions: WRITE,
  },
  fde: {
    id: '00000000-0000-4000-8000-000000000003',
    username: 'fde',
    display_name: 'Farah Engineer',
    role: 'FDE_ENGINEER',
    permissions: [...WRITE, 'fde'],
  },
  admin: {
    id: '00000000-0000-4000-8000-000000000004',
    username: 'admin',
    display_name: 'Ada Admin',
    role: 'ADMIN',
    permissions: [...WRITE, 'fde', 'admin'],
  },
};

// Kept in sessionStorage, as a cookie would outlive a re-render.
const SESSION_KEY = 'delphi.mock.session';

function signedIn(): Me | null {
  const username = sessionStorage.getItem(SESSION_KEY);
  return username === null ? null : (MOCK_USERS[username] ?? null);
}

/** Sign a mock user in (or out with null). Tests use it to skip the login form. */
export function setMockSession(username: string | null): void {
  if (username === null) sessionStorage.removeItem(SESSION_KEY);
  else sessionStorage.setItem(SESSION_KEY, username);
}

/** The api's own texts (backend/src/delphi/api/routers/auth.py). */
export const INVALID_LOGIN = 'The username or password is incorrect.';

export function problem(
  status: number,
  title: string,
  code: string,
  detail: string,
  url: string,
  headers: Record<string, string> = {},
) {
  return HttpResponse.json(
    {
      type: 'about:blank',
      title,
      status,
      detail,
      code,
      instance: new URL(url).pathname,
      request_id: crypto.randomUUID(),
    },
    { status, headers: { 'Content-Type': 'application/problem+json', ...headers } },
  );
}

/** A case as `GET /cases` returns it (backend/src/delphi/api/routers/cases.py), opened at 10:00. */
export function mockCase(overrides: Partial<CaseView> = {}): CaseView {
  return {
    id: '01930000-0000-7000-8000-000000000001',
    type: 'PAYMENT_HOLD',
    protocol: 'COERCION',
    status: 'OPEN',
    subject_payment_id: '01930000-0000-7000-8000-0000000000aa',
    subject_account_id: null,
    guardian_session_id: '01930000-0000-7000-8000-0000000000bb',
    kb_version_id: 1,
    leading_hypothesis: 'APP_DIGITAL_ARREST',
    leading_hypothesis_name: 'Coerced victim — digital arrest',
    posterior: { APP_DIGITAL_ARREST: 0.71, GENUINE: 0.08, UNEXPLAINED: 0.1 },
    prior_components: {},
    recommended_decision: null,
    risk_by_decision: null,
    amount_at_stake_paise: 48_000_000,
    opened_at: '2026-11-14T10:00:00Z',
    sla_due_at: '2026-11-14T10:30:00Z',
    sla_breached: false,
    unexplained: false,
    dd_pack_complete: false,
    disposition: null,
    disposed_at: null,
    disposition_typology: null,
    disposition_confidence: null,
    is_override: false,
    override_rationale: null,
    disposed_by: null,
    version: 1,
    ...overrides,
  };
}

/** A fetched evidence record as `GET /cases/{id}` returns it (#102), with its template narrative. */
export function mockEvidence(overrides: Partial<EvidenceRecord> = {}): EvidenceRecord {
  const id = overrides.id ?? '01930000-0000-7000-8000-0000000000e1';
  return {
    id,
    source_key: 'INBOUND_FAN_IN',
    outcome: 'HIGH_FAN_IN',
    facts: { unrelated_payers: 14, window_h: 48 },
    record_refs: [],
    posterior_before: { APP_DIGITAL_ARREST: 0.5, MULE: 0.2, GENUINE: 0.3 },
    posterior_after: { APP_DIGITAL_ARREST: 0.7, MULE: 0.25, GENUINE: 0.05 },
    info_yield_bits: 0.4,
    rank_at_request: 1,
    net_value_at_request_paise: 120_000,
    fetch_ms: 12,
    requested_by: '00000000-0000-4000-8000-000000000002',
    requested_at: '2026-11-14T10:05:00Z',
    narrative: `14 payers with no prior relation to ACC-…5678 credited it in the 48 h before 10:05 [${id}].`,
    narrative_source: 'TEMPLATE',
    ...overrides,
  };
}

/** A case as the Navigator console reads it (`GET /cases/{id}`, #103). */
export function mockCaseDetail(overrides: Partial<CaseDetail> = {}): CaseDetail {
  return {
    ...mockCase(),
    status: 'IN_PROGRESS',
    posterior: { APP_DIGITAL_ARREST: 0.71, MULE: 0.11, GENUINE: 0.08, UNEXPLAINED: 0.1 },
    recommended_decision: 'CONFIRM_FRAUD',
    risk_by_decision: { CONFIRM_FRAUD: 1_200_000, RELEASE: 30_000_000 },
    next_best_evidence: [
      {
        source_key: 'CUSTOMER_OUTREACH_CALL',
        eligible: true,
        ineligible_reason: null,
        net_value_paise: 250_000,
        cost_paise: 40_000,
        expected_minutes: 8,
        settle_pct: 42.5,
        pinned_by_protocol: true,
      },
      {
        source_key: 'PASS_THROUGH_DWELL',
        eligible: true,
        ineligible_reason: null,
        net_value_paise: 90_000,
        cost_paise: 10_000,
        expected_minutes: 2,
        settle_pct: 18,
        pinned_by_protocol: false,
      },
    ],
    evidence_sufficient: false,
    what_would_change_my_mind: null,
    evidence_records: [],
    police_alert: null,
    ...overrides,
  };
}

/** The KB version the console names hypotheses and outreach outcomes from (docs/07). */
export const MOCK_KB_CONTENT = {
  hypotheses: {
    APP_DIGITAL_ARREST: { display_name: 'Coerced victim — digital arrest', color: '#B91C1C' },
    MULE: { display_name: 'Mule account', color: '#7C3AED' },
    GENUINE: { display_name: 'Genuine', color: '#15803D' },
  },
  sources: {
    CUSTOMER_OUTREACH_CALL: {
      outcomes: [
        'CONFIRMS_COERCION',
        'CONFIRMS_GENUINE_PURPOSE',
        'DENIES_INITIATING',
        'UNREACHABLE',
      ],
    },
  },
  decision_profiles: {},
};

/** The case queue the mock api serves; tests replace it with `server.use`. */
export const MOCK_CASES: CasePage = { items: [mockCase()], next_cursor: null };

/** The Overview's live figures with nothing decided yet; tests replace it with `server.use`. */
export const MOCK_SUMMARY: MetricsSummary = {
  window_hours: 24,
  as_of: '2026-11-22T10:00:00Z',
  guardian: {
    decided: 0,
    decisions: {},
    median_session_seconds: null,
    active_probes_per_session: null,
    info_yield_bits: {},
    held: 0,
    label_coverage_held: null,
    dd_pack_completeness: null,
  },
  cases: {
    open_cases: 0,
    open_by_protocol: {},
    sla_breaches: 0,
    disposed: 0,
    median_hold_resolution_minutes: null,
  },
  golden_hour_ratio: null,
  alerts: [],
  alerts_not_tracked: ['BACKUP_AGE', 'SSE_OUTBOX_LAG', 'PROBE_YIELD_DEGRADED'],
};

export const MOCK_STATUS: StatusView = {
  dataset_ready: true,
  models_ready: true,
  seed_job_id: null,
  seed_progress: null,
  models: {},
  models_below_gate: [],
};

export const handlers = [
  http.get('/api/v1/cases', () => HttpResponse.json(MOCK_CASES)),
  http.get('/api/v1/metrics/summary', () => HttpResponse.json(MOCK_SUMMARY)),
  http.get('/api/v1/status', () => HttpResponse.json(MOCK_STATUS)),
  http.get('/api/v1/guardian/sessions', () => HttpResponse.json({ items: [], next_cursor: null })),
  http.post('/api/v1/auth/login', async ({ request }) => {
    if (request.headers.get(CSRF_HEADER) !== '1') {
      return problem(403, 'Forbidden', 'CSRF_REJECTED', 'Missing CSRF header.', request.url);
    }
    const body = (await request.json()) as LoginRequest;
    const user = MOCK_USERS[body.username];
    if (!user || body.password !== MOCK_PASSWORD) {
      return problem(401, 'Unauthorized', 'UNAUTHENTICATED', INVALID_LOGIN, request.url);
    }
    setMockSession(user.username);
    return new HttpResponse(null, { status: 204 });
  }),
  http.post('/api/v1/auth/logout', () => {
    setMockSession(null);
    return new HttpResponse(null, { status: 204 });
  }),
  http.get('/api/v1/auth/me', ({ request }) => {
    const me = signedIn();
    return me
      ? HttpResponse.json(me)
      : problem(401, 'Unauthorized', 'UNAUTHENTICATED', 'Sign in to continue.', request.url);
  }),
];
