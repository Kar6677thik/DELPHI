import { type APIRequestContext, expect, type Page } from '@playwright/test';

import { writeHeaders } from './session';

// Shared by the story journeys (docs/21 §4): starting demo jobs within the API's rate limit,
// waiting for them, and putting the bank back to the seed so a story's path does not depend
// on what earlier journeys left behind.

const JOB_TIMEOUT_MS = 120_000;
/** One poll a second: the journeys share their session's bucket (20/s) with the SPA. */
export const POLL_MS = 1_000;
/** A 429 says when to come back (`Retry-After`); a few retries ride out a burst. */
const RATE_LIMIT_RETRIES = 5;
const MS_PER_S = 1_000;

export interface Job {
  id: string;
  status: string;
  result: Record<string, unknown> | null;
  error: string | null;
}

/** `POST` a write within the rate limit: a 429 waits for its `Retry-After`, then tries again. */
export async function postWrite(
  request: APIRequestContext,
  baseURL: string | undefined,
  path: string,
  data: unknown,
) {
  for (let attempt = 0; ; attempt += 1) {
    const response = await request.post(path, { headers: writeHeaders(baseURL), data });
    if (response.status() !== 429 || attempt >= RATE_LIMIT_RETRIES) return response;
    const wait = Number(response.headers()['retry-after'] ?? '1');
    await new Promise((resolve) => setTimeout(resolve, Math.max(wait, 1) * MS_PER_S));
  }
}

/** The job, once it finished; fails unless it SUCCEEDED. */
export async function finishedJob(request: APIRequestContext, id: string): Promise<Job> {
  let found: Job | undefined;
  await expect
    .poll(
      async () => {
        found = (await (await request.get(`/api/v1/jobs/${id}`)).json()) as Job;
        return found.status;
      },
      { timeout: JOB_TIMEOUT_MS, intervals: [POLL_MS], message: `job ${id}` },
    )
    .toMatch(/^(SUCCEEDED|FAILED|CANCELLED)$/);
  expect(found?.status, found?.error ?? '').toBe('SUCCEEDED');
  return found as Job;
}

/** Queue a job (202) and wait for it to succeed. */
export async function runJob(
  page: Page,
  baseURL: string | undefined,
  path: string,
  data: unknown,
): Promise<Job> {
  const response = await postWrite(page.request, baseURL, path, data);
  expect(response.status(), `${path}: ${await response.text()}`).toBe(202);
  return finishedJob(page.request, ((await response.json()) as Job).id);
}

/** Back to right after the seed (docs/14 §10): a story then runs on its scripted history. */
export async function resetToSeed(page: Page, baseURL: string | undefined): Promise<void> {
  await runJob(page, baseURL, '/api/v1/admin/demo/reset', {});
}

interface KbVersion {
  id: number;
  status: string;
}

/**
 * The texts a customer may be shown for a question probe (docs/08 §4): the APPROVED variants
 * in their language, or the English ones if none is approved yet (docs/21 E2E-S1).
 */
export async function approvedTexts(
  request: APIRequestContext,
  probeKey: string,
  language: string,
): Promise<string[]> {
  const versions = (await (await request.get('/api/v1/kb/versions')).json()) as {
    items: KbVersion[];
  };
  const active = versions.items.find((v) => v.status === 'ACTIVE');
  expect(active, 'an ACTIVE KB version').toBeDefined();
  const kb = (await (await request.get(`/api/v1/kb/versions/${String(active?.id)}`)).json()) as {
    content: {
      question_bank: Record<
        string,
        { variants: { language: string; status: string; text: string }[] }
      >;
    };
  };
  const approved = (kb.content.question_bank[probeKey]?.variants ?? []).filter(
    (v) => v.status === 'APPROVED',
  );
  const own = approved.filter((v) => v.language === language);
  const shown = own.length > 0 ? own : approved.filter((v) => v.language === 'en');
  return shown.map((v) => v.text.replace(/\s+/g, ' ').trim());
}
