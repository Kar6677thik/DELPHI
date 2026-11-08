import { MutationCache, QueryCache, QueryClient, type QueryKey } from '@tanstack/react-query';

import { LOGIN_MUTATION_KEY, ME_QUERY_KEY } from '../api/auth';
import { ApiError, isUnauthenticated } from '../api/client';
import { QUERY_MAX_RETRIES } from './config';

const FIRST_SERVER_ERROR = 500;

function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < FIRST_SERVER_ERROR) return false;
  return failureCount < QUERY_MAX_RETRIES;
}

function sameKey(a: QueryKey | undefined, b: QueryKey): boolean {
  return a !== undefined && a.length === b.length && a.every((part, i) => part === b[i]);
}

/**
 * A 401 from any call means the session has ended (docs/16 §1). Restarting the `auth/me` query
 * makes RequireSession see it and send the user to /login?next=<where they were>. Sign-in's
 * own 401 (bad credentials) and `auth/me`'s (RequireSession handles it) are left alone.
 */
export function createQueryClient(): QueryClient {
  const sessionEnded = (error: unknown) => {
    if (isUnauthenticated(error)) void client.resetQueries({ queryKey: ME_QUERY_KEY });
  };
  const client: QueryClient = new QueryClient({
    queryCache: new QueryCache({
      onError: (error, query) => {
        if (!sameKey(query.queryKey, ME_QUERY_KEY)) sessionEnded(error);
      },
    }),
    mutationCache: new MutationCache({
      onError: (error, _variables, _context, mutation) => {
        if (!sameKey(mutation.options.mutationKey, LOGIN_MUTATION_KEY)) sessionEnded(error);
      },
    }),
    defaultOptions: { queries: { retry: shouldRetry } },
  });
  return client;
}
