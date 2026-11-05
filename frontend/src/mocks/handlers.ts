import { http, HttpResponse } from 'msw';

import { CSRF_HEADER } from '../api/client';
import type { LoginRequest, Me, Permission } from '../api/pending-contracts';

/**
 * A mocked auth API (docs/16 §2) for `pnpm dev:mock` and the tests, until #6 ships the real
 * one. Every mock user signs in with this password. It guards nothing.
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

// Kept in sessionStorage, so a reload in `pnpm dev:mock` stays signed in.
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

function problem(status: number, title: string, code: string, detail: string, url: string) {
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
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  );
}

export const handlers = [
  http.post('/api/v1/auth/login', async ({ request }) => {
    if (request.headers.get(CSRF_HEADER) !== '1') {
      return problem(403, 'Forbidden', 'CSRF_REJECTED', 'Missing CSRF header.', request.url);
    }
    const body = (await request.json()) as LoginRequest;
    const user = MOCK_USERS[body.username];
    if (!user || body.password !== MOCK_PASSWORD) {
      return problem(401, 'Unauthorized', 'UNAUTHENTICATED', 'Sign-in failed.', request.url);
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
      : problem(401, 'Unauthorized', 'UNAUTHENTICATED', 'Sign in first.', request.url);
  }),
];
