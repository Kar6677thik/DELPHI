import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The api serves the built app at / and the API at /api/v1 on the same origin (docs/18,
// ADR-0008). In development, Vite proxies /api to `uv run delphi api`, or to the compose
// stack's `api` service (DELPHI_API_ORIGIN, compose.yaml).
const API_ORIGIN = process.env.DELPHI_API_ORIGIN ?? 'http://localhost:8000';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // changeOrigin off: the api sees the browser's own Host and Origin (localhost:5173), so
      // its CSRF check (docs/16 §1: Origin must equal the request's origin when no
      // DELPHI_PUBLIC_ORIGIN is set) passes for the dev server's requests and no other's.
      '/api': { target: API_ORIGIN, changeOrigin: false },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['tests/setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/api/schema.d.ts', 'src/main.tsx'],
      // docs/21 §6: frontend components ≥ 70%.
      thresholds: {
        'src/components/**': { statements: 70, branches: 70, functions: 70, lines: 70 },
      },
    },
  },
});
