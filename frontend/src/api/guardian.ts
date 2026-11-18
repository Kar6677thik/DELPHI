import { useQuery } from '@tanstack/react-query';

import type { HypothesisStyles } from '../components/HypothesisBar';
import { getJson } from './client';
import type { GuardianSessionPage, GuardianSessionView, KbVersionView } from './types';

/** Every Guardian query sits under this key, so a `guardian.*` event refreshes them all. */
export const GUARDIAN_QUERY_KEY = ['guardian'] as const;

export interface SessionFilters {
  status?: string;
  decision?: string;
  unexplained?: boolean;
}

function query(filters: SessionFilters): string {
  const params = new URLSearchParams({ limit: '50' });
  if (filters.status) params.set('status', filters.status);
  if (filters.decision) params.set('decision', filters.decision);
  if (filters.unexplained) params.set('unexplained', 'true');
  return params.toString();
}

/** The monitor's list, newest first (docs/16 `GET /guardian/sessions`): the first page. */
export function useGuardianSessions(filters: SessionFilters) {
  return useQuery({
    queryKey: [...GUARDIAN_QUERY_KEY, 'sessions', filters],
    queryFn: () => getJson<GuardianSessionPage>(`/guardian/sessions?${query(filters)}`),
  });
}

export function useGuardianSession(id: string) {
  return useQuery({
    queryKey: [...GUARDIAN_QUERY_KEY, 'session', id],
    queryFn: () => getJson<GuardianSessionView>(`/guardian/sessions/${encodeURIComponent(id)}`),
  });
}

/** The implicit open hypothesis has no KB file (docs/06 §2), so it gets its own style. */
const UNEXPLAINED: HypothesisStyles = {
  UNEXPLAINED: { name: 'Unexplained', color: '#64748B' },
};

/**
 * Each hypothesis's display name and colour in a KB version (docs/07 §4). A version never
 * changes once stored, so it is fetched once.
 */
export function useHypothesisStyles(versionId: number | undefined) {
  return useQuery({
    queryKey: ['kb', 'version', versionId],
    enabled: versionId !== undefined,
    staleTime: Infinity,
    queryFn: async (): Promise<HypothesisStyles> => {
      const version = await getJson<KbVersionView>(`/kb/versions/${String(versionId)}`);
      const content = version.content as {
        hypotheses?: Record<string, { display_name?: string; color?: string }>;
      };
      const styles: Record<string, { name: string; color: string }> = { ...UNEXPLAINED };
      for (const [key, h] of Object.entries(content.hypotheses ?? {})) {
        styles[key] = { name: h.display_name ?? key, color: h.color ?? '#64748B' };
      }
      return styles;
    },
  });
}
