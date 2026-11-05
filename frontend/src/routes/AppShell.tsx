import { NavLink, Outlet } from 'react-router';

import { RoleGate } from '../components/RoleGate';
import { SseIndicator } from '../components/SseIndicator';
import { UserMenu } from '../components/UserMenu';
import { EVENT_STREAM_URL } from '../lib/config';
import { useSession } from '../lib/session';
import { useEventStream } from '../lib/useEventStream';
import { SCREENS } from './screens';

const NAV_ITEMS = [
  { path: '/', label: 'Overview', permission: 'view' as const },
  ...SCREENS.flatMap((s) =>
    s.nav ? [{ path: s.path, label: s.nav, permission: s.permission }] : [],
  ),
];

function navClass({ isActive }: { isActive: boolean }): string {
  const base =
    'block rounded-md px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-slate-900';
  return isActive ? `${base} bg-slate-900 text-white` : `${base} text-slate-700 hover:bg-slate-200`;
}

/** The signed-in layout: header with live status and user menu, role-aware navigation. */
export function AppShell() {
  const me = useSession();
  const streamStatus = useEventStream(EVENT_STREAM_URL);

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:rounded focus:bg-white focus:px-3 focus:py-2"
      >
        Skip to main content
      </a>
      <header className="flex items-center gap-4 border-b border-slate-200 bg-white px-4 py-2">
        <span className="text-lg font-semibold tracking-tight">DELPHI</span>
        <div className="ml-auto flex items-center gap-4">
          <SseIndicator status={streamStatus} />
          {me && <UserMenu me={me} />}
        </div>
      </header>
      <div className="flex">
        <nav aria-label="Main" className="w-48 shrink-0 border-r border-slate-200 p-3">
          <ul className="space-y-1">
            {NAV_ITEMS.map((item) => (
              <RoleGate key={item.path} permission={item.permission}>
                <li>
                  <NavLink to={item.path} end={item.path === '/'} className={navClass}>
                    {item.label}
                  </NavLink>
                </li>
              </RoleGate>
            ))}
          </ul>
        </nav>
        <main id="main" className="min-w-0 flex-1 p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
