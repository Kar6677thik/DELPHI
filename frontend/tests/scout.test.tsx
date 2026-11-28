import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';

import type { ClusterView, DraftView } from '../src/api/types';
import {
  backtestMessage,
  confidencePresets,
  normalise,
  readSpec,
} from '../src/routes/TypologyDraftPage';
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
      return HttpResponse.json(
        { job_id: crypto.randomUUID(), created: true, status: 'QUEUED' },
        { status: 202 },
      );
    }),
    http.get('/api/v1/jobs/:id', ({ params }) =>
      HttpResponse.json({
        id: String(params.id),
        type: 'RUN_BACKTEST',
        status: 'RUNNING',
        progress: 0,
        result: null,
        error: null,
        attempts: 1,
        created_at: '2026-11-25T10:00:00Z',
        finished_at: null,
      }),
    ),
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
          config: {
            elicitation: { default_ess: 20, confidence_ess: { LOW: 12, MEDIUM: 24, HIGH: 60 } },
            scout: { lookback_days: 21 },
          },
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

  it('pages the clusters with the cursor, so an older draft stays reachable', async () => {
    serve();
    server.use(
      http.get('/api/v1/scout/clusters', ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor');
        return HttpResponse.json(
          cursor === 'older'
            ? {
                items: [{ ...CLUSTER, id: 'c-2', suggested_name: 'Older cluster' }],
                next_cursor: null,
              }
            : { items: [CLUSTER], next_cursor: 'older' },
        );
      }),
    );
    const { user } = renderApp('/scout');
    await screen.findByRole('row', { name: /New-payee collect scam/ });
    await user.click(screen.getByRole('button', { name: 'Load more' }));
    expect(await screen.findByRole('row', { name: /Older cluster/ })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
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
    // The confidence presets are the ACTIVE KB's `elicitation.confidence_ess`, not fixed numbers.
    await user.selectOptions(within(row).getByLabelText('Confidence'), '60');
    await user.click(screen.getByRole('button', { name: 'Save as revision 3' }));
    expect(await screen.findByText('Saved as revision 3.')).toBeVisible();
    expect(seen.patches).toHaveLength(1);
    expect(seen.patches[0]?.ifMatch).toBe('"4"');
    expect(seen.patches[0]?.body).toMatchObject({
      key: 'APP_COLLECT_NEW_PAYEE',
      likelihoods: { CALL_STATUS: { probs: [0.25, 0.75], ess: 60 } },
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

  it('keeps unsaved edits when someone saves a new revision, and offers a reload', async () => {
    let current = draft();
    serve(current);
    server.use(http.get('/api/v1/typology-drafts/:id', () => HttpResponse.json(current)));
    const { user, queryClient } = renderApp(`/scout/drafts/${DRAFT_ID}`);
    const name = await screen.findByLabelText('Display name');
    await user.clear(name);
    await user.type(name, 'My edit');
    // Someone's save bumps the revision (and the version) on the server.
    current = draft({ version: 5, revision: 3, spec: { ...draft().spec, name: 'Server name' } });
    await queryClient.invalidateQueries();
    expect(await screen.findByText(/changed on the server/)).toBeVisible();
    expect(screen.getByLabelText('Display name')).toHaveValue('My edit');
    expect(screen.getByRole('button', { name: 'Save as revision 4' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: /Reload the draft/ }));
    expect(screen.getByLabelText('Display name')).toHaveValue('Server name');
  });

  it('keeps editing through a backtest, which bumps the version but not the revision', async () => {
    let current = draft();
    const seen = serve(current);
    server.use(http.get('/api/v1/typology-drafts/:id', () => HttpResponse.json(current)));
    const { user, queryClient } = renderApp(`/scout/drafts/${DRAFT_ID}`);
    const name = await screen.findByLabelText('Display name');
    await user.clear(name);
    await user.type(name, 'My edit');
    // The backtest's report lands: version 4 → 5, revision still 2.
    current = draft({
      version: 5,
      status: 'BACKTESTED',
      backtest_report: REPORT,
      backtested_revision: 2,
    });
    await queryClient.invalidateQueries();
    expect(await screen.findByText(/Queue items it now explains/)).toBeVisible();
    expect(screen.queryByText(/changed on the server/)).toBeNull();
    expect(screen.getByLabelText('Display name')).toHaveValue('My edit');
    await user.click(screen.getByRole('button', { name: 'Save as revision 3' }));
    await waitFor(() => {
      expect(seen.patches).toHaveLength(1);
    });
    expect(seen.patches[0]?.ifMatch).toBe('"5"'); // the server's version now, quietly
    expect(seen.patches[0]?.body).toMatchObject({ name: 'My edit' });
  });

  it('locks the form while a save is in flight, so nothing typed then is lost', async () => {
    serve();
    let release = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.patch('/api/v1/typology-drafts/:id', async () => {
        await held;
        return HttpResponse.json({ ...draft(), revision: 3, version: 5 });
      }),
    );
    const { user } = renderApp(`/scout/drafts/${DRAFT_ID}`);
    await user.type(await screen.findByLabelText('Display name'), '!');
    await user.click(screen.getByRole('button', { name: 'Save as revision 3' }));
    await waitFor(() => {
      expect(screen.getByLabelText('Display name')).toBeDisabled();
    });
    release();
    await waitFor(() => {
      expect(screen.getByLabelText('Display name')).toBeEnabled();
    });
  });

  it('takes a server-side bump quietly when nothing is unsaved', async () => {
    let current = draft();
    serve(current);
    server.use(http.get('/api/v1/typology-drafts/:id', () => HttpResponse.json(current)));
    const { queryClient } = renderApp(`/scout/drafts/${DRAFT_ID}`);
    expect(await screen.findByLabelText('Display name')).toHaveValue('New-payee collect scam');
    current = draft({ version: 5, spec: { ...draft().spec, name: 'After the backtest' } });
    await queryClient.invalidateQueries();
    await waitFor(() => {
      expect(screen.getByLabelText('Display name')).toHaveValue('After the backtest');
    });
    expect(screen.queryByText(/changed on the server/)).toBeNull();
  });

  it('explains a 412 and keeps the edits until the reload', async () => {
    serve();
    server.use(
      http.patch('/api/v1/typology-drafts/:id', ({ request }) =>
        problem(412, 'Precondition Failed', 'VERSION_CONFLICT', 'Stale ETag.', request.url),
      ),
    );
    const { user } = renderApp(`/scout/drafts/${DRAFT_ID}`);
    const name = await screen.findByLabelText('Display name');
    await user.type(name, '!');
    await user.click(screen.getByRole('button', { name: 'Save as revision 3' }));
    expect(await screen.findByText(/Someone saved this draft after you started/)).toBeVisible();
    expect(screen.getByLabelText('Display name')).toHaveValue('New-payee collect scam!');
  });

  it('makes the form read-only after a 409, and for a superseded draft', async () => {
    serve();
    server.use(
      http.patch('/api/v1/typology-drafts/:id', ({ request }) =>
        problem(409, 'Conflict', 'INVALID_STATE_TRANSITION', 'Not editable.', request.url),
      ),
    );
    const { user } = renderApp(`/scout/drafts/${DRAFT_ID}`);
    await user.click(await screen.findByRole('button', { name: 'Save as revision 3' }));
    expect(await screen.findByText(/can no longer be edited/)).toBeVisible();
    expect(screen.getByLabelText('Display name')).toBeDisabled();
  });

  it('says a superseded draft is kept for audit and is read-only', async () => {
    serve(draft({ status: 'SUPERSEDED' }));
    renderApp(`/scout/drafts/${DRAFT_ID}`);
    expect(await screen.findByText(/superseded this candidate/)).toBeVisible();
    expect(screen.getByLabelText('Display name')).toBeDisabled();
  });

  it('never sends a colour nobody chose', async () => {
    const bare = draft();
    const seen = serve(draft({ spec: { ...bare.spec, color: undefined } }));
    const { user } = renderApp(`/scout/drafts/${DRAFT_ID}`);
    await user.type(await screen.findByLabelText('Display name'), '!');
    await user.click(screen.getByRole('button', { name: 'Save as revision 3' }));
    await waitFor(() => {
      expect(seen.patches).toHaveLength(1);
    });
    expect(seen.patches[0]?.body).not.toHaveProperty('color');
  });

  it('sends a cleared open mass share as null, so the server default applies', async () => {
    const bare = draft();
    const seen = serve(draft({ spec: { ...bare.spec, open_mass_share: 0.3 } }));
    const { user } = renderApp(`/scout/drafts/${DRAFT_ID}`);
    const share = await screen.findByLabelText('Open mass share');
    expect(share).toHaveValue(0.3);
    await user.clear(share);
    expect(share).toHaveAttribute('placeholder', 'Server default');
    await user.click(screen.getByRole('button', { name: 'Save as revision 3' }));
    await waitFor(() => {
      expect(seen.patches).toHaveLength(1);
    });
    expect(seen.patches[0]?.body).toHaveProperty('open_mass_share', null);
  });

  it('shows a viewer the draft with its inputs disabled', async () => {
    setMockSession('viewer');
    serve();
    renderApp(`/scout/drafts/${DRAFT_ID}`);
    expect(await screen.findByLabelText('Display name')).toBeDisabled();
    expect(screen.queryByRole('button', { name: /Save as revision/ })).toBeNull();
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
    // Nothing stored: no default here (the server's applies), and no colour nobody picked.
    expect(spec.open_mass_share).toBeNull();
    expect(spec.color).toBeNull();
  });

  it('reads the confidence presets from the KB, Low to High', () => {
    expect(
      confidencePresets({ config: { elicitation: { confidence_ess: { HIGH: 50, LOW: 10 } } } }),
    ).toEqual([
      { label: 'Low', ess: 10 },
      { label: 'High', ess: 50 },
    ]);
    expect(confidencePresets(undefined)).toEqual([]);
  });

  it('tells a new backtest from one of the revision already', () => {
    expect(backtestMessage(true, 'QUEUED')).toMatch(/Backtest started/);
    expect(backtestMessage(false, 'QUEUED')).toMatch(/queued already/);
    expect(backtestMessage(false, 'RUNNING')).toMatch(/being backtested already/);
    expect(backtestMessage(false, 'SUCCEEDED')).toMatch(/report is below/);
    expect(backtestMessage(true, 'FAILED')).toMatch(/Asking again/);
  });
});
