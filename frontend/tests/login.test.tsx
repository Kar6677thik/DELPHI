import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { safeNext } from '../src/lib/safeNext';
import { MOCK_PASSWORD, setMockSession } from '../src/mocks/handlers';
import { server } from '../src/mocks/node';
import { renderApp } from './renderApp';

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

  it('shows the problem for other failures, e.g. rate limiting', async () => {
    server.use(
      http.post('/api/v1/auth/login', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Too Many Requests',
            status: 429,
            detail: 'Too many sign-in attempts.',
            code: 'RATE_LIMITED',
            instance: '/api/v1/auth/login',
            request_id: 'req-429',
          },
          { status: 429, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    );
    const { user } = renderApp('/login');
    await user.type(await screen.findByLabelText('Username'), 'admin');
    await user.type(screen.getByLabelText('Password'), MOCK_PASSWORD);
    await user.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Code RATE_LIMITED');
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
