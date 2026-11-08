import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, vi } from 'vitest';

import { setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { FakeEventSource } from './fakeEventSource';

vi.stubGlobal('EventSource', FakeEventSource);

// Radix menus measure their trigger; jsdom has no ResizeObserver.
vi.stubGlobal(
  'ResizeObserver',
  class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  },
);

beforeAll(() => {
  server.listen({ onUnhandledFrame: 'error' });
});

afterEach(() => {
  cleanup();
  server.resetHandlers();
  setMockSession(null);
  FakeEventSource.instances = [];
});

afterAll(() => {
  server.close();
});
