import type { Permission } from '../api/pending-contracts';

export interface Screen {
  path: string;
  title: string;
  /** Needed to open the screen (docs/18 §2, docs/19 §3). Write controls are gated separately. */
  permission: Permission;
  /** The navigation label, for screens with an entry in the side navigation. */
  nav?: string;
}

/** Every route in docs/18 §2 except /login and the Overview, in navigation order. */
export const SCREENS: readonly Screen[] = [
  { path: '/phone', title: 'Mock phone', permission: 'demo:drive', nav: 'Phone' },
  { path: '/guardian', title: 'Guardian monitor', permission: 'view', nav: 'Guardian' },
  { path: '/guardian/:id', title: 'Session detail', permission: 'view' },
  { path: '/cases', title: 'Case queue', permission: 'view', nav: 'Cases' },
  { path: '/cases/:id', title: 'Navigator console', permission: 'view' },
  { path: '/cases/:id/dd-pack', title: 'DD pack', permission: 'view' },
  { path: '/campaigns', title: 'Campaigns', permission: 'view', nav: 'Campaigns' },
  { path: '/campaigns/:id', title: 'Campaign', permission: 'view' },
  { path: '/chase/:id', title: 'Chase', permission: 'view' },
  { path: '/scout', title: 'Typology Scout', permission: 'view', nav: 'Scout' },
  { path: '/scout/drafts/:id', title: 'Typology draft editor', permission: 'view' },
  { path: '/kb', title: 'KB versions', permission: 'view', nav: 'KB' },
  { path: '/kb/questions', title: 'Question bank review', permission: 'view' },
  { path: '/learning', title: 'Learning', permission: 'view', nav: 'Learning' },
  { path: '/evaluation', title: 'Evaluation', permission: 'view', nav: 'Evaluation' },
  { path: '/admin', title: 'Admin', permission: 'admin', nav: 'Admin' },
];
