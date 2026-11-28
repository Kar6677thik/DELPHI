import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { apiFetch } from './client';
import type { components } from './schema';

type Schemas = components['schemas'];

/** `GET /chase/operations/{id}` (docs/16 `ChaseView`). */
export type ChaseView = Schemas['ChaseView'];
/** An account still holding traced money, with its score (docs/10 §4). */
export type ChaseCandidate = Schemas['CandidateView'];
export type ChaseLien = Schemas['LienView'];
export type ChaseTicket = Schemas['TicketView'];

/** Every chase query sits under this key, so a `chase.*` event refreshes them all. */
export const CHASE_ROOT_KEY = ['chase'] as const;

export function chaseQueryKey(id: string) {
  return [...CHASE_ROOT_KEY, 'operation', id] as const;
}

/** A chase operation: its candidates, liens and tickets. */
export function useChase(id: string) {
  return useQuery({
    queryKey: chaseQueryKey(id),
    queryFn: async () =>
      (await (await apiFetch(`/chase/operations/${encodeURIComponent(id)}`)).json()) as ChaseView,
  });
}

/**
 * Approve liens on the selected candidates (docs/10 §5, FR-CHS-04): only a person does this.
 * The answer is the operation with the liens placed and the tickets sent.
 */
export function useApproveLiens(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (candidateIds: readonly string[]) => {
      const response = await apiFetch(`/chase/operations/${encodeURIComponent(id)}/liens`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          // docs/16 §1: a retried approval with the same key is answered once.
          'Idempotency-Key': crypto.randomUUID(),
        },
        body: JSON.stringify({ candidate_ids: candidateIds }),
      });
      return (await response.json()) as ChaseView;
    },
    onSuccess: (data) => {
      client.setQueryData(chaseQueryKey(id), data);
    },
  });
}
