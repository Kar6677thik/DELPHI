import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { ApiError, apiFetch, getJson } from './client';
import type { CaseDetail, CasePage, CaseSort, DispositionRequest, EvidenceRequest } from './types';
import type { components } from './schema';

/** Every case query sits under this key, so a `case.*` event refreshes them all. */
export const CASES_ROOT_KEY = ['cases'] as const;
export const CASES_QUERY_KEY = [...CASES_ROOT_KEY, 'queue'] as const;

/**
 * The case queue (docs/16 `GET /cases`): the first page, in the chosen order, optionally only
 * one campaign's cases.
 */
export function useCases(sort: CaseSort = 'sla_due_at', campaignId: string | null = null) {
  const filter = campaignId === null ? '' : `&campaign_id=${encodeURIComponent(campaignId)}`;
  return useQuery({
    queryKey: [...CASES_QUERY_KEY, sort, campaignId],
    queryFn: () => getJson<CasePage>(`/cases?sort=${sort}&limit=50${filter}`),
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

// ---------------------------------------------------------------- the case DD pack

/** One section of a DD pack (docs/09 §7). */
export interface DdPackSection {
  number: number;
  title: string;
  required: boolean;
  /** What the section holds: text, a list or a table; "Not applicable" when it does not apply. */
  content: unknown;
  /** Why a section is empty or does not apply. */
  note: string | null;
}

export interface DdPack {
  caseId: string;
  complete: boolean;
  sections: DdPackSection[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Reads `GET /cases/{id}/dd-pack` (docs/16). The api types the body as an open object (it serves
 * HTML too), so each field is checked here rather than cast; a malformed section is skipped.
 */
export function parseDdPack(body: unknown): DdPack {
  const data = isRecord(body) ? body : {};
  const sections = Array.isArray(data.sections) ? data.sections : [];
  return {
    caseId: typeof data.case_id === 'string' ? data.case_id : '',
    complete: data.complete === true,
    sections: sections.flatMap((s): DdPackSection[] =>
      isRecord(s) && typeof s.number === 'number' && typeof s.title === 'string'
        ? [
            {
              number: s.number,
              title: s.title,
              required: s.required === true,
              content: s.content,
              note: typeof s.note === 'string' ? s.note : null,
            },
          ]
        : [],
    ),
  };
}

/** The case's DD pack. Reading it records DD_PACK_VIEWED on the server (docs/09 §5). */
export function useCaseDdPack(id: string) {
  return useQuery({
    queryKey: [...caseQueryKey(id), 'dd-pack'],
    queryFn: async () =>
      parseDdPack(await getJson<unknown>(`/cases/${encodeURIComponent(id)}/dd-pack`)),
  });
}

// ---------------------------------------------------------------- the police alert

export type AlertAction = components['schemas']['AlertAction'];

/**
 * Send (recorded as SENT_SIMULATED) or discard a coercion case's police alert draft (docs/09 §6,
 * FR-NAV-10). Only a person does this; the answer is the case with its new version and ETag.
 * A second action on the same alert is 409 (docs/16).
 */
export function useActOnPoliceAlert(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (action: AlertAction) =>
      readCase(
        await apiFetch(`/cases/${encodeURIComponent(id)}/police-alert/${action}`, {
          method: 'POST',
        }),
      ),
    onSuccess: (data) => {
      client.setQueryData(caseQueryKey(id), data);
      void client.invalidateQueries({ queryKey: CASES_QUERY_KEY });
    },
    onError: (error) => {
      // 409: someone else acted on it first; show the alert as it now is.
      if (error instanceof ApiError && error.status === 409) {
        void client.invalidateQueries({ queryKey: caseQueryKey(id) });
      }
    },
  });
}
