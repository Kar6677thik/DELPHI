import { defineConfig, devices } from '@playwright/test';

// End-to-end journeys (docs/21 §4) against a running stack: in CI, `docker compose up` from the
// repository root (the production image serves the built SPA on :8000). Locally, the same
// stack, or `uv run delphi api` with a built frontend.
export default defineConfig({
  testDir: 'e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:8000',
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
