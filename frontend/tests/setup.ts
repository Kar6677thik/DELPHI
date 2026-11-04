import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/react';
import { afterAll, afterEach, beforeAll } from 'vitest';

import { setMockSession } from '../src/mocks/handlers';
import { server } from '../src/mocks/node';

beforeAll(() => {
  server.listen({ onUnhandledFrame: 'error' });
});

afterEach(() => {
  cleanup();
  server.resetHandlers();
  setMockSession(null);
});

afterAll(() => {
  server.close();
});
