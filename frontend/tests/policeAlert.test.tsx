import { screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import type { CaseDetail, KbVersionView } from '../src/api/types';
import { alertFacts } from '../src/routes/PoliceAlertPanel';
import {
  MOCK_KB_CONTENT,
  MOCK_USERS,
  mockCaseDetail,
  problem,
  setMockSession,
} from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

const CASE_ID = '01930000-0000-7000-8000-000000000001';
const CASE_URL = `/api/v1/cases/${CASE_ID}`;
const OTHER_USER = '00000000-0000-4000-8000-0000000000aa';

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

type Alert = NonNullable<CaseDetail['police_alert']>;

/** The draft `open_case` makes for a coercion case (docs/09 §6, #195). */
function draft(overrides: Partial<Alert> = {}): Alert {
  return {
    id: '01930000-0000-7000-8000-0000000000a1',
    status: 'DRAFT',
    payload: {
      victim: 'CUST-…4821',
      amount_paise: 48_000_000,
      channel: 'UPI',
      beneficiary_account_id: '01930000-0000-7000-8000-0000000000cc',
      initiated_at: '2026-11-14T09:58:00Z',
      evidence: [],
    },
    acted_by: null,
    acted_at: null,
    ...overrides,
  };
}

function serve(detail: CaseDetail | (() => CaseDetail)) {
  server.use(
    http.get('/api/v1/kb/versions/1', () => HttpResponse.json(KB)),
    http.get(CASE_URL, () => {
      const body = typeof detail === 'function' ? detail() : detail;
      return HttpResponse.json(body, { headers: { ETag: `"${String(body.version)}"` } });
    }),
  );
}

async function open(user = 'investigator') {
  setMockSession(user);
  const app = renderApp(`/cases/${CASE_ID}`);
  await screen.findByRole('heading', { name: 'Hypotheses and recommendation' });
  return app;
}

function panel() {
  return within(screen.getByRole('region', { name: 'Police alert' }));
}

describe('the police alert in the console (docs/09 §6, #209)', () => {
  it('shows the draft with what it would report', async () => {
    serve(mockCaseDetail({ police_alert: draft() }));
    await open();
    const alert = panel();
    expect(alert.getByText('Draft, not sent')).toBeInTheDocument();
    expect(alert.getByText('CUST-…4821')).toBeInTheDocument();
    expect(alert.getByText('₹4,80,000.00')).toBeInTheDocument();
    expect(alert.getByText('UPI')).toBeInTheDocument();
    expect(alert.getByText('01930000-0000-7000-8000-0000000000cc')).toBeInTheDocument();
    expect(alert.getByRole('button', { name: 'Send' })).toBeInTheDocument();
    expect(alert.getByRole('button', { name: 'Discard' })).toBeInTheDocument();
  });

  it('shows nothing for a case without an alert (not a coercion case)', async () => {
    serve(mockCaseDetail({ protocol: 'STANDARD', police_alert: null }));
    await open();
    expect(screen.queryByRole('region', { name: 'Police alert' })).not.toBeInTheDocument();
  });

  it('offers no actions to a viewer (case:write only)', async () => {
    serve(mockCaseDetail({ police_alert: draft() }));
    await open('viewer');
    expect(panel().queryByRole('button')).not.toBeInTheDocument();
  });

  it('sends only after confirmation, then shows who sent it, when, and the evidence', async () => {
    const posted: string[] = [];
    serve(mockCaseDetail({ police_alert: draft() }));
    server.use(
      http.post(`${CASE_URL}/police-alert/:action`, ({ params }) => {
        posted.push(String(params.action));
        const sent = mockCaseDetail({
          version: 2,
          police_alert: draft({
            status: 'SENT_SIMULATED',
            acted_by: MOCK_USERS.investigator?.id ?? null,
            acted_at: '2026-11-14T10:05:00Z',
            payload: {
              ...draft().payload,
              evidence: [{ source_key: 'ACCOUNT_PROFILE', outcome: 'NEW_FULL_KYC', at: '…' }],
            },
          }),
        });
        return HttpResponse.json(sent, { headers: { ETag: '"2"' } });
      }),
    );
    const { user } = await open();
    await user.click(panel().getByRole('button', { name: 'Send' }));
    expect(panel().getByText(/Send this alert to the police\?/)).toBeInTheDocument();
    await user.click(panel().getByRole('button', { name: 'Cancel' }));
    expect(posted).toEqual([]); // nothing is sent without the confirmation
    await user.click(panel().getByRole('button', { name: 'Send' }));
    await user.click(panel().getByRole('button', { name: 'Yes, send it' }));
    expect(await panel().findByText(/^Sent \(simulated\) by you, /)).toBeInTheDocument();
    expect(posted).toEqual(['send']);
    expect(panel().getByText('Account profile: New full kyc')).toBeInTheDocument();
    expect(panel().queryByRole('button', { name: 'Send' })).not.toBeInTheDocument();
  });

  it('discards after confirmation', async () => {
    const posted: string[] = [];
    serve(mockCaseDetail({ police_alert: draft() }));
    server.use(
      http.post(`${CASE_URL}/police-alert/:action`, ({ params }) => {
        posted.push(String(params.action));
        return HttpResponse.json(
          mockCaseDetail({
            version: 2,
            police_alert: draft({
              status: 'DISCARDED',
              acted_by: MOCK_USERS.investigator?.id ?? null,
              acted_at: '2026-11-14T10:05:00Z',
            }),
          }),
          { headers: { ETag: '"2"' } },
        );
      }),
    );
    const { user } = await open();
    await user.click(panel().getByRole('button', { name: 'Discard' }));
    await user.click(panel().getByRole('button', { name: 'Yes, discard it' }));
    expect(await panel().findByText(/^Discarded by you, /)).toBeInTheDocument();
    expect(posted).toEqual(['discard']);
  });

  it('on a 409 says someone already acted, and reloads the case', async () => {
    let acted = false;
    serve(() =>
      mockCaseDetail({
        police_alert: acted
          ? draft({
              status: 'SENT_SIMULATED',
              acted_by: OTHER_USER,
              acted_at: '2026-11-14T10:04:00Z',
            })
          : draft(),
      }),
    );
    server.use(
      http.post(`${CASE_URL}/police-alert/:action`, ({ request }) => {
        acted = true; // a colleague sent it a moment earlier
        return problem(
          409,
          'Conflict',
          'INVALID_STATE_TRANSITION',
          'Already acted on.',
          request.url,
        );
      }),
    );
    const { user } = await open();
    await user.click(panel().getByRole('button', { name: 'Discard' }));
    await user.click(panel().getByRole('button', { name: 'Yes, discard it' }));
    expect(
      await panel().findByText('Someone already acted on this alert. The case has been reloaded.'),
    ).toBeInTheDocument();
    expect(
      await panel().findByText(new RegExp(`^Sent \\(simulated\\) by ${OTHER_USER}`)),
    ).toBeInTheDocument();
  });
});

describe('alertFacts', () => {
  it('reads the payload with checks and keeps only well-formed evidence', () => {
    expect(
      alertFacts({
        victim: 'CUST-…4821',
        amount_paise: 1.5,
        channel: '',
        evidence: [{ source_key: 'A', outcome: 'B' }, { source_key: 'C' }, 'x', null],
      }),
    ).toEqual({
      victim: 'CUST-…4821',
      amountPaise: null,
      channel: null,
      beneficiary: null,
      initiatedAt: null,
      evidence: [{ source: 'A', outcome: 'B', at: null }],
    });
  });
});
