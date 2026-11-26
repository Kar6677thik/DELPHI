import type { ComponentType, ReactNode } from 'react';
import type { RouteObject } from 'react-router';

import { RequireSession } from '../components/RequireSession';
import { RoleGate } from '../components/RoleGate';
import { AppShell } from './AppShell';
import { ForbiddenPage } from './ForbiddenPage';
import { NotFoundPage } from './NotFoundPage';
import { PlaceholderPage } from './PlaceholderPage';
import { SCREENS } from './screens';

/** Screens that are built, loaded on first visit; the others still show their placeholder. */
const PAGES: Partial<Record<string, () => Promise<ComponentType>>> = {
  '/cases': () => import('./CasesPage').then((m) => m.CasesPage),
  '/cases/:id': () => import('./CaseConsolePage').then((m) => m.CaseConsolePage),
  '/cases/:id/dd-pack': () => import('./CaseDdPackPage').then((m) => m.CaseDdPackPage),
  '/phone': () => import('./PhonePage').then((m) => m.PhonePage),
  '/guardian': () => import('./GuardianPage').then((m) => m.GuardianPage),
  '/guardian/:id': () => import('./GuardianSessionPage').then((m) => m.GuardianSessionPage),
  '/scout': () => import('./ScoutPage').then((m) => m.ScoutPage),
  '/scout/drafts/:id': () => import('./TypologyDraftPage').then((m) => m.TypologyDraftPage),
  '/kb': () => import('./KbPage').then((m) => m.KbPage),
  '/admin': () => import('./AdminPage').then((m) => m.AdminPage),
};

export const routes: RouteObject[] = [
  {
    path: '/login',
    lazy: () => import('./LoginPage').then((m) => ({ Component: m.LoginPage })),
  },
  {
    // The Theatre is full-screen for the projector: signed in, but outside the app shell.
    path: '/theatre',
    lazy: () =>
      import('./TheatrePage').then((m) => ({
        element: (
          <RequireSession>
            <m.TheatrePage />
          </RequireSession>
        ),
      })),
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
      ...SCREENS.map((screen): RouteObject => {
        const gate = (page: ReactNode) => (
          <RoleGate permission={screen.permission} fallback={<ForbiddenPage />}>
            {page}
          </RoleGate>
        );
        const load = PAGES[screen.path];
        return load
          ? { path: screen.path, lazy: () => load().then((Page) => ({ element: gate(<Page />) })) }
          : {
              path: screen.path,
              element: gate(<PlaceholderPage title={screen.title} path={screen.path} />),
            };
      }),
      { path: '*', element: <NotFoundPage /> },
    ],
  },
];
