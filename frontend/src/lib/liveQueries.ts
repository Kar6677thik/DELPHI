import type { QueryClient, QueryKey } from '@tanstack/react-query';

import { CASES_QUERY_KEY } from '../api/cases';

/**
 * Which cached queries an SSE event makes stale (docs/18 §1: "SSE events invalidate the relevant
 * query keys"; docs/16 §3: payloads are small, clients re-fetch over REST). A prefix key
 * invalidates every query under it.
 */
export const LIVE_QUERIES: Readonly<Record<string, readonly QueryKey[]>> = {
  'case.opened': [CASES_QUERY_KEY.slice(0, 1)],
  'case.updated': [CASES_QUERY_KEY.slice(0, 1)],
};

export function invalidateFor(client: QueryClient, eventType: string): void {
  for (const queryKey of LIVE_QUERIES[eventType] ?? []) {
    void client.invalidateQueries({ queryKey });
  }
}
