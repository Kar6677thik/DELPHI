import { Outlet, type RouteObject } from 'react-router';

import { RequireSession } from '../components/RequireSession';
import { NotFoundPage } from './NotFoundPage';

export const routes: RouteObject[] = [
  {
    path: '/login',
    lazy: () => import('./LoginPage').then((m) => ({ Component: m.LoginPage })),
  },
  {
    element: (
      <RequireSession>
        <main id="main" className="min-h-screen bg-slate-50 p-6 text-slate-900">
          <Outlet />
        </main>
      </RequireSession>
    ),
    children: [
      {
        index: true,
        lazy: () => import('./OverviewPage').then((m) => ({ Component: m.OverviewPage })),
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
];
