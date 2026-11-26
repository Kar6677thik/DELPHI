import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';

import type { ClusterView, DraftView } from '../src/api/types';
import { normalise, readSpec } from '../src/routes/TypologyDraftPage';
import { problem, setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

/** The Typology Scout and the draft editor (`/scout`, `/scout/drafts/:id`, docs/18 §2, #118). */

const DRAFT_ID = '01930000-0000-7000-8000-0000000000d1';

const CLUSTER: ClusterView = {
  id: '01930000-0000-7000-8000-0000000000c1',
  run_id: '01930000-0000-7000-8000-0000000000e1',
  size: 14,
  member_refs: [],
  distinguishing_features: {
    features: [
      {
        feature: 'payee_account_age_days',
        smd: -2.1,
        separates: false,
        cluster_mean: 3,
        reference_mean: 412.5,
      },
      { feature: 'is_new_payee', smd: null, separates: true, cluster_mean: 1, reference_mean: 0 },
    ],
    first_at: '2026-11-20T00:00:00Z',
    last_at: '2026-11-25T00:00:00Z',
  },
  outcome_profile: {},
  created_at: '2026-11-25T10:00:00Z',
  draft_id: DRAFT_ID,
  draft_status: 'CANDIDATE',
  suggested_name: 'New-payee collect scam',
};

function draft(overrides: Partial<DraftView> = {}): DraftView {
  return {
    id: DRAFT_ID,
    cluster_id: CLUSTER.id,
    status: 'DRAFT',
    revision: 2,
    spec: {
      name: 'New-payee collect scam',
      key: 'APP_COLLECT_NEW_PAYEE',
      description: 'Collect requests from payees added minutes before.',
      color: '#7C3AED',
      open_mass_share: 0.5,
      likelihoods: { CALL_STATUS: { probs: [0.25, 0.75], ess: 16 } },
    },
    editors: ['00000000-0000-4000-8000-000000000003'],
    last_edited_by: '00000000-0000-4000-8000-000000000003',
    backtest_report: null,
    backtested_revision: null,
    created_at: '2026-11-25T10:00:00Z',
    updated_at: '2026-11-25T11:00:00Z',
    version: 4,
    issues: [],
    ...overrides,
  };
}

const REPORT = {
  new_hypothesis: 'APP_COLLECT_NEW_PAYEE',
  sessions: 120,
  cases: 8,
  skipped: 1,
  attributed: 11,
  attributed_labelled: 6,
  attributed_labelled_precision: 0.8333,
  genuine_disturbed: 0,
  decision_changes: { 'RELEASE→RELEASE': 100, 'HOLD_FOR_REVIEW→PROTECTIVE_HOLD': 3 },
  unexplained_rate: { before: 0.12, after: 0.04 },
  limitations: ['A replay cannot run probes that were never run.'],
};

interface Seen {
  patches: { ifMatch: string | null; body: unknown }[];
  runs: number;
  backtests: number;
}

function serve(current: DraftView = draft()): Seen {
  const seen: Seen = { patches: [], runs: 0, backtests: 0 };
  server.use(
    http.get('/api/v1/scout/queue', () =>
      HttpResponse.json({ items: Array.from({ length: 3 }, () => ({})), next_cursor: null }),
    ),
    http.get('/api/v1/scout/clusters', () =>
      HttpResponse.json({ items: [CLUSTER], next_cursor: null }),
    ),
    http.post('/api/v1/scout/runs', () => {
      seen.runs += 1;
      return HttpResponse.json({ job_id: crypto.randomUUID(), created: true }, { status: 202 });
    }),
    http.get('/api/v1/typology-drafts/:id', () => HttpResponse.json(current)),
    http.patch('/api/v1/typology-drafts/:id', async ({ request }) => {
      const body = await request.json();
      seen.patches.push({ ifMatch: request.headers.get('If-Match'), body });
      return HttpResponse.json({ ...current, revision: current.revision + 1 });
    }),
    http.post('/api/v1/typology-drafts/:id/backtest', () => {
      seen.backtests += 1;
      return HttpResponse.json({ job_id: crypto.randomUUID(), created: true }, { status: 202 });
    }),
    http.get('/api/v1/kb/versions', () =>
      HttpResponse.json({
        items: [
          {
            id: 1,
            status: 'ACTIVE',
            parent_id: null,
            change_summary: 'Seed.',
            created_by: null,
            created_at: '2026-11-08T00:00:00Z',
            activated_at: null,
            content_sha256: 'abc',
          },
        ],
        next_cursor: null,
      }),
    ),
    http.get('/api/v1/kb/versions/:id', () =>
      HttpResponse.json({
        id: 1,
        status: 'ACTIVE',
        parent_id: null,
        change_summary: 'Seed.',
        created_by: null,
        created_at: '2026-11-08T00:00:00Z',
        activated_at: null,
        content_sha256: 'abc',
        content: {
          hypotheses: {},
          sources: {
            CALL_STATUS: {
              kind: 'PROBE',
              mode: 'PASSIVE',
              friction_tier: 'NONE',
              outcomes: ['NO_CALL', 'ON_CALL_UNKNOWN_NUMBER'],
              description: '',
              question: false,
            },
          },
          source_order: ['CALL_STATUS'],
        },
      }),
    ),
  );
  return seen;
}

describe('the Typology Scout', () => {
  beforeEach(() => {
    setMockSession('fde');
  });

  it('shows the queue, the clusters with what sets them apart, and their drafts', async () => {
    const seen = serve();
    const { user } = renderApp('/scout');
    const waiting = await screen.findByText('Unexplained items waiting');
    await waitFor(() => {
      expect(waiting.nextElementSibling).toHaveTextContent(/^3$/);
    });
    const table = await screen.findByRole('table', { name: 'Clusters, newest first' });
    const row = within(table).getByRole('row', { name: /New-payee collect scam/ });
    expect(within(row).getByText(/Payee account age days/)).toBeVisible();
    expect(within(row).getByText('412.50')).toBeVisible();
    expect(within(row).getByRole('link', { name: /Candidate/ })).toHaveAttribute(
      'href',
      `/scout/drafts/${DRAFT_ID}`,
    );
    await user.click(screen.getByRole('button', { name: 'Run the Scout now' }));
    expect(await screen.findByText(/Run started/)).toBeVisible();
    expect(seen.runs).toBe(1);
  });

  it('lets a viewer read it without the run button', async () => {
    setMockSession('viewer');
    serve();
    renderApp('/scout');
    expect(await screen.findByRole('table', { name: 'Clusters, newest first' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Run the Scout now' })).toBeNull();
  });
});

describe('the typology draft editor', () => {
  beforeEach(() => {
    setMockSession('fde');
  });

  it('normalises the weights live and saves the normalised row with the ETag', async () => {
    const seen = serve();
    const { user } = renderApp(`/scout/drafts/${DRAFT_ID}`);
    const row = await screen.findByRole('group', { name: 'Call status' });
    const onCall = await within(row).findByLabelText('On call unknown number');
    await user.clear(onCall);
    await user.type(onCall, '3');
    const noCall = within(row).getByLabelText('No call');
    await user.clear(noCall);
    await user.type(noCall, '1');
    expect(within(row).getByText('75%')).toBeVisible();
    await user.selectOptions(within(row).getByLabelText('Confidence'), '50');
    await user.click(screen.getByRole('button', { name: 'Save as revision 3' }));
    expect(await screen.findByText('Saved as revision 3.')).toBeVisible();
    expect(seen.patches).toHaveLength(1);
    expect(seen.patches[0]?.ifMatch).toBe('"4"');
    expect(seen.patches[0]?.body).toMatchObject({
      key: 'APP_COLLECT_NEW_PAYEE',
      likelihoods: { CALL_STATUS: { probs: [0.25, 0.75], ess: 50 } },
    });
  });

  it('shows the KB rules a save breaks', async () => {
    serve();
    server.use(
      http.patch('/api/v1/typology-drafts/:id', ({ request }) =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Unprocessable Entity',
            status: 422,
            detail: 'The draft breaks the KB rules.',
            code: 'VALIDATION_FAILED',
            instance: new URL(request.url).pathname,
            request_id: crypto.randomUUID(),
            errors: [
              {
                path: 'hypotheses.APP_COLLECT_NEW_PAYEE',
                code: 'OPEN_MASS',
                message: 'open_mass_share exceeds 0.9',
              },
            ],
          },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    );
    const { user } = renderApp(`/scout/drafts/${DRAFT_ID}`);
    await user.click(await screen.findByRole('button', { name: 'Save as revision 3' }));
    expect(await screen.findByText('What the KB rules refuse')).toBeVisible();
    expect(screen.getByText('open_mass_share exceeds 0.9')).toBeVisible();
  });

  it('starts a backtest and shows its report, flagged stale after an edit', async () => {
    const seen = serve(draft({ backtest_report: REPORT, backtested_revision: 1 }));
    const { user } = renderApp(`/scout/drafts/${DRAFT_ID}`);
    const section = await screen.findByRole('region', { name: 'Backtest' });
    expect(within(section).getByText(/report is for revision 1/)).toBeVisible();
    expect(within(section).getByText('11')).toBeVisible();
    expect(within(section).getByText('83% of 6')).toBeVisible();
    expect(within(section).getByText('12% → 4%')).toBeVisible();
    expect(within(section).getByText('Hold for review → Protective hold')).toBeVisible();
    await user.click(within(section).getByRole('button', { name: 'Backtest revision 2' }));
    expect(await within(section).findByText(/Backtest started/)).toBeVisible();
    expect(seen.backtests).toBe(1);
  });

  it('cannot edit or backtest a draft with KB issues or past review', async () => {
    serve(
      draft({
        status: 'CANDIDATE',
        issues: [{ path: 'hypotheses', code: 'KEY', message: 'A key is required.' }],
      }),
    );
    renderApp(`/scout/drafts/${DRAFT_ID}`);
    expect(await screen.findByText('A key is required.')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Backtest revision 2' })).toBeDisabled();
  });

  it('shows a missing draft as not found', async () => {
    serve();
    server.use(
      http.get('/api/v1/typology-drafts/:id', ({ request }) =>
        problem(404, 'Not Found', 'NOT_FOUND', 'No typology draft.', request.url),
      ),
    );
    renderApp(`/scout/drafts/${DRAFT_ID}`);
    expect(await screen.findByRole('alert')).toHaveTextContent('No typology draft.');
  });
});

describe('the editor helpers', () => {
  it('normalises weights, and all-zero weights to a uniform row', () => {
    expect(normalise([1, 3])).toEqual([0.25, 0.75]);
    expect(normalise([0, 0])).toEqual([0.5, 0.5]);
    expect(normalise([-1, 2])).toEqual([0, 1]);
  });

  it('reads a stored spec leniently', () => {
    const spec = readSpec({ name: 'X', likelihoods: { A: { probs: [1], ess: 2 }, B: 'junk' } });
    expect(spec.likelihoods).toEqual({ A: { probs: [1], ess: 2 } });
    expect(spec.open_mass_share).toBe(0.5);
  });
});
