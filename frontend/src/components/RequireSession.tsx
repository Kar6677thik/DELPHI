import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router';

import { useMe } from '../api/auth';
import { isUnauthenticated } from '../api/client';
import { SessionContext } from '../lib/session';
import { ProblemAlert } from './ProblemAlert';

/** Signs the user in first, then provides the session to everything below. */
export function RequireSession({ children }: { children: ReactNode }) {
  const me = useMe();
  const location = useLocation();

  if (me.isPending) {
    return (
      <p role="status" className="p-6 text-sm text-slate-600">
        Loading…
      </p>
    );
  }
  if (me.isError) {
    if (isUnauthenticated(me.error)) {
      const next = encodeURIComponent(location.pathname + location.search);
      return <Navigate to={`/login?next=${next}`} replace />;
    }
    return (
      <div className="p-6">
        <ProblemAlert error={me.error} title="Could not load your session" />
      </div>
    );
  }
  return <SessionContext.Provider value={me.data}>{children}</SessionContext.Provider>;
}
