import { act, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import type { CaseDetail, KbVersionView } from '../src/api/types';
import { FakeEventSource } from './fakeEventSource';
import {
  MOCK_KB_CONTENT,
  mockCaseDetail,
  mockEvidence,
  problem,
  setMockSession,
} from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

const CASE_ID = '01930000-0000-7000-8000-000000000001';
const CASE_URL = `/api/v1/cases/${CASE_ID}`;

const KB: KbVersionView = {
  id: 1,
  status: 'ACTIVE',
  parent_id: null,
  content_sha256: 'abc',
  change_summary: 'KB v1',
  created_by: null,
  created_at: '2026-11-01T00:00:00Z',
  activated_at: '2026-11-01T00:00:00Z',
  approved_by: null,
  retired_at: null,
  rollback_reason: null,
  content: MOCK_KB_CONTENT,
};

function serve(detail: CaseDetail | (() => CaseDetail)) {
  server.use(
    http.get('/api/v1/kb/versions/1', () => HttpResponse.json(KB)),
    http.get(CASE_URL, () => {
      const body = typeof detail === 'function' ? detail() : detail;
      return HttpResponse.json(body, { headers: { ETag: `"${String(body.version)}"` } });
    }),
  );
}

async function openConsole(user = 'investigator') {
  setMockSession(user);
  const app = renderApp(`/cases/${CASE_ID}`);
  expect(await screen.findByRole('heading', { name: 'Navigator console' })).toBeInTheDocument();
  await screen.findByRole('heading', { name: 'Hypotheses and recommendation' });
  return app;
}

describe('Navigator console (docs/09 §2)', () => {
  it('shows a loading state, then the case (docs/18 §4)', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get('/api/v1/kb/versions/1', () => HttpResponse.json(KB)),
      http.get(CASE_URL, async () => {
        await gate;
        return HttpResponse.json(mockCaseDetail(), { headers: { ETag: '"1"' } });
      }),
    );
    setMockSession('viewer');
    renderApp(`/cases/${CASE_ID}`);
    expect(await screen.findByText('Loading the case…')).toBeInTheDocument();
    release();
    expect(
      await screen.findByRole('heading', { name: 'Hypotheses and recommendation' }),
    ).toBeInTheDocument();
    expect(screen.queryByText('Loading the case…')).not.toBeInTheDocument();
  });

  it('shows the problem when the case cannot load (docs/18 §4)', async () => {
    server.use(
      http.get(CASE_URL, ({ request }) =>
        problem(404, 'Not Found', 'NOT_FOUND', 'No case with that id.', request.url),
      ),
    );
    setMockSession('viewer');
    renderApp(`/cases/${CASE_ID}`);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load the case');
    expect(alert).toHaveTextContent('NOT_FOUND');
  });

  it('shows a disposed case as decided, with nothing left to fetch', async () => {
    serve(
      mockCaseDetail({
        status: 'DISPOSED',
        disposition: 'CONFIRM_FRAUD',
        evidence_sufficient: true,
        next_best_evidence: [],
      }),
    );
    await openConsole();
    const taken = await screen.findByRole('row', { current: true });
    expect(taken).toHaveTextContent('Confirm fraud(decision taken)');
    expect(screen.queryByText(/Evidence sufficient/)).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Next best evidence' })).not.toBeInTheDocument();
  });

  it('shows the "evidence sufficient" banner when the planner says STOP (FR-NAV-03)', async () => {
    serve(mockCaseDetail({ evidence_sufficient: true }));
    await openConsole();
    const banner = (await screen.findByText(/Evidence sufficient/)).closest('[role="status"]');
    expect(banner).toHaveTextContent(
      'Evidence sufficient — recommend Confirm fraud (expected loss ₹12,000.00)',
    );
  });

  it('has no banner while more evidence is worth fetching', async () => {
    serve(mockCaseDetail());
    await openConsole();
    await screen.findByRole('heading', { name: 'Next best evidence' });
    expect(screen.queryByText(/Evidence sufficient/)).not.toBeInTheDocument();
  });

  it('ranks the next best evidence with the protocol row pinned and badged', async () => {
    serve(mockCaseDetail());
    await openConsole();
    const table = within(await screen.findByRole('table', { name: /Evidence to fetch next/ }));
    const rows = table.getAllByRole('row').slice(1);
    expect(rows[0]).toHaveTextContent('Customer outreach call');
    expect(rows[0]).toHaveTextContent('Protocol');
    expect(rows[0]).toHaveTextContent('42.5% of remaining uncertainty');
    expect(rows[0]).toHaveTextContent('₹2,500.00');
    expect(rows[1]).toHaveTextContent('Pass through dwell');
    expect(rows[1]).not.toHaveTextContent('Protocol');
  });

  it('says what would change its mind (docs/06 §12)', async () => {
    serve(
      mockCaseDetail({
        what_would_change_my_mind: {
          source_key: 'CUSTOMER_OUTREACH_CALL',
          outcome: 'CONFIRMS_GENUINE_PURPOSE',
          probability: 0.12,
          new_decision: 'RELEASE',
          risk_change_paise: -500_000,
        },
      }),
    );
    await openConsole();
    const section = await screen.findByRole('region', { name: 'What would change my mind' });
    expect(section).toHaveTextContent(
      'If Customer outreach call came back Confirms genuine purpose (chance 12%), the recommendation would become Release',
    );
  });

  it('refreshes the case when its narrative is written (SSE evidence.narrative_ready)', async () => {
    let calls = 0;
    const record = mockEvidence();
    serve(() => {
      calls += 1;
      return mockCaseDetail({
        evidence_records: [{ ...record, narrative: calls === 1 ? null : record.narrative }],
      });
    });
    await openConsole();
    expect(await screen.findByText('The summary is being written…')).toBeInTheDocument();
    act(() => {
      FakeEventSource.latest().emit('evidence.narrative_ready', '9');
    });
    expect(await screen.findByText('Template summary')).toBeInTheDocument();
  });
});
