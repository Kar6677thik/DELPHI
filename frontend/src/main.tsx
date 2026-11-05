import './index.css';

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter } from 'react-router';

import { App } from './App';
import { createQueryClient } from './lib/queryClient';
import { routes } from './routes/router';

/** `pnpm dev:mock` answers the API from src/mocks (no backend needed). */
async function startMockApi(): Promise<void> {
  if (import.meta.env.MODE !== 'mock') return;
  const { worker } = await import('./mocks/browser');
  await worker.start({ onUnhandledFrame: 'bypass' });
}

void startMockApi().then(() => {
  const root = document.getElementById('root');
  if (!root) throw new Error('index.html has no #root element');
  createRoot(root).render(
    <StrictMode>
      <App router={createBrowserRouter(routes)} queryClient={createQueryClient()} />
    </StrictMode>,
  );
});
