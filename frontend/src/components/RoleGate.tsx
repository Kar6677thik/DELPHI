import type { ReactNode } from 'react';

import type { Permission } from '../api/types';
import { useHasPermission } from '../lib/session';

interface RoleGateProps {
  permission: Permission;
  /**
   * A node is shown only with the permission. A function always renders and receives
   * `allowed`, so a write control can be shown disabled instead of hidden.
   */
  children: ReactNode | ((allowed: boolean) => ReactNode);
  /** Shown instead of a hidden node. */
  fallback?: ReactNode;
}

/** Hides or disables controls by permission (docs/18 §3). The server enforces them anyway. */
export function RoleGate({ permission, children, fallback = null }: RoleGateProps) {
  const allowed = useHasPermission(permission);
  if (typeof children === 'function') return children(allowed);
  return allowed ? children : fallback;
}
