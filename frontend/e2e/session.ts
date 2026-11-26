import { expect, type Page } from '@playwright/test';

// Shared by the E2E journeys (docs/21 §4): the bootstrap admin from the compose stack.
export const ADMIN = process.env.E2E_USERNAME ?? 'admin';
export const ADMIN_PASSWORD = process.env.E2E_PASSWORD ?? 'dev-only-admin-pw';

/** The headers every mutating request needs (CSRF, docs/19 §4) plus an Idempotency-Key. */
export function writeHeaders(baseURL: string | undefined): Record<string, string> {
  return {
    'X-Delphi-Csrf': '1',
    Origin: new URL(baseURL ?? 'http://localhost:8000').origin,
    'Idempotency-Key': crypto.randomUUID(),
  };
}

export async function signIn(page: Page, username: string, password: string): Promise<void> {
  await page.goto('/');
  await page.getByLabel('Username').fill(username);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
}
