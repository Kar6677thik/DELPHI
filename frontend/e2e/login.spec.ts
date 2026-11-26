import { expect, test } from '@playwright/test';

import { ADMIN as USERNAME, ADMIN_PASSWORD as PASSWORD } from './session';

// The M0 smoke test (docs/25 M0, #10): the deployed image serves the SPA, and the bootstrap
// admin can sign in and out against the real api and database.

test('an unknown password is refused', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Sign in to DELPHI' })).toBeVisible();
  await page.getByLabel('Username').fill(USERNAME);
  await page.getByLabel('Password').fill('not the password');
  await page.getByRole('button', { name: 'Sign in' }).click();
  // Generic on purpose: the page never says which of the two was wrong (docs/19 §1).
  await expect(page.getByRole('alert')).toHaveText('Invalid username or password.');
  await expect(page.getByRole('heading', { name: 'Overview' })).toHaveCount(0);
});

test('the admin signs in, sees the Overview and signs out', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Username').fill(USERNAME);
  await page.getByLabel('Password').fill(PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
  await page.getByRole('button', { name: /\(ADMIN\)/ }).click();
  await page.getByRole('menuitem', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in to DELPHI' })).toBeVisible();
});
