import { useQuery } from '@tanstack/react-query';

import { getJson } from './client';
import type { GuardianSessionPage, MetricsSummary, StatusView } from './types';

/** The Overview's queries share this key, so a decision or a case event refreshes them. */
export const OVERVIEW_QUERY_KEY = ['overview'] as const;

/** How many recent decisions the Overview lists (docs/18 §2). */
export const RECENT_DECISIONS = 5;

export function useMetricsSummary(hours: number) {
  return useQuery({
    queryKey: [...OVERVIEW_QUERY_KEY, 'summary', hours],
    queryFn: () => getJson<MetricsSummary>(`/metrics/summary?hours=${String(hours)}`),
  });
}

export function useStatus() {
  return useQuery({
    queryKey: [...OVERVIEW_QUERY_KEY, 'status'],
    queryFn: () => getJson<StatusView>('/status'),
  });
}

export function useRecentDecisions() {
  return useQuery({
    queryKey: [...OVERVIEW_QUERY_KEY, 'recent'],
    queryFn: () =>
      getJson<GuardianSessionPage>(
        `/guardian/sessions?status=DECIDED&limit=${String(RECENT_DECISIONS)}`,
      ),
  });
}
