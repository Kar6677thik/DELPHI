import { useQuery } from '@tanstack/react-query';

import { getJson } from './client';
import type { CasePage } from './types';

export const CASES_QUERY_KEY = ['cases', 'queue'] as const;

/** The case queue, soonest SLA first (docs/16 `GET /cases`): the first page. */
export function useCases() {
  return useQuery({
    queryKey: CASES_QUERY_KEY,
    queryFn: () => getJson<CasePage>('/cases?sort=sla_due_at&limit=50'),
  });
}
