import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { setMockSession } from '../src/mocks/handlers';
import { renderApp } from './renderApp';

function navLabels(): string[] {
  const nav = screen.getByRole('navigation', { name: 'Main' });
  return within(nav)
    .getAllByRole('link')
    .map((link) => link.textContent);
}

describe('app shell', () => {
  it('shows the Overview, the live-update status and the user', async () => {
    setMockSession('admin');
    renderApp('/');
    expect(await screen.findByRole('heading', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Live updates: Connecting…');
    expect(screen.getByRole('button', { name: 'Ada Admin (ADMIN)' })).toBeInTheDocument();
  });

  it('gives a VIEWER read-only navigation: no Phone, no Admin', async () => {
    setMockSession('viewer');
    renderApp('/');
    await screen.findByRole('heading', { name: 'Overview' });
    expect(navLabels()).toEqual([
      'Overview',
      'Guardian',
      'Cases',
      'Campaigns',
      'Scout',
      'KB',
      'Learning',
      'Evaluation',
    ]);
  });

  it('gives an ADMIN every screen', async () => {
    setMockSession('admin');
    renderApp('/');
    await screen.findByRole('heading', { name: 'Overview' });
    expect(navLabels()).toEqual([
      'Overview',
      'Phone',
      'Guardian',
      'Cases',
      'Campaigns',
      'Scout',
      'KB',
      'Learning',
      'Evaluation',
      'Admin',
    ]);
  });

  it('refuses a screen the role does not include', async () => {
    setMockSession('viewer');
    renderApp('/admin');
    expect(await screen.findByRole('heading', { name: 'No access' })).toBeInTheDocument();
  });

  it('routes detail screens and unknown paths', async () => {
    setMockSession('viewer');
    renderApp('/guardian/0192-abc');
    expect(await screen.findByRole('heading', { name: 'Session detail' })).toBeInTheDocument();
  });

  it('signs out from the user menu', async () => {
    setMockSession('fde');
    const { router, user } = renderApp('/kb');
    await user.click(await screen.findByRole('button', { name: 'Farah Engineer (FDE_ENGINEER)' }));
    await user.click(await screen.findByRole('menuitem', { name: 'Sign out' }));
    await screen.findByRole('heading', { name: 'Sign in to DELPHI' });
    expect(router.state.location.pathname).toBe('/login');
  });
});
