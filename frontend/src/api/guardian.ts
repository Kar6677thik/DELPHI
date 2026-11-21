import { useInfiniteQuery, useQueries, useQuery } from '@tanstack/react-query';

import type { HypothesisStyles } from '../components/HypothesisBar';
import { getJson } from './client';
import type {
  GuardianSessionPage,
  GuardianSessionView,
  KbVersionPage,
  KbVersionView,
  SessionMode,
  SessionStatus,
} from './types';

/** Every Guardian query sits under this key, so a `guardian.*` event refreshes them all. */
export const GUARDIAN_QUERY_KEY = ['guardian'] as const;

/** Rows per page of the monitor (docs/16 §1 allows up to 200). */
export const PAGE_SIZE = 50;

export interface SessionFilters {
  status?: SessionStatus;
  /** A decision of the KB's GUARDIAN_PAYMENT profile: the api checks it against the active KB. */
  decision?: string;
  mode?: SessionMode;
  unexplained?: boolean;
}

function query(filters: SessionFilters, cursor: string | undefined): string {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (filters.status) params.set('status', filters.status);
  if (filters.decision) params.set('decision', filters.decision);
  if (filters.mode) params.set('mode', filters.mode);
  if (filters.unexplained) params.set('unexplained', 'true');
  if (cursor) params.set('cursor', cursor);
  return params.toString();
}

/** The monitor's list, newest first (docs/16 `GET /guardian/sessions`), a page at a time. */
export function useGuardianSessions(filters: SessionFilters) {
  return useInfiniteQuery({
    queryKey: [...GUARDIAN_QUERY_KEY, 'sessions', filters],
    queryFn: ({ pageParam }) =>
      getJson<GuardianSessionPage>(`/guardian/sessions?${query(filters, pageParam)}`),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function useGuardianSession(id: string) {
  return useQuery({
    queryKey: [...GUARDIAN_QUERY_KEY, 'session', id],
    queryFn: () => getJson<GuardianSessionView>(`/guardian/sessions/${encodeURIComponent(id)}`),
  });
}

// ---------------------------------------------------------------- KB display data

/** What the monitor shows from a KB version (docs/07 §4): names, colours, Guardian decisions. */
export interface KbDisplay {
  styles: HypothesisStyles;
  /** The GUARDIAN_PAYMENT profile's decisions, least intrusive first. */
  decisions: readonly string[];
  /** Each source's outcomes (docs/07 §3), e.g. what an outreach call can record. */
  outcomes: Readonly<Record<string, readonly string[]>>;
}

const GUARDIAN_PROFILE = 'GUARDIAN_PAYMENT';
const UNSTYLED = '#94A3B8'; // the bar's fallback grey (slate-400)

/** The implicit open hypothesis has no KB file (docs/06 §2), so it gets its own style. */
const UNEXPLAINED: HypothesisStyles = {
  UNEXPLAINED: { name: 'Unexplained', color: UNSTYLED },
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Reads the display data out of a KB version's content. The api types `content` as an open
 * object (it is the whole KB, docs/07), so each field is checked here rather than cast.
 */
export function kbDisplay(content: unknown): KbDisplay {
  const kb = isRecord(content) ? content : {};
  const styles: Record<string, { name: string; color: string }> = { ...UNEXPLAINED };
  const hypotheses = isRecord(kb.hypotheses) ? kb.hypotheses : {};
  for (const [key, h] of Object.entries(hypotheses)) {
    const entry = isRecord(h) ? h : {};
    styles[key] = {
      name: typeof entry.display_name === 'string' ? entry.display_name : key,
      color: typeof entry.color === 'string' ? entry.color : UNSTYLED,
    };
  }
  const profiles = isRecord(kb.decision_profiles) ? kb.decision_profiles : {};
  const profile = isRecord(profiles[GUARDIAN_PROFILE]) ? profiles[GUARDIAN_PROFILE] : {};
  const decisions = Array.isArray(profile.decisions)
    ? profile.decisions.filter((d): d is string => typeof d === 'string')
    : [];
  const outcomes: Record<string, string[]> = {};
  const sources = isRecord(kb.sources) ? kb.sources : {};
  for (const [key, source] of Object.entries(sources)) {
    const list = isRecord(source) && Array.isArray(source.outcomes) ? source.outcomes : [];
    outcomes[key] = list.filter((o): o is string => typeof o === 'string');
  }
  return { styles, decisions, outcomes };
}

function kbVersionQuery(versionId: number) {
  return {
    queryKey: ['kb', 'version', versionId],
    staleTime: Infinity, // a stored version never changes
    queryFn: () => getJson<KbVersionView>(`/kb/versions/${String(versionId)}`),
    select: (version: KbVersionView) => kbDisplay(version.content),
  };
}

/** One KB version's display data, fetched once. */
export function useKbDisplay(versionId: number | undefined) {
  return useQuery({ ...kbVersionQuery(versionId ?? 0), enabled: versionId !== undefined });
}

/** Display data for each of several KB versions, so each row is named by its own version. */
export function useKbDisplays(versionIds: readonly number[]): ReadonlyMap<number, KbDisplay> {
  return useQueries({
    queries: versionIds.map((id) => kbVersionQuery(id)),
    combine: (results) =>
      new Map(
        results.flatMap((r, i) => {
          const id = versionIds[i];
          return r.data && id !== undefined ? [[id, r.data] as const] : [];
        }),
      ),
  });
}

/** The ACTIVE KB version's display data: its decisions are what the api's filter accepts. */
export function useActiveKbDisplay() {
  const versions = useQuery({
    queryKey: ['kb', 'versions'],
    queryFn: () => getJson<KbVersionPage>('/kb/versions?limit=200'),
  });
  const active = versions.data?.items.find((v) => v.status === 'ACTIVE')?.id;
  return useKbDisplay(active);
}
