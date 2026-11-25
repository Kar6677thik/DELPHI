import { type APIRequestContext, expect, test } from '@playwright/test';

import { ADMIN, ADMIN_PASSWORD, signIn, writeHeaders } from './session';

// E2E-RBAC (docs/21 §4, FR-UI-05): a VIEWER sees no screen it may not open, and every write the
// api offers answers 403 to it, whatever the UI shows. The write controls on a real case are
// checked in stories.spec.ts, which has the data.
const VIEWER = `viewer-${String(Date.now())}`;
const VIEWER_PASSWORD = 'viewer correct horse battery';
const SOME_ID = '00000000-0000-7000-8000-000000000001';

// Writes a viewer may make (docs/16): signing in and out, and recording trace events, which
// needs only `view`. Every other write in the api's openapi.json must answer 403.
const VIEWER_WRITES = new Set([
  'POST /api/v1/auth/login',
  'POST /api/v1/auth/logout',
  'POST /api/v1/cases/{case_id}/trace-events',
]);
const WRITE_METHODS = ['post', 'put', 'patch', 'delete'];

/** Every write in the running api's contract, path parameters filled, except VIEWER_WRITES. */
async function apiWrites(request: APIRequestContext): Promise<[string, string][]> {
  const response = await request.get('/api/v1/openapi.json');
  expect(response.ok()).toBe(true);
  const spec = (await response.json()) as { paths: Record<string, Record<string, unknown>> };
  const writes: [string, string][] = [];
  for (const [path, operations] of Object.entries(spec.paths)) {
    for (const method of Object.keys(operations).filter((m) => WRITE_METHODS.includes(m))) {
      if (VIEWER_WRITES.has(`${method.toUpperCase()} ${path}`)) continue;
      const filled = path.replace('{story}', 'S1').replace(/\{[a-z_]+\}/g, SOME_ID);
      writes.push([method, filled]);
    }
  }
  return writes;
}

test.beforeAll(async ({ browser, baseURL }) => {
  const page = await browser.newPage();
  await signIn(page, ADMIN, ADMIN_PASSWORD);
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

test('a viewer sees no write screens in the navigation and cannot open them', async ({ page }) => {
  await signIn(page, VIEWER, VIEWER_PASSWORD);
  const nav = page.getByRole('navigation');
  await expect(nav.getByRole('link', { name: 'Cases' })).toBeVisible();
  await expect(nav.getByRole('link', { name: 'Admin' })).toHaveCount(0);
  await expect(nav.getByRole('link', { name: 'Phone' })).toHaveCount(0);
  for (const path of ['/admin', '/phone']) {
    await page.goto(path);
    await expect(page.getByRole('heading', { name: 'No access' })).toBeVisible();
  }
});

test('every api write answers 403 FORBIDDEN to a viewer', async ({ page, baseURL }) => {
  await signIn(page, VIEWER, VIEWER_PASSWORD);
  const writes = await apiWrites(page.request);
  expect(writes.length).toBeGreaterThan(10);
  for (const [method, path] of writes) {
    // An empty body: the permission check answers before the body is read.
    const response = await page.request.fetch(path, {
      method,
      headers: writeHeaders(baseURL),
      data: {},
    });
    const what = `${method.toUpperCase()} ${path}`;
    expect(response.status(), what).toBe(403);
    // FORBIDDEN, not CSRF_REJECTED: the request passed the CSRF check and reached RBAC.
    expect(((await response.json()) as { code: string }).code, what).toBe('FORBIDDEN');
  }
});
