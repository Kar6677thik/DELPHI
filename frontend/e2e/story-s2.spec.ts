import { expect, test } from '@playwright/test';

// E2E-S2 (docs/21 §4, #101): Arjun pays a hospital from a new phone; a fingerprint settles it,
// so the payment is sent within 30 s, and the session shows the biometric chosen over the OTP.
// The stack needs the test dataset with the personas and its trained models (ci.yml e2e job).
const USERNAME = process.env.E2E_USERNAME ?? 'admin';
const PASSWORD = process.env.E2E_PASSWORD ?? 'dev-only-admin-pw';

/** docs/21 E2E-S2: from paying to "Payment sent". */
const SENT_WITHIN_MS = 30_000;
/** Arjun's answer times, from `sim/config/personas.yaml` S2 (`after_s`). */
const BIOMETRIC_AFTER_MS = 2_000;
const PURPOSE_AFTER_MS = 5_000;
/** Staging the story is a worker job; the worker polls its queue. */
const STAGED_WITHIN_MS = 60_000;

interface Candidate {
  source_key: string;
  selected: boolean;
}
interface SessionView {
  id: string;
  decision: string | null;
  probe_runs: { probe_key: string; candidates: Candidate[] | null }[];
}

test('E2E-S2: Arjun confirms with a fingerprint and the payment is sent', async ({ page }) => {
  test.setTimeout(3 * 60_000);
  await page.goto('/');
  await page.getByLabel('Username').fill(USERNAME);
  await page.getByLabel('Password').fill(PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();

  await page.goto('/phone?story=S2');
  await expect(page.getByText(/S-2 Arjun is staged/)).toBeVisible({ timeout: STAGED_WITHIN_MS });

  const phone = page.getByRole('region', { name: 'Customer’s phone' });
  const paid = page.waitForResponse(
    (r) => r.url().endsWith('/api/v1/guardian/sessions') && r.request().method() === 'POST',
  );
  const started = Date.now();
  await phone.getByRole('button', { name: /^Pay/ }).click();
  const session = (await (await paid).json()) as SessionView;

  // Answer as Arjun's persona script does (docs/14 §6), whatever the Guardian asks, in its
  // order: the fingerprint matches, and the payment is for a hospital.
  const sent = phone.getByText('Payment sent');
  const sensor = phone.getByRole('button', { name: 'Touch the sensor' });
  const purpose = phone.getByRole('button', { name: 'Medical / hospital' });
  const left = () => Math.max(SENT_WITHIN_MS - (Date.now() - started), 1);
  while (!(await sent.isVisible())) {
    await expect(sent.or(sensor).or(purpose)).toBeVisible({ timeout: left() });
    for (const [answer, afterMs] of [
      [sensor, BIOMETRIC_AFTER_MS],
      [purpose, PURPOSE_AFTER_MS],
    ] as const) {
      if (await answer.isVisible()) {
        // As the script does: an instant answer reads as a timing anomaly (docs/08 §4).
        await page.waitForTimeout(afterMs);
        await answer.click();
        await expect(answer).toBeHidden({ timeout: left() }); // the answered screen goes
      }
    }
  }
  expect(Date.now() - started).toBeLessThanOrEqual(SENT_WITHIN_MS);

  const view = await page.request.get(`/api/v1/guardian/sessions/${session.id}`);
  expect(view.ok()).toBe(true);
  const decided = (await view.json()) as SessionView;
  expect(decided.decision).toBe('RELEASE');
  const first = decided.probe_runs.find((r) => r.probe_key === 'BIOMETRIC_STEP_UP');
  expect(first, 'the Guardian ran the biometric').toBeDefined();
  const otp = first?.candidates?.find((c) => c.source_key === 'OTP_STEP_UP');
  expect(otp, 'the OTP was a candidate').toBeDefined();
  expect(otp?.selected).toBe(false);
  expect(decided.probe_runs.some((r) => r.probe_key === 'OTP_STEP_UP')).toBe(false);
});
