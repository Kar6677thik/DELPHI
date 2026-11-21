import { act, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import type { CaseDetail, KbVersionView } from '../src/api/types';
import { caseChanges, typologies } from '../src/routes/DispositionPanel';
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
  it('shows the "evidence sufficient" banner when the planner says STOP (FR-NAV-03)', async () => {
    serve(mockCaseDetail({ evidence_sufficient: true }));
    await openConsole();
    const banner = (await screen.findByText(/Evidence sufficient/)).closest('[role="status"]');
    expect(banner).toHaveTextContent(
      'Evidence sufficient — recommend Confirm fraud (expected loss ₹12,000.00)',
    );
    // Fetching more is still allowed.
    expect(screen.getByRole('button', { name: 'Fetch' })).toBeEnabled();
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

  it('shows write controls disabled to a viewer (docs/18 §3 RoleGate)', async () => {
    serve(mockCaseDetail());
    await openConsole('viewer');
    expect(await screen.findByRole('button', { name: 'Fetch' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Record call' })).toBeDisabled();
    expect(screen.getByText('Only investigators decide cases.')).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'Decide the case' })).not.toBeInTheDocument();
  });

  it('fetches evidence with an idempotency key and shows its card', async () => {
    const sent: { body?: unknown; key?: string | null } = {};
    serve(mockCaseDetail());
    server.use(
      http.post(`${CASE_URL}/evidence-requests`, async ({ request }) => {
        sent.body = await request.json();
        sent.key = request.headers.get('Idempotency-Key');
        const after = mockCaseDetail({
          version: 2,
          evidence_records: [mockEvidence({ source_key: 'PASS_THROUGH_DWELL' })],
        });
        return HttpResponse.json(after, { headers: { ETag: '"2"' } });
      }),
    );
    const { user } = await openConsole();
    await user.click(await screen.findByRole('button', { name: 'Fetch' }));
    expect(await screen.findByRole('article', { name: /Pass through dwell/ })).toBeInTheDocument();
    expect(sent).toMatchObject({
      body: { source_key: 'PASS_THROUGH_DWELL', refresh: false, simulate: false },
    });
    expect(sent.key).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('records an outreach call with its outcome and note (docs/09 §3)', async () => {
    let body: unknown = null;
    serve(mockCaseDetail());
    server.use(
      http.post(`${CASE_URL}/evidence-requests`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(mockCaseDetail({ version: 2 }), { headers: { ETag: '"2"' } });
      }),
    );
    const { user } = await openConsole();
    await user.click(await screen.findByRole('button', { name: 'Record call' }));
    const form = within(screen.getByRole('form', { name: 'Record the outreach call' }));
    expect(form.getByRole('button', { name: 'Record' })).toBeDisabled();
    await user.click(form.getByRole('radio', { name: 'Confirms coercion' }));
    await user.type(form.getByRole('textbox', { name: 'Note' }), 'Caller on the line');
    await user.click(form.getByRole('button', { name: 'Record' }));
    await screen.findByRole('button', { name: 'Record call' });
    expect(
      screen.queryByRole('form', { name: 'Record the outreach call' }),
    ).not.toBeInTheDocument();
    expect(body).toEqual({
      source_key: 'CUSTOMER_OUTREACH_CALL',
      outcome: 'CONFIRMS_COERCION',
      note: 'Caller on the line',
      refresh: false,
      simulate: false,
    });
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

describe('disposition (docs/09 §2 item 6, docs/05 §4)', () => {
  it('sends the decision with If-Match and shows the disposed case', async () => {
    const sent: { body?: unknown; ifMatch?: string | null } = {};
    serve(mockCaseDetail({ version: 3 }));
    server.use(
      http.post(`${CASE_URL}/disposition`, async ({ request }) => {
        sent.body = await request.json();
        sent.ifMatch = request.headers.get('If-Match');
        return HttpResponse.json(
          mockCaseDetail({
            version: 4,
            status: 'DISPOSED',
            disposition: 'CONFIRM_FRAUD',
            disposition_typology: 'APP_DIGITAL_ARREST',
            disposition_confidence: 4,
          }),
          { headers: { ETag: '"4"' } },
        );
      }),
    );
    const { user } = await openConsole();
    const form = within(await screen.findByRole('form', { name: 'Decide the case' }));
    await user.click(form.getByRole('radio', { name: /Confirm fraud/ }));
    await user.selectOptions(
      form.getByRole('combobox', { name: /Typology/ }),
      'APP_DIGITAL_ARREST',
    );
    await user.selectOptions(form.getByRole('combobox', { name: /Confidence/ }), '4');
    await user.click(form.getByRole('button', { name: 'Decide' }));
    expect(
      await screen.findByText('Confirm fraud (Coerced victim — digital arrest), confidence 4/5'),
    ).toBeInTheDocument();
    expect(sent).toEqual({
      ifMatch: '"3"',
      body: {
        decision: 'CONFIRM_FRAUD',
        typology: 'APP_DIGITAL_ARREST',
        confidence: 4,
        note: null,
        override_rationale: null,
      },
    });
  });

  it('needs a rationale of 20 characters to override the recommendation', async () => {
    let posted = false;
    serve(mockCaseDetail());
    server.use(
      http.post(`${CASE_URL}/disposition`, () => {
        posted = true;
        return HttpResponse.json(mockCaseDetail());
      }),
    );
    const { user } = await openConsole();
    const form = within(await screen.findByRole('form', { name: 'Decide the case' }));
    await user.click(form.getByRole('radio', { name: 'Release' }));
    await user.selectOptions(form.getByRole('combobox', { name: /Confidence/ }), '2');
    await user.type(form.getByRole('textbox', { name: /Override rationale/ }), 'too short');
    await user.click(form.getByRole('button', { name: 'Decide' }));
    expect(
      await form.findByText('An override needs a rationale of at least 20 characters.'),
    ).toBeInTheDocument();
    expect(posted).toBe(false);
  });

  it('on a 412 says the case changed, with what changed, and reloads (FR-UI-04)', async () => {
    let version = 1;
    serve(() =>
      version === 1
        ? mockCaseDetail({ version: 1 })
        : mockCaseDetail({
            version: 2,
            recommended_decision: 'RELEASE',
            evidence_records: [mockEvidence()],
          }),
    );
    server.use(
      http.post(`${CASE_URL}/disposition`, ({ request }) => {
        version = 2; // someone else fetched evidence meanwhile
        return problem(
          412,
          'Precondition Failed',
          'VERSION_CONFLICT',
          'The case changed since you read it.',
          request.url,
        );
      }),
    );
    const { user } = await openConsole();
    const form = within(await screen.findByRole('form', { name: 'Decide the case' }));
    await user.click(form.getByRole('radio', { name: /Confirm fraud/ }));
    await user.selectOptions(form.getByRole('combobox', { name: /Typology/ }), 'MULE');
    await user.selectOptions(form.getByRole('combobox', { name: /Confidence/ }), '3');
    await user.click(form.getByRole('button', { name: 'Decide' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('This case changed — reload');
    expect(await within(alert).findByText('Recommendation: Confirm fraud → Release')).toBeVisible();
    expect(within(alert).getByText('Evidence records: 0 → 1')).toBeVisible();
    expect(form.getByRole('button', { name: 'Decide' })).toBeDisabled();

    await user.click(within(alert).getByRole('button', { name: 'Reload' }));
    expect(screen.queryByText('This case changed — reload')).not.toBeInTheDocument();
    expect(form.getByRole('button', { name: 'Decide' })).toBeEnabled();
  });
});

describe('disposition helpers', () => {
  it('offers the fraud hypotheses, most likely first, then FRAUD_OTHER', () => {
    expect(typologies(mockCaseDetail())).toEqual(['APP_DIGITAL_ARREST', 'MULE', 'FRAUD_OTHER']);
  });

  it('lists nothing when nothing a reader sees changed', () => {
    const detail = mockCaseDetail();
    expect(caseChanges(detail, { ...detail, version: 9 })).toEqual([]);
  });
});
