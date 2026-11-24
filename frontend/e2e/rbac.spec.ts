import { expect, test } from '@playwright/test';

import { ADMIN, ADMIN_PASSWORD, signIn, writeHeaders } from './session';

// E2E-RBAC (docs/21 §4, FR-UI-05): a VIEWER sees no write controls and no screen it may not
// open, and every write the api offers answers 403 to it, whatever the UI shows.
const VIEWER = `viewer-${String(Date.now())}`;
const VIEWER_PASSWORD = 'viewer correct horse battery';
const SOME_ID = '00000000-0000-7000-8000-000000000001';

// Every POST/PATCH in openapi.json except login and logout, with an empty body: the
// permission check answers before the body is read.
const WRITES: readonly (readonly [string, string])[] = [
  ['post', '/api/v1/admin/datasets'],
  ['post', '/api/v1/admin/demo/scenarios/S1/run'],
  ['post', '/api/v1/admin/sensing/train'],
  ['post', '/api/v1/admin/users'],
  ['patch', `/api/v1/admin/users/${SOME_ID}`],
  ['post', `/api/v1/cases/${SOME_ID}/disposition`],
  ['post', `/api/v1/cases/${SOME_ID}/evidence-requests`],
  ['post', `/api/v1/cases/${SOME_ID}/police-alert/send`],
  ['post', '/api/v1/guardian/sessions'],
  ['post', `/api/v1/guardian/sessions/${SOME_ID}/cancel`],
  ['post', `/api/v1/guardian/sessions/${SOME_ID}/probe-responses`],
  ['post', `/api/v1/jobs/${SOME_ID}/retry`],
];

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

test('a viewer sees no enabled write control on the screens it can open', async ({ page }) => {
  await signIn(page, VIEWER, VIEWER_PASSWORD);
  for (const path of ['/cases', '/guardian', '/campaigns', '/scout', '/kb', '/learning']) {
    await page.goto(path);
    await expect(page.locator('main')).toBeVisible();
    // Write controls are hidden or disabled for a viewer (docs/18 §3): no enabled submit.
    await expect(page.locator('main button[type="submit"]:enabled')).toHaveCount(0);
  }
});

test('every api write answers 403 to a viewer', async ({ page, baseURL }) => {
  await signIn(page, VIEWER, VIEWER_PASSWORD);
  for (const [method, path] of WRITES) {
    const response = await page.request.fetch(path, {
      method,
      headers: writeHeaders(baseURL),
      data: {},
    });
    expect(response.status(), `${method.toUpperCase()} ${path}`).toBe(403);
  }
});
