import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { apiFetch, getJson } from './client';
import type { CaseDetail, CasePage, CaseSort, DispositionRequest, EvidenceRequest } from './types';

/** Every case query sits under this key, so a `case.*` event refreshes them all. */
export const CASES_ROOT_KEY = ['cases'] as const;
export const CASES_QUERY_KEY = [...CASES_ROOT_KEY, 'queue'] as const;

/** The case queue (docs/16 `GET /cases`): the first page, soonest SLA first or newest first. */
export function useCases(sort: CaseSort = 'sla_due_at') {
  return useQuery({
    queryKey: [...CASES_QUERY_KEY, sort],
    queryFn: () => getJson<CasePage>(`/cases?sort=${sort}&limit=50`),
  });
}

/** A case with the `ETag` it was read with: the disposition sends it back as `If-Match`. */
export interface CaseWithEtag {
  detail: CaseDetail;
  etag: string;
}

export function caseQueryKey(id: string) {
  return [...CASES_ROOT_KEY, 'case', id] as const;
}

async function readCase(response: Response): Promise<CaseWithEtag> {
  const detail = (await response.json()) as CaseDetail;
  // docs/16 §1: a mutable aggregate carries its version as the ETag.
  const etag = response.headers.get('etag') ?? `"${String(detail.version)}"`;
  return { detail, etag };
}

/** The Navigator console's case (docs/16 `GET /cases/{id}`). */
export function useCase(id: string) {
  return useQuery({
    queryKey: caseQueryKey(id),
    queryFn: async () => readCase(await apiFetch(`/cases/${encodeURIComponent(id)}`)),
  });
}

function post(path: string, body: unknown, headers: Record<string, string>) {
  return apiFetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      // docs/16 §1: a retried request with the same key is answered once.
      'Idempotency-Key': crypto.randomUUID(),
      ...headers,
    },
    body: JSON.stringify(body),
  });
}

/** Fetch one evidence item (docs/09 §3); the answer is the updated case. */
export function useRequestEvidence(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (request: EvidenceRequest) =>
      readCase(await post(`/cases/${encodeURIComponent(id)}/evidence-requests`, request, {})),
    onSuccess: (data) => {
      client.setQueryData(caseQueryKey(id), data);
      void client.invalidateQueries({ queryKey: CASES_QUERY_KEY });
    },
  });
}

/** Decide the case (docs/05 §4): `If-Match` is the ETag the console was showing (412 if stale). */
export function useDispose(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ request, etag }: { request: DispositionRequest; etag: string }) =>
      readCase(
        await post(`/cases/${encodeURIComponent(id)}/disposition`, request, { 'If-Match': etag }),
      ),
    onSuccess: (data) => {
      client.setQueryData(caseQueryKey(id), data);
      void client.invalidateQueries({ queryKey: CASES_QUERY_KEY });
    },
  });
}
