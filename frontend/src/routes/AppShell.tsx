import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router';

import { DelphiMark } from '../components/DelphiMark';
import { RoleGate } from '../components/RoleGate';
import { SseIndicator } from '../components/SseIndicator';
import { UserMenu } from '../components/UserMenu';
import { EVENT_STREAM_URL } from '../lib/config';
import { invalidateFor } from '../lib/liveQueries';
import { useSession } from '../lib/session';
import { useEventStream } from '../lib/useEventStream';
import { SCREENS } from './screens';

const NAV_ITEMS = [
  { path: '/', label: 'Overview', permission: 'view' as const },
  { path: '/theatre', label: 'Theatre', permission: 'view' as const },
  ...SCREENS.flatMap((s) =>
    s.nav ? [{ path: s.path, label: s.nav, permission: s.permission }] : [],
  ),
];

function navClass({ isActive }: { isActive: boolean }): string {
  // The style's one selection mark: a 1px accent line exactly as wide as the label.
  const base =
    'inline-block py-1.5 text-[14px] leading-5 transition-colors duration-150 focus-visible:outline-1 focus-visible:outline-offset-4 focus-visible:outline-accent';
  return isActive ? `${base} text-ink selected-line` : `${base} text-muted hover:text-ink`;
}

/** The deployment's scope, shown under the product's name (docs/18 §3). */
const SCOPE = 'Demo bank · synthetic data';

/** The signed-in layout: the product and its scope, role-aware navigation, live status. */
export function AppShell() {
  const me = useSession();
  const queryClient = useQueryClient();
  // The phone menu is open on the page it was opened on, so navigating closes it.
  const { pathname } = useLocation();
  const [menuOpenOn, setMenuOpenOn] = useState<string | null>(null);
  const menuOpen = menuOpenOn === pathname;
  const onEvent = useCallback(
    (type: string) => {
      invalidateFor(queryClient, type);
    },
    [queryClient],
  );
  const streamStatus = useEventStream(EVENT_STREAM_URL, onEvent);

  const nav = (
    <nav aria-label="Main">
      <ul className="space-y-0.5">
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
  );
  const footer = (
    <div className="space-y-3 text-[13px]">
      <SseIndicator status={streamStatus} />
      {me && <UserMenu me={me} />}
    </div>
  );

  return (
    <div className="min-h-screen bg-ground text-ink">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2"
      >
        Skip to main content
      </a>
      {/* Phone: a 48px bar with the product and a menu button; the nav opens as a sheet. */}
      <header className="sticky top-0 z-30 flex h-12 items-center gap-3 border-b border-line bg-ground px-4 md:hidden print:hidden">
        <Brand />
        <button
          type="button"
          className="ml-auto h-11 rounded-full px-4 text-[14px] text-soft hover:text-ink"
          aria-expanded={menuOpen}
          aria-controls="phone-menu"
          onClick={() => {
            setMenuOpenOn(menuOpen ? null : pathname);
          }}
        >
          {menuOpen ? 'Close' : 'Menu'}
        </button>
      </header>
      {menuOpen && (
        <div
          id="phone-menu"
          className="fixed inset-x-0 bottom-0 top-12 z-20 overflow-y-auto bg-ground p-6 md:hidden"
        >
          {nav}
          <div className="mt-8">{footer}</div>
        </div>
      )}
      <div className="flex">
        <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-line px-6 py-7 md:flex print:hidden">
          <Brand />
          <p className="mt-1 pl-[30px] text-[12px] text-muted">{SCOPE}</p>
          <div className="mt-9 flex-1 overflow-y-auto">{nav}</div>
          {footer}
        </aside>
        <main id="main" className="min-w-0 flex-1 px-4 py-6 md:px-8 md:py-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function Brand() {
  return (
    <span className="flex items-center gap-2">
      <DelphiMark />
      <span className="text-[16px] font-semibold tracking-tight">DELPHI</span>
    </span>
  );
}
