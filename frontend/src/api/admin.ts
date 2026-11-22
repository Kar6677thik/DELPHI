import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { apiFetch, getJson } from './client';
import { OVERVIEW_QUERY_KEY } from './overview';
import type {
  AuditVerifyView,
  DatasetRequest,
  JobPage,
  JobStatus,
  JobType,
  JobView,
} from './types';

/** The Admin screen's queries share this key, so a `job.updated` event refreshes them. */
export const ADMIN_QUERY_KEY = ['admin'] as const;

/** How many jobs one page of the list holds. */
export const JOB_PAGE_SIZE = 25;

export interface JobFilter {
  status?: JobStatus;
  type?: JobType;
}

export function useJobs(filter: JobFilter, limit: number) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (filter.status) params.set('status', filter.status);
  if (filter.type) params.set('type', filter.type);
  return useQuery({
    queryKey: [...ADMIN_QUERY_KEY, 'jobs', filter, limit],
    queryFn: () => getJson<JobPage>(`/jobs?${params.toString()}`),
    placeholderData: (previous) => previous,
  });
}

/** Checks the audit hash chain on demand: it reads every entry, so it is never automatic. */
export function useVerifyAudit() {
  return useMutation({
    mutationFn: () => getJson<AuditVerifyView>('/admin/audit/verify'),
  });
}

async function startJob(path: string, body?: unknown): Promise<JobView> {
  const response = await apiFetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      // docs/16 §1: a retried request with the same key starts one job, not two.
      'Idempotency-Key': crypto.randomUUID(),
    },
    body: JSON.stringify(body ?? {}),
  });
  return (await response.json()) as JobView;
}

function useJobMutation<T>(start: (input: T) => Promise<JobView>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: start,
    onSettled: () => {
      void client.invalidateQueries({ queryKey: ADMIN_QUERY_KEY });
      void client.invalidateQueries({ queryKey: OVERVIEW_QUERY_KEY });
    },
  });
}

/** `POST /admin/datasets`: 409 once the bank holds a dataset, or while one is being made. */
export function useGenerateDataset() {
  return useJobMutation((request: DatasetRequest) => startJob('/admin/datasets', request));
}

/** `POST /admin/sensing/train`: 409 without a dataset, or while a training runs. */
export function useTrainSensing() {
  return useJobMutation(() => startJob('/admin/sensing/train'));
}

/** `POST /jobs/{id}/retry`: only a FAILED job (409 otherwise). */
export function useRetryJob() {
  return useJobMutation(async (id: string) => {
    const response = await apiFetch(`/jobs/${encodeURIComponent(id)}/retry`, { method: 'POST' });
    return (await response.json()) as JobView;
  });
}
