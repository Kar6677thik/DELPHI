import { type APIRequestContext, expect, type Page, test } from '@playwright/test';

import { type Job, POLL_MS, runJob } from './demo';
import { ADMIN, ADMIN_PASSWORD, signIn, writeHeaders } from './session';

// The journeys that need data (docs/21 §4), in order: the admin seeds `test` with the personas
// (docs/21 §1), S-1 opens a case, a viewer finds that case's write controls hidden or disabled
// (E2E-RBAC), and the demo reset removes it so S-1 runs again (E2E-RESET).
// Up to three S-1 runs of about 20 s each (see caseFromS1), with their resets.
test.describe.configure({ mode: 'serial', timeout: 300_000 });

const VIEWER = `story-viewer-${String(Date.now())}`;
const VIEWER_PASSWORD = 'story viewer correct horse battery';
const JOB_TIMEOUT_MS = 120_000;
const CASE_WAIT_MS = 60_000; // S-1 autoplay opens its case in about 20 s
const CASE_ATTEMPTS = 3;

async function post(page: Page, baseURL: string | undefined, path: string, data: object) {
  return runJob(page, baseURL, path, data);
}

async function caseIds(request: APIRequestContext): Promise<string[]> {
  const page = (await (await request.get('/api/v1/cases')).json()) as { items: { id: string }[] };
  return page.items.map((c) => c.id);
}

/** Starts S-1 on the worker's autoplay customer; its job result names the session. */
async function startS1(page: Page, baseURL: string | undefined): Promise<Job> {
  return post(page, baseURL, '/api/v1/admin/demo/scenarios/S1/run', { mode: 'autoplay' });
}

async function reset(page: Page, baseURL: string | undefined): Promise<Job> {
  return post(page, baseURL, '/api/v1/admin/demo/reset', {});
}

/**
 * A case from S-1. The autoplay customer's answers are sampled, so about one run in five takes
 * the cooling-off path, whose window is 600 s in REAL time (kb/engine.yaml): that run opens no
 * case in time. Up to CASE_ATTEMPTS runs, each from a reset bank.
 */
async function caseFromS1(page: Page, baseURL: string | undefined): Promise<string> {
  for (let attempt = 1; attempt <= CASE_ATTEMPTS; attempt += 1) {
    await reset(page, baseURL);
    await startS1(page, baseURL);
    const deadline = Date.now() + CASE_WAIT_MS;
    while (Date.now() < deadline) {
      const [opened] = await caseIds(page.request);
      if (opened !== undefined) return opened;
      await page.waitForTimeout(1_000);
    }
  }
  throw new Error(`S-1 opened no case in ${String(CASE_ATTEMPTS)} runs`);
}

test.beforeAll(async ({ browser, baseURL }) => {
  const page = await browser.newPage();
  await signIn(page, ADMIN, ADMIN_PASSWORD);
  const status = (await (await page.request.get('/api/v1/status')).json()) as {
    dataset_ready: boolean;
  };
  if (!status.dataset_ready) {
    await post(page, baseURL, '/api/v1/admin/datasets', { profile: 'test', personas: true });
  }
  await expect
    .poll(
      async () => {
        const s = (await (await page.request.get('/api/v1/status')).json()) as {
          dataset_ready: boolean;
          models_ready: boolean;
        };
        return s.dataset_ready && s.models_ready;
      },
      {
        timeout: JOB_TIMEOUT_MS,
        intervals: [POLL_MS],
        message: 'the dataset and the sensing models are ready',
      },
    )
    .toBe(true);
  const created = await page.request.post('/api/v1/admin/users', {
    headers: writeHeaders(baseURL),
    data: {
      username: VIEWER,
      display_name: 'E2E viewer',
      password: VIEWER_PASSWORD,
      role: 'VIEWER',
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  await page.close();
});

test('E2E-RESET: the reset returns the bank to the seed, and S-1 runs again', async ({
  page,
  baseURL,
}) => {
  await signIn(page, ADMIN, ADMIN_PASSWORD);
  const first = await startS1(page, baseURL);
  const session = first.result?.session_id as string;
  expect((await page.request.get(`/api/v1/guardian/sessions/${session}`)).status()).toBe(200);
  const done = await reset(page, baseURL);
  expect(done.result).toHaveProperty('deleted');
  // Everything after the seed is gone: the story's session and any case it opened.
  expect((await page.request.get(`/api/v1/guardian/sessions/${session}`)).status()).toBe(404);
  expect(await caseIds(page.request)).toEqual([]);
  // The persona is back where the seed left her: the same customer plays S-1 again.
  const again = await startS1(page, baseURL);
  expect(again.result?.session_id).not.toBe(session);
  expect(again.result?.customer_id).toBe(first.result?.customer_id);
  expect(
    (
      await page.request.get(`/api/v1/guardian/sessions/${String(again.result?.session_id)}`)
    ).status(),
  ).toBe(200);
});

test('E2E-RBAC: a viewer finds the case console write controls hidden or disabled', async ({
  page,
  baseURL,
}) => {
  await signIn(page, ADMIN, ADMIN_PASSWORD);
  const caseId = await caseFromS1(page, baseURL);
  // The admin sees them, so the viewer's check below can fail.
  await page.goto(`/cases/${caseId}`);
  await expect(page.getByRole('form', { name: 'Decide the case' })).toBeVisible();
  await expect(
    page.getByRole('button', { name: /^(Fetch|Record call)$/, disabled: false }).first(),
  ).toBeVisible();
  await page.context().clearCookies();
  await signIn(page, VIEWER, VIEWER_PASSWORD);
  await page.goto(`/cases/${caseId}`);
  await expect(page.getByRole('heading', { name: 'Navigator console' })).toBeVisible();
  // Disposition: hidden. Evidence requests: shown, disabled (docs/18 §3).
  await expect(page.getByRole('form', { name: 'Decide the case' })).toHaveCount(0);
  await expect(page.getByText('Only investigators decide cases.')).toBeVisible();
  const evidence = page.getByRole('button', { name: /^(Fetch|Record call)$/ });
  await expect(evidence.first()).toBeVisible();
  for (const button of await evidence.all()) await expect(button).toBeDisabled();
});
