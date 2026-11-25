import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { ADMIN_QUERY_KEY } from './admin';
import { apiFetch, getJson } from './client';
import { type PaymentDraft, type Situation, situationOf } from './phone';
import type { components } from './schema';
import type { JobView } from './types';

type Schemas = components['schemas'];

/**
 * The demo story runner (docs/16 §2 "Admin and jobs", docs/14 §10, #101):
 * `POST /admin/demo/scenarios/{story}/run` queues a `DEMO_STORY` job. In `autoplay` the worker
 * plays the story on its own; in `interactive` the job's result is the payment the presenter
 * pays with from the mock phone.
 */

/** The stories that run so far (the server answers 422 for the others). */
export const STORIES = [
  { name: 'S1', title: 'S-1 Kamala', note: 'A digital-arrest call: ₹4,80,000 to a new payee.' },
  { name: 'S2', title: 'S-2 Arjun', note: 'A genuine hospital bill, from a new phone.' },
] as const;

export type StoryName = (typeof STORIES)[number]['name'];
export type StoryMode = Schemas['StoryRunRequest']['mode'];

/** How often an unfinished job is read again, in milliseconds. */
export const JOB_POLL_MS = 1000;

const FINISHED: ReadonlySet<JobView['status']> = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);

export function isFinished(job: JobView): boolean {
  return FINISHED.has(job.status);
}

export function useRunStory() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ story, mode }: { story: StoryName; mode: StoryMode }) => {
      const body: Schemas['StoryRunRequest'] = { mode };
      const response = await apiFetch(`/admin/demo/scenarios/${story}/run`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          // docs/16 §1: a retried request with the same key starts one job, not two.
          'Idempotency-Key': crypto.randomUUID(),
        },
        body: JSON.stringify(body),
      });
      return (await response.json()) as JobView;
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: ADMIN_QUERY_KEY });
    },
  });
}

/** `GET /jobs/{id}`, read again every second until the job finishes. */
export function useJob(id: string | null) {
  return useQuery({
    queryKey: [...ADMIN_QUERY_KEY, 'job', id],
    queryFn: () => getJson<JobView>(`/jobs/${encodeURIComponent(id ?? '')}`),
    enabled: id !== null,
    refetchInterval: (query) => {
      const job = query.state.data;
      return job && isFinished(job) ? false : JOB_POLL_MS;
    },
  });
}

/** An interactive story, staged on the phone: who pays, what, and around them. */
export interface StagedStory {
  story: string;
  customerId: string;
  payment: PaymentDraft;
  situation: Situation;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

/**
 * The payment an interactive `DEMO_STORY` job staged, from its result; `null` until it
 * succeeds. The story's scripted telemetry sets where "Around the customer" starts (S-1 is on a
 * call with an unknown number), and the presenter can still change it.
 */
export function stagedStory(job: JobView | undefined): StagedStory | null {
  if (job?.status !== 'SUCCEEDED' || !isRecord(job.result)) return null;
  const { story, mode, customer_id: customerId, payment } = job.result;
  if (mode !== 'interactive' || typeof story !== 'string' || typeof customerId !== 'string') {
    return null;
  }
  if (!isRecord(payment)) return null;
  const { telemetry, ...draft } = payment as unknown as PaymentDraft & {
    telemetry?: Schemas['TelemetryIn'];
  };
  return { story, customerId, payment: draft, situation: situationOf(telemetry) };
}
