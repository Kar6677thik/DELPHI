import type { RouteObject } from 'react-router';

import { RequireSession } from '../components/RequireSession';
import { RoleGate } from '../components/RoleGate';
import { AppShell } from './AppShell';
import { ForbiddenPage } from './ForbiddenPage';
import { NotFoundPage } from './NotFoundPage';
import { PlaceholderPage } from './PlaceholderPage';
import { SCREENS } from './screens';

export const routes: RouteObject[] = [
  {
    path: '/login',
    lazy: () => import('./LoginPage').then((m) => ({ Component: m.LoginPage })),
  },
  {
    element: (
      <RequireSession>
        <AppShell />
      </RequireSession>
    ),
    children: [
      {
        index: true,
        lazy: () => import('./OverviewPage').then((m) => ({ Component: m.OverviewPage })),
      },
      ...SCREENS.map((screen) => ({
        path: screen.path,
        element: (
          <RoleGate permission={screen.permission} fallback={<ForbiddenPage />}>
            <PlaceholderPage title={screen.title} />
          </RoleGate>
        ),
      })),
      { path: '*', element: <NotFoundPage /> },
    ],
  },
];
