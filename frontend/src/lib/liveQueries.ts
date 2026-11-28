import type { QueryClient, QueryKey } from '@tanstack/react-query';

import { ADMIN_QUERY_KEY } from '../api/admin';
import { CAMPAIGNS_ROOT_KEY } from '../api/campaigns';
import { CASES_ROOT_KEY } from '../api/cases';
import { GUARDIAN_QUERY_KEY } from '../api/guardian';
import { OVERVIEW_QUERY_KEY } from '../api/overview';

/**
 * Which cached queries an SSE event makes stale (docs/18 §1: "SSE events invalidate the relevant
 * query keys"; docs/16 §3: payloads are small, clients re-fetch over REST). A prefix key
 * invalidates every query under it.
 */
export const LIVE_QUERIES: Readonly<Record<string, readonly QueryKey[]>> = {
  'case.opened': [CASES_ROOT_KEY, OVERVIEW_QUERY_KEY],
  'case.updated': [CASES_ROOT_KEY, OVERVIEW_QUERY_KEY],
  // A recompute (#107) changes a campaign and the campaign column of its cases.
  'campaign.updated': [CAMPAIGNS_ROOT_KEY, CASES_ROOT_KEY],
  'evidence.narrative_ready': [CASES_ROOT_KEY],
  'guardian.session.updated': [GUARDIAN_QUERY_KEY],
  'guardian.session.decided': [GUARDIAN_QUERY_KEY, OVERVIEW_QUERY_KEY],
  'job.updated': [OVERVIEW_QUERY_KEY, ADMIN_QUERY_KEY],
};

export function invalidateFor(client: QueryClient, eventType: string): void {
  for (const queryKey of LIVE_QUERIES[eventType] ?? []) {
    void client.invalidateQueries({ queryKey });
  }
}
