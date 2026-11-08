import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { RoleGate } from '../src/components/RoleGate';
import { SessionContext } from '../src/lib/session';
import { MOCK_USERS } from './mocks/handlers';

function renderAs(username: string | null, ui: React.ReactNode) {
  const me = username === null ? null : (MOCK_USERS[username] ?? null);
  render(<SessionContext.Provider value={me}>{ui}</SessionContext.Provider>);
}

describe('RoleGate (docs/18 §3)', () => {
  it('shows a node only with the permission', () => {
    renderAs(
      'admin',
      <RoleGate permission="admin">
        <span>admin tools</span>
      </RoleGate>,
    );
    expect(screen.getByText('admin tools')).toBeInTheDocument();
  });

  it('hides a node, or shows the fallback, without it', () => {
    renderAs(
      'viewer',
      <>
        <RoleGate permission="admin">
          <span>admin tools</span>
        </RoleGate>
        <RoleGate permission="fde" fallback={<span>read only</span>}>
          <span>edit draft</span>
        </RoleGate>
      </>,
    );
    expect(screen.queryByText('admin tools')).not.toBeInTheDocument();
    expect(screen.queryByText('edit draft')).not.toBeInTheDocument();
    expect(screen.getByText('read only')).toBeInTheDocument();
  });

  it('disables a write control for a VIEWER (E2E-RBAC, docs/21)', () => {
    renderAs(
      'viewer',
      <RoleGate permission="case:write">
        {(allowed) => <button disabled={!allowed}>Dispose case</button>}
      </RoleGate>,
    );
    expect(screen.getByRole('button', { name: 'Dispose case' })).toBeDisabled();
  });

  it('enables it for an INVESTIGATOR', () => {
    renderAs(
      'investigator',
      <RoleGate permission="case:write">
        {(allowed) => <button disabled={!allowed}>Dispose case</button>}
      </RoleGate>,
    );
    expect(screen.getByRole('button', { name: 'Dispose case' })).toBeEnabled();
  });

  it('allows nothing without a session', () => {
    renderAs(
      null,
      <RoleGate permission="view">
        <span>anything</span>
      </RoleGate>,
    );
    expect(screen.queryByText('anything')).not.toBeInTheDocument();
  });
});
