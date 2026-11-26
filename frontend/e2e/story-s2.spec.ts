import { expect, type Locator, test } from '@playwright/test';

// E2E-S2 (docs/21 §4, #101): Arjun pays a hospital from a new phone with the real sensing
// prior, answering as his persona script does, and the payment is sent within 30 s after at
// most two active probes. Which step-up runs follows the live prior (R-01, #281): ENG-S2 pins
// the biometric-over-OTP choice on the fixture prior.
// The stack needs the demo dataset with its personas and trained models (ci.yml e2e job).
const USERNAME = process.env.E2E_USERNAME ?? 'admin';
const PASSWORD = process.env.E2E_PASSWORD ?? 'dev-only-admin-pw';

/** docs/21 E2E-S2: from paying to "Payment sent". */
const SENT_WITHIN_MS = 30_000;
/** docs/21 ENG-S2: at most two active probes. */
const MAX_ACTIVE_PROBES = 2;
/** Arjun's answer times, from `sim/config/personas.yaml` S2 (`after_s`); the OTP likewise. */
const STEP_UP_AFTER_MS = 2_000;
const PURPOSE_AFTER_MS = 5_000;
/** Staging the story is a worker job; the worker polls its queue. */
const STAGED_WITHIN_MS = 60_000;

interface SessionView {
  decision: string | null;
  probe_runs: { probe_key: string; mode: string }[];
}

test('E2E-S2: Arjun answers as scripted and the payment is sent', async ({ page }) => {
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
  const left = () => Math.max(SENT_WITHIN_MS - (Date.now() - started), 1);
  await phone.getByRole('button', { name: /^Pay/ }).click();
  const { id } = (await (await paid).json()) as { id: string };

  // Answer whatever the Guardian asks, at the script's pace: an instant answer reads as a
  // timing anomaly (docs/08 §4). The fingerprint matches; the payment is for a hospital; an
  // OTP is typed from the mock SMS.
  const sent = phone.getByText('Payment sent');
  const sensor = phone.getByRole('button', { name: 'Touch the sensor' });
  const purpose = phone.getByRole('button', { name: 'Medical / hospital' });
  const otp = phone.getByLabel('One-time code');
  const answer = async (screen: Locator, afterMs: number, act: () => Promise<void>) => {
    // Each probe screen is keyed by its run: wait for this one to go, even when the next
    // probe looks the same (two spell-breakers in a row both have a "No").
    const shown = await screen.elementHandle();
    await page.waitForTimeout(afterMs);
    await act();
    await page.waitForFunction((el) => !el?.isConnected, shown, { timeout: left() });
  };
  while (!(await sent.isVisible())) {
    await expect(sent.or(sensor).or(purpose).or(otp)).toBeVisible({ timeout: left() });
    if (await sensor.isVisible()) {
      await answer(sensor, STEP_UP_AFTER_MS, () => sensor.click());
    } else if (await purpose.isVisible()) {
      await answer(purpose, PURPOSE_AFTER_MS, () => purpose.click());
    } else if (await otp.isVisible()) {
      const sms = await phone.getByRole('status').filter({ hasText: 'one-time code' }).innerText();
      const code = /\b(\d{6})\b/.exec(sms)?.[1] ?? '';
      expect(code, 'the mock SMS carries the code').toHaveLength(6);
      await answer(otp, STEP_UP_AFTER_MS, async () => {
        await otp.fill(code);
        await phone.getByRole('button', { name: 'Confirm' }).click();
      });
    }
  }
  expect(Date.now() - started).toBeLessThanOrEqual(SENT_WITHIN_MS);

  const view = await page.request.get(`/api/v1/guardian/sessions/${id}`);
  expect(view.ok()).toBe(true);
  const session = (await view.json()) as SessionView;
  expect(session.decision).toBe('RELEASE');
  const active = session.probe_runs.filter((r) => r.mode === 'ACTIVE');
  expect(active.length).toBeLessThanOrEqual(MAX_ACTIVE_PROBES);
  // STEP_UP_AUTH is exclusive (docs/06 §17): never both the biometric and the OTP.
  const stepUps = active.filter((r) => ['BIOMETRIC_STEP_UP', 'OTP_STEP_UP'].includes(r.probe_key));
  expect(stepUps.length).toBeLessThanOrEqual(1);
});
