import type { QueryClient, QueryKey } from '@tanstack/react-query';

import { ADMIN_QUERY_KEY } from '../api/admin';
import { CASES_ROOT_KEY } from '../api/cases';
import { GUARDIAN_QUERY_KEY } from '../api/guardian';
import { OVERVIEW_QUERY_KEY } from '../api/overview';
import { QUESTION_BANK_KEY } from '../api/questionBank';

/**
 * Which cached queries an SSE event makes stale (docs/18 §1: "SSE events invalidate the relevant
 * query keys"; docs/16 §3: payloads are small, clients re-fetch over REST). A prefix key
 * invalidates every query under it.
 */
export const LIVE_QUERIES: Readonly<Record<string, readonly QueryKey[]>> = {
  'case.opened': [CASES_ROOT_KEY, OVERVIEW_QUERY_KEY],
  'case.updated': [CASES_ROOT_KEY, OVERVIEW_QUERY_KEY],
  'evidence.narrative_ready': [CASES_ROOT_KEY],
  'guardian.session.updated': [GUARDIAN_QUERY_KEY],
  'guardian.session.decided': [GUARDIAN_QUERY_KEY, OVERVIEW_QUERY_KEY],
  // A finished LLM_QUESTION_VARIANTS job brings new drafts to the question bank review.
  'job.updated': [OVERVIEW_QUERY_KEY, ADMIN_QUERY_KEY, QUESTION_BANK_KEY],
};

export function invalidateFor(client: QueryClient, eventType: string): void {
  for (const queryKey of LIVE_QUERIES[eventType] ?? []) {
    void client.invalidateQueries({ queryKey });
  }
}
