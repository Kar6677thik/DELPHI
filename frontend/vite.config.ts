import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The api serves the built app at / and the API at /api/v1 on the same origin (docs/18,
// ADR-0008). In development, Vite proxies /api to `uv run delphi api`.
const API_ORIGIN = 'http://localhost:8000';

export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss()],
  // `pnpm dev:mock` serves the MSW worker from mock-public/, so it never ships in dist.
  publicDir: mode === 'mock' ? 'mock-public' : 'public',
  server: mode === 'mock' ? {} : { proxy: { '/api': API_ORIGIN } },
  test: {
    environment: 'jsdom',
    setupFiles: ['tests/setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/api/schema.d.ts', 'src/mocks/browser.ts', 'src/main.tsx'],
      // docs/21 §6: frontend components ≥ 70%.
      thresholds: {
        'src/components/**': { statements: 70, branches: 70, functions: 70, lines: 70 },
      },
    },
  },
}));
