import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter } from 'react-router';

import { App } from '../src/App';
import { createQueryClient } from '../src/lib/queryClient';
import { routes } from '../src/routes/router';

export function renderApp(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const queryClient = createQueryClient();
  const user = userEvent.setup();
  render(<App router={router} queryClient={queryClient} />);
  return { router, user, queryClient };
}
