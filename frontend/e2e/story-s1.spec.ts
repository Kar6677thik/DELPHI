import { expect, type Locator, type Page, test } from '@playwright/test';

import { approvedTexts, resetToSeed } from './demo';

// E2E-S1, the Guardian half (docs/21 §4, #101): Kamala, on a call with an unknown number and
// typing normally, pays a "safe account" at her script's 23:40 (#291); answering what is asked
// as her persona script does (purpose, then the spell-breaker with an app switch), she ends on
// the protective screen. The Navigator half (the case, the outreach, the DD pack) follows with
// #105/#108.
// The stack needs the demo dataset with its personas and trained models (ci.yml e2e job).
const USERNAME = process.env.E2E_USERNAME ?? 'admin';
const PASSWORD = process.env.E2E_PASSWORD ?? 'dev-only-admin-pw';

/** Kamala's answer times, from `sim/config/personas.yaml` S1 (`after_s`). */
const PURPOSE_AFTER_MS = 6_000;
const SPELL_BREAKER_AFTER_MS = 11_000;
const STEP_UP_AFTER_MS = 2_000;
/** Staging the story is a worker job; each probe waits on the planner. */
const STAGED_WITHIN_MS = 60_000;
const DECIDED_WITHIN_MS = 90_000;
/** Kamala reads Telugu: an APPROVED `te` spell-breaker if the KB has one, else English (#4). */
const KAMALA_LANGUAGE = 'te';

/** One app switch (docs/08 §4 `visibility_hidden_count`): the page hides, then comes back. */
async function switchAppAndBack(page: Page): Promise<void> {
  for (const state of ['hidden', 'visible']) {
    await page.evaluate((s) => {
      Object.defineProperty(document, 'visibilityState', { value: s, configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
    }, state);
  }
}

test('E2E-S1 (Guardian): Kamala answers as scripted and is protected', async ({
  page,
  baseURL,
}) => {
  test.setTimeout(4 * 60_000);
  await page.goto('/');
  await page.getByLabel('Username').fill(USERNAME);
  await page.getByLabel('Password').fill(PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
  // From the seed: earlier journeys' S-1 runs change her history (payee saved, 24 h counts),
  // and with it the prior and the path (#291).
  await resetToSeed(page, baseURL);
  const spellBreakerTexts = await approvedTexts(
    page.request,
    'SPELL_BREAKER_DIGITAL_ARREST',
    KAMALA_LANGUAGE,
  );

  await page.goto('/phone?story=S1');
  await expect(page.getByText(/S-1 Kamala is staged/)).toBeVisible({ timeout: STAGED_WITHIN_MS });
  const around = page.getByRole('group', { name: 'Around the customer' });
  await expect(around.getByRole('button', { name: 'On a call, unknown number' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect(around.getByRole('button', { name: 'Natural' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );

  const phone = page.getByRole('region', { name: 'Customer’s phone' });
  const paid = page.waitForResponse(
    (r) => r.url().endsWith('/api/v1/guardian/sessions') && r.request().method() === 'POST',
  );
  await phone.getByRole('button', { name: /^Pay/ }).click();
  const { id } = (await (await paid).json()) as { id: string };

  const started = Date.now();
  const left = () => Math.max(DECIDED_WITHIN_MS - (Date.now() - started), 1);
  const protectedScreen = phone.getByRole('heading', { name: 'Hang up.' });
  const decided = phone.getByRole('button', { name: 'Done' });
  const purpose = phone.getByRole('button', { name: 'Family or a friend' });
  const no = phone.getByRole('button', { name: 'No', exact: true });
  const sensor = phone.getByRole('button', { name: 'Touch the sensor' });
  const otp = phone.getByLabel('One-time code');
  const pause = phone.getByRole('button', { name: 'Send now' });
  const answer = async (screen: Locator, afterMs: number, act: () => Promise<void>) => {
    // Each probe screen is keyed by its run: wait for this one to go, even when the next
    // probe looks the same (two spell-breakers in a row both have a "No").
    const shown = await screen.elementHandle();
    await page.waitForTimeout(afterMs);
    await act();
    await page.waitForFunction((el) => !el?.isConnected, shown, { timeout: left() });
  };
  let spellBreakers = 0;
  while (!(await decided.isVisible())) {
    await expect(decided.or(purpose).or(no).or(sensor).or(otp).or(pause)).toBeVisible({
      timeout: left(),
    });
    if (await purpose.isVisible()) {
      await answer(purpose, PURPOSE_AFTER_MS, () => purpose.click());
    } else if (await no.isVisible()) {
      const heading = (await phone.getByRole('heading', { level: 2 }).innerText())
        .replace(/\s+/g, ' ')
        .trim();
      expect(spellBreakerTexts, 'an APPROVED spell-breaker variant').toContain(heading);
      spellBreakers += 1;
      await answer(no, SPELL_BREAKER_AFTER_MS, async () => {
        await switchAppAndBack(page);
        await no.click();
      });
    } else if (await pause.isVisible()) {
      // A cooling-off runs ten real minutes (the server refuses faked timings): the live S-1
      // was meant to protect her before that (R-01, #291).
      throw new Error('S-1 reached the cooling-off instead of the protective screen (#291)');
    } else if (await sensor.isVisible()) {
      await answer(sensor, STEP_UP_AFTER_MS, () => sensor.click());
    } else if (await otp.isVisible()) {
      const sms = await phone.getByRole('status').filter({ hasText: 'one-time code' }).innerText();
      const code = /\b(\d{6})\b/.exec(sms)?.[1] ?? '';
      await answer(otp, STEP_UP_AFTER_MS, async () => {
        await otp.fill(code);
        await phone.getByRole('button', { name: 'Confirm' }).click();
      });
    }
  }
  await expect(protectedScreen).toBeVisible();
  expect(spellBreakers, 'the spell-breaker was asked (ENG-S1)').toBeGreaterThanOrEqual(1);

  const view = await page.request.get(`/api/v1/guardian/sessions/${id}`);
  expect(view.ok()).toBe(true);
  const session = (await view.json()) as {
    decision: string | null;
    probe_runs: { probe_key: string; mode: string }[];
  };
  expect(session.decision).toBe('PROTECTIVE_HOLD');
  // Every spell-breaker shown was the English one (counted above), and recorded.
  const shown = session.probe_runs.filter((r) => r.probe_key === 'SPELL_BREAKER_DIGITAL_ARREST');
  expect(shown).toHaveLength(spellBreakers);
});
