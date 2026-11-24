import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';

import type { JobView, StatusView, UserView } from '../src/api/types';
import { MOCK_STATUS, problem, setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

/** The Admin screen (docs/18 §2 `/admin`, #231). */

const READY: StatusView = {
  ...MOCK_STATUS,
  models: {
    PRIOR: { version: 3, gate_passed: true },
    ANOMALY: { version: 2, gate_passed: true },
    MULE: { version: 2, gate_passed: false },
  },
  models_below_gate: ['MULE'],
};

function job(overrides: Partial<JobView> = {}): JobView {
  return {
    id: '01930000-0000-7000-8000-0000000000a1',
    type: 'TRAIN_SENSING',
    status: 'SUCCEEDED',
    progress: 1,
    result: null,
    error: null,
    attempts: 1,
    created_at: '2026-11-22T10:00:00Z',
    finished_at: '2026-11-22T10:01:30Z',
    ...overrides,
  };
}

const FAILED = job({
  id: '01930000-0000-7000-8000-0000000000a2',
  type: 'GENERATE_DATASET',
  status: 'FAILED',
  progress: 0.4,
  attempts: 3,
  error: 'The worker stopped mid-batch.',
  finished_at: null,
});

function user(overrides: Partial<UserView> = {}): UserView {
  return {
    id: '00000000-0000-4000-8000-000000000002',
    username: 'priya.investigator',
    display_name: 'Priya Investigator',
    role: 'INVESTIGATOR',
    disabled: false,
    created_at: '2026-11-08T09:00:00Z',
    version: 3,
    ...overrides,
  };
}

const ADA = user({
  id: '00000000-0000-4000-8000-000000000004',
  username: 'admin',
  display_name: 'Ada Admin',
  role: 'ADMIN',
  version: 1,
});

beforeEach(() => {
  server.use(http.get('/api/v1/admin/users', () => HttpResponse.json([ADA, user()])));
});

describe('/admin', () => {
  it('is for admins only', async () => {
    setMockSession('investigator');
    renderApp('/admin');
    expect(await screen.findByRole('heading', { name: 'No access' })).toBeVisible();
  });

  it('shows readiness, a model below its gate, and the jobs', async () => {
    setMockSession('admin');
    server.use(
      http.get('/api/v1/status', () => HttpResponse.json(READY)),
      http.get('/api/v1/jobs', () =>
        HttpResponse.json({ items: [FAILED, job()], next_cursor: null }),
      ),
    );
    renderApp('/admin');
    expect(await screen.findByText(/a model below its quality gate/)).toBeVisible();
    const strip = screen.getByLabelText('Readiness');
    expect(within(strip).getByText('below its gate')).toBeVisible();
    expect(within(strip).getAllByText('gate passed')).toHaveLength(2);

    const table = await screen.findByRole('table', { name: 'Jobs, newest first' });
    expect(within(table).getByText('The worker stopped mid-batch.')).toBeVisible();
    expect(within(table).getByText('1 m 30 s')).toBeVisible();
    expect(within(table).getAllByRole('button')).toHaveLength(1);
  });

  it('filters the jobs and retries a failed one', async () => {
    setMockSession('admin');
    const asked: string[] = [];
    let retried = false;
    server.use(
      http.get('/api/v1/jobs', ({ request }) => {
        asked.push(new URL(request.url).search);
        return HttpResponse.json({
          items: [retried ? { ...FAILED, status: 'QUEUED', error: null } : FAILED],
          next_cursor: null,
        });
      }),
      http.post('/api/v1/jobs/:id/retry', () => {
        retried = true;
        return HttpResponse.json({ ...FAILED, status: 'QUEUED', error: null });
      }),
    );
    const { user } = renderApp('/admin');
    await user.selectOptions(await screen.findByLabelText('Status'), 'FAILED');
    expect(asked.at(-1)).toContain('status=FAILED');

    await user.click(await screen.findByRole('button', { name: 'Retry Generate dataset' }));
    const table = screen.getByRole('table', { name: 'Jobs, newest first' });
    expect(await within(table).findByText('Queued')).toBeVisible();
    expect(within(table).queryByRole('button')).not.toBeInTheDocument();
  });

  it('checks the audit chain on demand', async () => {
    setMockSession('admin');
    let checks = 0;
    server.use(
      http.get('/api/v1/jobs', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.get('/api/v1/admin/audit/verify', () => {
        checks += 1;
        return HttpResponse.json(
          checks === 1
            ? { ok: true, checked: 1204, first_bad_id: null }
            : { ok: false, checked: 311, first_bad_id: 312 },
        );
      }),
    );
    const { user } = renderApp('/admin');
    const card = await screen.findByRole('region', { name: 'Audit chain' });
    expect(within(card).getByText('Not checked')).toBeVisible();
    expect(checks).toBe(0);

    await user.click(within(card).getByRole('button', { name: 'Check the chain' }));
    expect(await within(card).findByText('Intact')).toBeVisible();
    expect(within(card).getByText('1,204')).toBeVisible();

    await user.click(within(card).getByRole('button', { name: 'Check again' }));
    expect(await within(card).findByText('Broken')).toBeVisible();
    expect(within(card).getByText('#312')).toBeVisible();
  });

  it('queues a dataset with an idempotency key, and shows a refusal', async () => {
    setMockSession('admin');
    const keys: (string | null)[] = [];
    const bodies: unknown[] = [];
    server.use(
      http.get('/api/v1/status', () =>
        HttpResponse.json({ ...MOCK_STATUS, dataset_ready: false, models_ready: false }),
      ),
      http.get('/api/v1/jobs', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.post('/api/v1/admin/datasets', async ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'));
        bodies.push(await request.json());
        return keys.length === 1
          ? HttpResponse.json(job({ type: 'GENERATE_DATASET', status: 'QUEUED' }), { status: 202 })
          : problem(
              409,
              'Conflict',
              'JOB_ALREADY_RUNNING',
              'A dataset is already being generated.',
              request.url,
            );
      }),
    );
    const { user } = renderApp('/admin');
    const card = await screen.findByRole('region', { name: 'Set up the demo' });
    expect(await screen.findByText(/no dataset yet/)).toBeVisible();
    expect(within(card).getByRole('button', { name: 'Retrain the models' })).toBeDisabled();

    await user.selectOptions(within(card).getByLabelText('Profile'), 'eval');
    await user.click(within(card).getByRole('button', { name: 'Generate the dataset' }));
    expect(await within(card).findByText(/Queued: Generate dataset/)).toBeVisible();
    expect(bodies[0]).toEqual({ profile: 'eval' });
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/);

    await user.click(within(card).getByRole('button', { name: 'Generate the dataset' }));
    expect(await within(card).findByText('A dataset is already being generated.')).toBeVisible();
    expect(keys[1]).not.toBe(keys[0]);
  });
});

describe('/admin demo stories', () => {
  it('plays a story on autoplay, and sends the other mode to the phone', async () => {
    setMockSession('admin');
    const sent: { body?: unknown; key?: string | null } = {};
    server.use(
      http.get('/api/v1/jobs', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.post('/api/v1/admin/demo/scenarios/S2/run', async ({ request }) => {
        sent.body = await request.json();
        sent.key = request.headers.get('Idempotency-Key');
        return HttpResponse.json(job({ type: 'DEMO_STORY', status: 'QUEUED' }), { status: 202 });
      }),
    );
    const { user } = renderApp('/admin');
    const card = await screen.findByRole('region', { name: 'Demo stories' });
    const arjun = within(card).getByText('S-2 Arjun').closest('li');
    if (!arjun) throw new Error('no S-2 row');
    await waitFor(() => {
      expect(within(arjun).getByRole('button', { name: 'Autoplay' })).toBeEnabled();
    });
    await user.click(within(arjun).getByRole('button', { name: 'Autoplay' }));
    expect(await within(card).findByText(/Queued: the story plays/)).toBeVisible();
    expect(sent.body).toEqual({ mode: 'autoplay' });
    expect(sent.key).toMatch(/^[0-9a-f-]{36}$/);
    expect(within(arjun).getByRole('link', { name: 'Play on the phone' })).toHaveAttribute(
      'href',
      '/phone?story=S2',
    );
  });

  it('waits for the dataset and the models', async () => {
    setMockSession('admin');
    server.use(
      http.get('/api/v1/status', () =>
        HttpResponse.json({ ...MOCK_STATUS, dataset_ready: false, models_ready: false }),
      ),
      http.get('/api/v1/jobs', () => HttpResponse.json({ items: [], next_cursor: null })),
    );
    renderApp('/admin');
    const card = await screen.findByRole('region', { name: 'Demo stories' });
    expect(within(card).getByText(/run once the dataset and the sensing models/)).toBeVisible();
    for (const button of within(card).getAllByRole('button', { name: 'Autoplay' })) {
      expect(button).toBeDisabled();
    }
  });
});

describe('/admin users', () => {
  it('lists the users, marks you, and never offers to disable you', async () => {
    setMockSession('admin');
    server.use(http.get('/api/v1/jobs', () => HttpResponse.json({ items: [], next_cursor: null })));
    renderApp('/admin');
    const table = await screen.findByRole('table', { name: 'Users' });
    const [, adaRow, priyaRow] = within(table).getAllByRole('row');
    if (!adaRow || !priyaRow) throw new Error('missing rows');
    expect(within(adaRow).getByText('you')).toBeVisible();
    expect(within(adaRow).queryByRole('button', { name: 'Disable' })).not.toBeInTheDocument();
    expect(within(priyaRow).getByRole('button', { name: 'Disable' })).toBeVisible();
    expect(screen.getByText(/2 accounts, 2 enabled/)).toBeVisible();
  });

  it('changes a role with the row’s version as If-Match, and shows a 412', async () => {
    setMockSession('admin');
    const sent: { ifMatch: string | null; body: unknown }[] = [];
    server.use(
      http.get('/api/v1/jobs', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.patch('/api/v1/admin/users/:id', async ({ request }) => {
        sent.push({ ifMatch: request.headers.get('If-Match'), body: await request.json() });
        return sent.length === 1
          ? HttpResponse.json(user({ role: 'FDE_ENGINEER', version: 4 }))
          : problem(
              412,
              'Precondition Failed',
              'VERSION_CONFLICT',
              'This user was changed by someone else; reload and retry.',
              request.url,
            );
      }),
    );
    const { user: u } = renderApp('/admin');
    const role = await screen.findByLabelText('Role of Priya Investigator');
    await u.selectOptions(role, 'FDE_ENGINEER');
    expect(sent[0]).toEqual({ ifMatch: '"3"', body: { role: 'FDE_ENGINEER' } });

    await u.click(screen.getByRole('button', { name: 'Disable' }));
    expect(
      await screen.findByText('This user was changed by someone else; reload and retry.'),
    ).toBeVisible();
    expect(sent[1]?.body).toEqual({ disabled: true });
  });

  it('resets a password only once it is long enough', async () => {
    setMockSession('admin');
    let body: unknown = null;
    server.use(
      http.get('/api/v1/jobs', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.patch('/api/v1/admin/users/:id', async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(user({ version: 4 }));
      }),
    );
    const { user: u } = renderApp('/admin');
    const table = await screen.findByRole('table', { name: 'Users' });
    const reset = within(table).getAllByRole('button', { name: 'Reset password' })[1];
    if (!reset) throw new Error('missing button');
    await u.click(reset);
    const field = screen.getByLabelText('New password for Priya Investigator');
    await u.type(field, 'short');
    expect(screen.getByText('At least 12 characters.')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Set the password' })).toBeDisabled();
    await u.type(field, '-but-now-long');
    await u.click(screen.getByRole('button', { name: 'Set the password' }));
    expect(body).toEqual({ password: 'short-but-now-long' });
    await waitForGone('New password for Priya Investigator');
  });

  it('adds a user, and shows a taken username', async () => {
    setMockSession('admin');
    const bodies: unknown[] = [];
    server.use(
      http.get('/api/v1/jobs', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.post('/api/v1/admin/users', async ({ request }) => {
        bodies.push(await request.json());
        return problem(
          422,
          'Unprocessable',
          'VALIDATION_FAILED',
          'That username is taken.',
          request.url,
        );
      }),
    );
    const { user: u } = renderApp('/admin');
    await u.click(await screen.findByRole('button', { name: 'Add a user' }));
    const form = screen.getByRole('form', { name: 'Add a user' });
    const add = within(form).getByRole('button', { name: 'Add the user' });
    expect(add).toBeDisabled();
    await u.type(within(form).getByLabelText('Username'), 'ravi.k');
    await u.type(within(form).getByLabelText('Display name'), 'Ravi Kumar');
    await u.type(within(form).getByLabelText('Password'), 'a-long-password');
    await u.selectOptions(within(form).getByLabelText('Role'), 'INVESTIGATOR');
    expect(within(form).getByText('Works cases and chases')).toBeVisible();
    await u.click(add);
    expect(await within(form).findByText('That username is taken.')).toBeVisible();
    expect(bodies[0]).toEqual({
      username: 'ravi.k',
      display_name: 'Ravi Kumar',
      password: 'a-long-password',
      role: 'INVESTIGATOR',
    });
  });
});

async function waitForGone(label: string) {
  await waitFor(() => {
    expect(screen.queryByLabelText(label)).not.toBeInTheDocument();
  });
}
