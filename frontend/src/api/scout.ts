import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { apiFetch, getJson } from './client';
import type { ClusterPage, DraftPatch, DraftView, RunRequested, ScoutQueue } from './types';

/** The Scout pages' queries share this key, so runs, edits and backtests refresh them. */
export const SCOUT_QUERY_KEY = ['scout'] as const;

/** The queue's size is shown up to one page (docs/16 §1: `limit` ≤ 200), then "200+". */
export const QUEUE_PAGE = 200;
export const CLUSTER_PAGE = 50;

/** `GET /scout/queue`: the unexplained items waiting for a run (docs/11 §1). */
export function useScoutQueue() {
  return useQuery({
    queryKey: [...SCOUT_QUERY_KEY, 'queue'],
    queryFn: () => getJson<ScoutQueue>(`/scout/queue?limit=${String(QUEUE_PAGE)}`),
  });
}

/** `GET /scout/clusters`: newest first, each with its latest draft. */
export function useClusters() {
  return useQuery({
    queryKey: [...SCOUT_QUERY_KEY, 'clusters'],
    queryFn: () => getJson<ClusterPage>(`/scout/clusters?limit=${String(CLUSTER_PAGE)}`),
  });
}

/** `GET /typology-drafts/{id}`, with the KB issues the draft has as it stands. */
export function useDraft(id: string) {
  return useQuery({
    queryKey: [...SCOUT_QUERY_KEY, 'draft', id],
    queryFn: () => getJson<DraftView>(`/typology-drafts/${id}`),
  });
}

async function postJob(path: string): Promise<RunRequested> {
  const response = await apiFetch(path, {
    method: 'POST',
    // docs/16 §1: a retried request with the same key starts one job, not two.
    headers: { 'Idempotency-Key': crypto.randomUUID() },
  });
  return (await response.json()) as RunRequested;
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
  return useScoutMutation(() => postJob('/scout/runs'));
}

/**
 * `PATCH /typology-drafts/{id}` with the draft's ETag: 412 when someone saved in between, 422
 * with `errors` when the draft breaks a KB rule (nothing is saved then).
 */
export function useSaveDraft(id: string) {
  return useScoutMutation(async ({ version, patch }: { version: number; patch: DraftPatch }) => {
    const response = await apiFetch(`/typology-drafts/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'If-Match': `"${String(version)}"` },
      body: JSON.stringify(patch),
    });
    return (await response.json()) as DraftView;
  });
}

/** `POST /typology-drafts/{id}/backtest`: `RUN_BACKTEST` for the draft's current revision. */
export function useBacktest(id: string) {
  return useScoutMutation(() => postJob(`/typology-drafts/${id}/backtest`));
}
