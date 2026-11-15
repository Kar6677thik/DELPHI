import { http, HttpResponse } from 'msw';

import { CSRF_HEADER } from '../../src/api/client';
import type { CasePage, CaseView, LoginRequest, Me, Permission } from '../../src/api/types';

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

/** The case queue the mock api serves; tests replace it with `server.use`. */
export const MOCK_CASES: CasePage = { items: [mockCase()], next_cursor: null };

export const handlers = [
  http.get('/api/v1/cases', () => HttpResponse.json(MOCK_CASES)),
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
