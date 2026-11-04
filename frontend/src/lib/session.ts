import { createContext, useContext } from 'react';

import type { Me, Permission } from '../api/pending-contracts';

/** The signed-in user, provided by RequireSession for every page inside the shell. */
export const SessionContext = createContext<Me | null>(null);

export function useSession(): Me | null {
  return useContext(SessionContext);
}

export function useHasPermission(permission: Permission): boolean {
  return useSession()?.permissions.includes(permission) ?? false;
}
