import { act, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { ME_QUERY_KEY } from '../src/api/auth';
import { getJson, postJson } from '../src/api/client';
import { safeNext } from '../src/lib/safeNext';
import { lockoutMessage } from '../src/routes/LoginPage';
import { MOCK_PASSWORD, problem, setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

const LOCKED = 'Too many failed sign-in attempts. Try again later.';

describe('login and session', () => {
  it('sends an unauthenticated user to /login and back after signing in', async () => {
    const { router, user } = renderApp('/?view=queues');
    await screen.findByRole('heading', { name: 'Sign in to DELPHI' });
    expect(router.state.location.search).toBe('?next=%2F%3Fview%3Dqueues');

    await user.type(screen.getByLabelText('Username'), 'investigator');
    await user.type(screen.getByLabelText('Password'), MOCK_PASSWORD);
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    await screen.findByRole('heading', { name: 'Overview' });
    expect(router.state.location.pathname).toBe('/');
    expect(router.state.location.search).toBe('?view=queues');
  });

  it('signs back in after the session expires, without looping to /login (#38 review)', async () => {
    setMockSession('admin');
    const { router, user, queryClient } = renderApp('/?view=queues');
    await screen.findByRole('heading', { name: 'Overview' });

    // The session expires: the next /auth/me answers 401 and the shell sends the user to login.
    setMockSession(null);
    await act(() => queryClient.refetchQueries({ queryKey: ME_QUERY_KEY }));
    await screen.findByRole('heading', { name: 'Sign in to DELPHI' });
    expect(router.state.location.search).toBe('?next=%2F%3Fview%3Dqueues');

    await user.type(screen.getByLabelText('Username'), 'admin');
    await user.type(screen.getByLabelText('Password'), MOCK_PASSWORD);
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    await screen.findByRole('heading', { name: 'Overview' });
    // Still signed in once every pending render has settled: no bounce back to /login.
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(screen.queryByRole('heading', { name: 'Sign in to DELPHI' })).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/');
    expect(router.state.location.search).toBe('?view=queues');
  });

  it('shows one generic message for wrong credentials (docs/19 §1)', async () => {
    const { user } = renderApp('/login');
    await user.type(await screen.findByLabelText('Username'), 'admin');
    await user.type(screen.getByLabelText('Password'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid username or password.');
  });

  it('validates empty fields locally before calling the server', async () => {
    const { user } = renderApp('/login');
    await user.click(await screen.findByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('Enter your username.')).toBeInTheDocument();
    expect(screen.getByText('Enter your password.')).toBeInTheDocument();
    expect(screen.getByLabelText('Username')).toHaveAttribute('aria-invalid', 'true');
  });

  it('says when to try again once sign-in is locked (429 RATE_LIMITED, docs/16 §1)', async () => {
    server.use(
      http.post('/api/v1/auth/login', ({ request }) =>
        problem(429, 'Too Many Requests', 'RATE_LIMITED', LOCKED, request.url, {
          'Retry-After': '840',
        }),
      ),
    );
    const { user, router } = renderApp('/login');
    await user.type(await screen.findByLabelText('Username'), 'admin');
    await user.type(screen.getByLabelText('Password'), MOCK_PASSWORD);
    await user.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Too many failed sign-in attempts. Try again in 14 minutes.',
    );
    expect(router.state.location.pathname).toBe('/login');
  });

  it('words the lockout from Retry-After', () => {
    expect(lockoutMessage(null)).toBe('Too many failed sign-in attempts. Try again later.');
    expect(lockoutMessage(1)).toBe('Too many failed sign-in attempts. Try again in 1 minute.');
    expect(lockoutMessage(61)).toBe('Too many failed sign-in attempts. Try again in 2 minutes.');
  });

  it('shows the problem for other failures', async () => {
    server.use(
      http.post('/api/v1/auth/login', ({ request }) =>
        problem(500, 'Internal Server Error', 'INTERNAL', 'Something broke.', request.url),
      ),
    );
    const { user } = renderApp('/login');
    await user.type(await screen.findByLabelText('Username'), 'admin');
    await user.type(screen.getByLabelText('Password'), MOCK_PASSWORD);
    await user.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Code INTERNAL');
  });

  it('sends the user to sign in when any call finds the session ended (401)', async () => {
    setMockSession('admin');
    const { router, queryClient } = renderApp('/?view=queues');
    await screen.findByRole('heading', { name: 'Overview' });
    setMockSession(null); // the session ends on the server
    server.use(
      http.get('/api/v1/jobs', ({ request }) =>
        problem(401, 'Unauthorized', 'UNAUTHENTICATED', 'Sign in to continue.', request.url),
      ),
    );
    await act(async () => {
      await queryClient
        .query({ queryKey: ['jobs'], queryFn: () => getJson('/jobs') })
        .catch(() => undefined);
    });
    await screen.findByRole('heading', { name: 'Sign in to DELPHI' });
    expect(router.state.location.search).toBe('?next=%2F%3Fview%3Dqueues');
  });

  it('does the same when a mutation finds the session ended', async () => {
    setMockSession('admin');
    const { router, queryClient } = renderApp('/kb');
    await screen.findByRole('navigation');
    setMockSession(null);
    server.use(
      http.post('/api/v1/jobs/x/retry', ({ request }) =>
        problem(401, 'Unauthorized', 'UNAUTHENTICATED', 'Sign in to continue.', request.url),
      ),
    );
    const mutation = queryClient
      .getMutationCache()
      .build(queryClient, { mutationFn: () => postJson('/jobs/x/retry') });
    await act(async () => {
      await mutation.execute(undefined).catch(() => undefined);
    });
    await screen.findByRole('heading', { name: 'Sign in to DELPHI' });
    expect(router.state.location.search).toBe('?next=%2Fkb');
  });

  it('only redirects to same-origin paths', () => {
    expect(safeNext('/kb?x=1')).toBe('/kb?x=1');
    expect(safeNext(null)).toBe('/');
    expect(safeNext('https://evil.example')).toBe('/');
    expect(safeNext('//evil.example')).toBe('/');
    expect(safeNext('/\\evil.example')).toBe('/');
  });

  it('shows not found for an unknown path', async () => {
    setMockSession('viewer');
    renderApp('/nowhere');
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
  });

  it('shows a problem when the session cannot be loaded', async () => {
    server.use(http.get('/api/v1/auth/me', () => new HttpResponse(null, { status: 503 })));
    renderApp('/');
    await waitFor(
      () => {
        expect(screen.getByRole('alert')).toHaveTextContent('Could not load your session');
      },
      { timeout: 10_000 },
    );
  });
});
