import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

import { SCREENS } from '../src/routes/screens';
import { ADMIN, ADMIN_PASSWORD, signIn } from './session';

// E2E-A11Y (docs/21 §4, NFR-14): an axe scan of every route (docs/18 §2) finds no serious or
// critical violation. Routes with an id open with one that does not exist, so they show their
// not-found state; the admin sees every screen.
const SOME_ID = '00000000-0000-7000-8000-000000000001';
// WCAG 2.2 AA (NFR-14, docs/18 §5): axe's 2.0, 2.1 and 2.2 A and AA rules.
const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'];
// Every screen, the Theatre (outside the shell) and the not-found page.
const ROUTES = [
  '/login',
  '/',
  ...SCREENS.map((s) => s.path.replace(':id', SOME_ID)),
  '/theatre',
  '/no-such-page',
];

for (const route of ROUTES) {
  test(`axe finds nothing serious on ${route}`, async ({ page }) => {
    if (route !== '/login') await signIn(page, ADMIN, ADMIN_PASSWORD);
    await page.goto(route);
    // Not 'networkidle': the event stream (SSE) keeps a request open on every signed-in page.
    await expect(page.getByRole('heading').first()).toBeVisible();
    await expect(page.getByText(/^Loading/)).toHaveCount(0);
    const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(
      serious.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`),
    ).toEqual([]);
  });
}
