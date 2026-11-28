import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';

import { apiFetch, getJson } from './client';
import { isFinished, JOB_POLL_MS } from './demo';
import type {
  BacktestRequested,
  ClusterPage,
  DraftPatch,
  DraftView,
  JobView,
  RunRequested,
  ScoutQueue,
} from './types';

/** The Scout pages' queries share this key, so runs, edits and backtests refresh them. */
export const SCOUT_QUERY_KEY = ['scout'] as const;

/** The waiting count reads one small page: past it, the page says "50+" (no count endpoint). */
export const QUEUE_COUNT_PAGE = 50;
export const CLUSTER_PAGE = 50;

/** `GET /scout/queue`: the unexplained items waiting for a run (docs/11 §1), one small page. */
export function useScoutQueue() {
  return useQuery({
    queryKey: [...SCOUT_QUERY_KEY, 'queue'],
    queryFn: () => getJson<ScoutQueue>(`/scout/queue?limit=${String(QUEUE_COUNT_PAGE)}`),
  });
}

/** `GET /scout/clusters`: newest first, each with its latest draft, a page at a time. */
export function useClusters() {
  return useInfiniteQuery({
    queryKey: [...SCOUT_QUERY_KEY, 'clusters'],
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: String(CLUSTER_PAGE) });
      if (pageParam) params.set('cursor', pageParam);
      return getJson<ClusterPage>(`/scout/clusters?${params.toString()}`);
    },
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/**
 * An open draft is re-read this often while it can still change: a backtest started elsewhere
 * (another tab, another engineer) writes its report with no event of its own (#309 review).
 */
export const DRAFT_POLL_MS = 30_000;
const LIVE_DRAFT = new Set(['CANDIDATE', 'DRAFT', 'BACKTESTED']);

/** `GET /typology-drafts/{id}`, with the KB issues the draft has as it stands. */
export function useDraft(id: string) {
  return useQuery({
    queryKey: [...SCOUT_QUERY_KEY, 'draft', id],
    queryFn: () => getJson<DraftView>(`/typology-drafts/${id}`),
    refetchInterval: (query) =>
      query.state.data && LIVE_DRAFT.has(query.state.data.status) ? DRAFT_POLL_MS : false,
  });
}

/**
 * A job this page started, read every second until it ends; then the Scout's queries refresh.
 * Only the page's own jobs do that, not every `job.updated` (a SIM_TICK every few seconds).
 */
export function useFollowJob(jobId: string | null | undefined) {
  const client = useQueryClient();
  const job = useQuery({
    queryKey: ['scout-job', jobId],
    queryFn: () => getJson<JobView>(`/jobs/${encodeURIComponent(jobId ?? '')}`),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      // Stop on an error too (after the query's own retries), rather than polling for good.
      if (query.state.status === 'error') return false;
      const data = query.state.data;
      return data && isFinished(data) ? false : JOB_POLL_MS;
    },
  });
  const finished = job.data !== undefined && isFinished(job.data);
  useEffect(() => {
    if (finished) void client.invalidateQueries({ queryKey: SCOUT_QUERY_KEY });
  }, [finished, client]);
  return job;
}

async function postJob<T>(path: string): Promise<T> {
  const response = await apiFetch(path, {
    method: 'POST',
    // docs/16 §1: a retried request with the same key starts one job, not two.
    headers: { 'Idempotency-Key': crypto.randomUUID() },
  });
  return (await response.json()) as T;
}

function useScoutMutation<I, O>(run: (input: I) => Promise<O>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSettled: () => {
      void client.invalidateQueries({ queryKey: SCOUT_QUERY_KEY });
    },
  });
}

/** `POST /scout/runs`: a `RUN_SCOUT` job over the queue. */
export function useRunScout() {
  return useScoutMutation(() => postJob<RunRequested>('/scout/runs'));
}

/**
 * `PATCH /typology-drafts/{id}` with the ETag of the version the edits started from: 412 when
 * someone saved in between, 409 when the draft can't be edited any more (e.g. SUPERSEDED), 422
 * with `errors` when it breaks a KB rule (nothing is saved then).
 */
export function useSaveDraft(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ version, patch }: { version: number; patch: DraftPatch }) => {
      const response = await apiFetch(`/typology-drafts/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', 'If-Match': `"${String(version)}"` },
        body: JSON.stringify(patch),
      });
      return (await response.json()) as DraftView;
    },
    onSuccess: (saved) => {
      // The editor shows the saved draft at once, not the old one until the refetch.
      client.setQueryData([...SCOUT_QUERY_KEY, 'draft', id], saved);
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: SCOUT_QUERY_KEY });
    },
  });
}

/**
 * `POST /typology-drafts/{id}/backtest`: `RUN_BACKTEST` for the draft's current revision. An
 * existing run of that revision comes back with `created: false` and its `status`.
 */
export function useBacktest(id: string) {
  return useScoutMutation(() => postJob<BacktestRequested>(`/typology-drafts/${id}/backtest`));
}
