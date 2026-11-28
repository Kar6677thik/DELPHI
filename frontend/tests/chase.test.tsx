import { screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import type { ChaseCandidate, ChaseLien, ChaseView } from '../src/api/chase';
import {
  candidateStatus,
  goldenHourLabel,
  maskAccount,
  recoveredPaise,
} from '../src/routes/ChasePage';
import { problem, setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

const OP = '01930000-0000-7000-8000-00000000c001';
const URL = `/api/v1/chase/operations/${OP}`;
const MULE = '01930000-0000-7000-8000-00000000a111';
const SHOP = '01930000-0000-7000-8000-00000000b222';

function candidate(overrides: Partial<ChaseCandidate>): ChaseCandidate {
  return {
    id: '01930000-0000-7000-8000-00000000d001',
    account_id: MULE,
    hop: 1,
    taint_paise: 8_000_000,
    balance_paise: 8_000_000,
    p_present: 0.95,
    p_mule: 0.9,
    erv_paise: 7_077_147,
    wrongful_cost_paise: 66_000,
    net_paise: 7_011_147,
    rank: 1,
    recommended: true,
    ...overrides,
  };
}

const RECOMMENDED = candidate({});
const NOT_RECOMMENDED = candidate({
  id: '01930000-0000-7000-8000-00000000d002',
  account_id: SHOP,
  hop: 2,
  taint_paise: 100_000,
  balance_paise: 9_000_000,
  p_present: 0.98,
  p_mule: 0.02,
  erv_paise: 30_000,
  wrongful_cost_paise: 491_960,
  net_paise: -461_960,
  rank: 2,
  recommended: false,
});

function lien(overrides: Partial<ChaseLien> = {}): ChaseLien {
  return {
    id: '01930000-0000-7000-8000-00000000e001',
    candidate_id: RECOMMENDED.id,
    account_id: MULE,
    bank_code: 'DLPH',
    amount_paise: 8_000_000,
    status: 'PLACED',
    recorded_reasons: 'Traced ₹80,000.00 of reported fraud.',
    evidence_refs: [],
    approved_by: '00000000-0000-4000-8000-000000000002',
    approved_at: '2026-11-14T10:05:00Z',
    placed_at: '2026-11-14T10:05:00Z',
    review_due_at: null,
    released_at: null,
    release_reason: null,
    ...overrides,
  };
}

function chase(overrides: Partial<ChaseView> = {}): ChaseView {
  return {
    id: OP,
    complaint_id: null,
    source_payment_ids: ['01930000-0000-7000-8000-0000000000aa'],
    status: 'OPEN',
    reported_at: '2026-11-14T10:00:00Z',
    reported_loss_paise: 20_000_000,
    // Far ahead, so the timer is running whenever the test runs.
    golden_hour_ends_at: '2999-01-01T00:00:00Z',
    last_traced_at: '2026-11-14T10:00:00Z',
    candidates: [RECOMMENDED, NOT_RECOMMENDED],
    liens: [],
    tickets: [],
    version: 1,
    ...overrides,
  };
}

describe('the Chase screen (docs/10 §7, docs/18 §2)', () => {
  it('shows the candidates ranked, with their scores, to a viewer who cannot approve', async () => {
    setMockSession('viewer');
    server.use(http.get(URL, () => HttpResponse.json(chase())));
    renderApp(`/chase/${OP}`);
    const table = within(await screen.findByRole('table', { name: /Freeze candidates/ }));
    const [, first, second] = table.getAllByRole('row');
    expect(first).toHaveTextContent('••••a111');
    expect(first).toHaveTextContent('₹80,000.00');
    expect(first).toHaveTextContent('0.95');
    expect(first).toHaveTextContent('₹70,771.47');
    expect(first).toHaveTextContent('Recommended');
    expect(second).toHaveTextContent('••••b222');
    expect(second).toHaveTextContent('-₹4,619.60');
    expect(second).toHaveTextContent('Not recommended');
    expect(screen.getByRole('timer')).toHaveTextContent(/min \d\d s left/);
    expect(screen.getByText(/No lien yet/)).toBeVisible();
    // Approving is hidden without `chase:write` (docs/10 §5 step 1).
    expect(screen.queryByRole('button', { name: /Approve selected/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  });

  it('lets an investigator approve the recommended candidates, selected by default', async () => {
    setMockSession('investigator');
    let sent: { ids: unknown; key: string | null } | null = null;
    server.use(
      http.get(URL, () => HttpResponse.json(chase())),
      http.post(`${URL}/liens`, async ({ request }) => {
        const body = (await request.json()) as { candidate_ids: unknown };
        sent = { ids: body.candidate_ids, key: request.headers.get('Idempotency-Key') };
        return HttpResponse.json(chase({ liens: [lien()], version: 2 }));
      }),
    );
    const { user } = renderApp(`/chase/${OP}`);
    const approve = await screen.findByRole('button', { name: 'Approve selected (1)' });
    expect(screen.getByRole('checkbox', { name: 'Select account ••••a111' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Select account ••••b222' })).not.toBeChecked();
    await user.click(approve);

    const liens = within(await screen.findByRole('table', { name: /Liens/ }));
    expect(liens.getByRole('row', { name: /••••a111 DLPH ₹80,000.00 Placed/ })).toBeVisible();
    expect(sent).toMatchObject({ ids: [RECOMMENDED.id] });
    expect((sent as { key: string | null } | null)?.key).toMatch(/^[0-9a-f-]{36}$/);
    // The gauge counts the placed lien; the liened candidate can no longer be selected.
    expect(screen.getByRole('meter')).toHaveAttribute('aria-valuenow', '8000000');
    expect(screen.queryByRole('checkbox', { name: 'Select account ••••a111' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Approve selected (0)' })).toBeDisabled();
  });

  it('sends the investigator’s own selection', async () => {
    setMockSession('investigator');
    let ids: unknown = null;
    server.use(
      http.get(URL, () => HttpResponse.json(chase())),
      http.post(`${URL}/liens`, async ({ request }) => {
        ids = ((await request.json()) as { candidate_ids: unknown }).candidate_ids;
        return HttpResponse.json(chase());
      }),
    );
    const { user } = renderApp(`/chase/${OP}`);
    await user.click(await screen.findByRole('checkbox', { name: 'Select account ••••a111' }));
    await user.click(screen.getByRole('checkbox', { name: 'Select account ••••b222' }));
    await user.click(screen.getByRole('button', { name: 'Approve selected (1)' }));
    await screen.findByRole('button', { name: 'Approve selected (1)' });
    expect(ids).toEqual([NOT_RECOMMENDED.id]);
  });

  it('shows why an approval was refused', async () => {
    setMockSession('investigator');
    server.use(
      http.get(URL, () => HttpResponse.json(chase())),
      http.post(`${URL}/liens`, ({ request }) =>
        problem(
          409,
          'Conflict',
          'INVALID_STATE_TRANSITION',
          'A candidate already has a lien.',
          request.url,
        ),
      ),
    );
    const { user } = renderApp(`/chase/${OP}`);
    await user.click(await screen.findByRole('button', { name: 'Approve selected (1)' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('A candidate already has a lien.');
  });

  it('offers no approval on a closed operation, and shows a ticket’s status', async () => {
    setMockSession('investigator');
    const ticketed = lien({ bank_code: 'XBK1', status: 'TICKET_SENT', placed_at: null });
    server.use(
      http.get(URL, () =>
        HttpResponse.json(
          chase({
            status: 'CLOSED',
            golden_hour_ends_at: '2026-11-14T11:00:00Z',
            liens: [ticketed],
            tickets: [
              {
                id: '01930000-0000-7000-8000-00000000f001',
                lien_id: ticketed.id,
                target_bank: 'XBK1',
                status: 'SENT_SIMULATED',
                sent_at: '2026-11-14T10:05:00Z',
                acknowledged_at: null,
              },
            ],
          }),
        ),
      ),
    );
    renderApp(`/chase/${OP}`);
    const liens = within(await screen.findByRole('table', { name: /Liens/ }));
    expect(
      liens.getByRole('row', { name: /XBK1 ₹80,000.00 Ticket sent Sent simulated/ }),
    ).toBeVisible();
    expect(screen.getByRole('timer')).toHaveTextContent('Golden hour ended');
    expect(screen.getByRole('meter')).toHaveAttribute('aria-valuenow', '0'); // not placed yet
    expect(screen.queryByRole('button', { name: /Approve selected/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  });

  it('says so when the operation cannot be read', async () => {
    setMockSession('viewer');
    server.use(
      http.get(URL, ({ request }) =>
        problem(404, 'Not Found', 'NOT_FOUND', 'No chase operation with this id.', request.url),
      ),
    );
    renderApp(`/chase/${OP}`);
    expect(await screen.findByRole('alert')).toHaveTextContent('No chase operation with this id.');
  });
});

describe('the Chase screen’s figures', () => {
  it('masks an account to its last four characters', () => {
    expect(maskAccount(MULE)).toBe('••••a111');
  });

  it('counts down the golden hour and says when it has ended', () => {
    const ends = '2026-11-14T11:00:00Z';
    expect(goldenHourLabel(ends, Date.parse('2026-11-14T10:17:55Z'))).toBe('42 min 05 s left');
    expect(goldenHourLabel(ends, Date.parse('2026-11-14T10:59:59.5Z'))).toBe('0 min 01 s left');
    expect(goldenHourLabel(ends, Date.parse(ends))).toBe('Golden hour ended');
  });

  it('counts only placed liens as recovered', () => {
    const liens = [
      lien({ amount_paise: 500 }),
      lien({ amount_paise: 700, status: 'TICKET_SENT' }),
      lien({ amount_paise: 900, status: 'RELEASED' }),
      lien({ amount_paise: 0, status: 'FAILED_FUNDS_MOVED' }),
    ];
    expect(recoveredPaise(liens)).toBe(500);
  });

  it('shows a lien’s status over the recommendation, and an unscored candidate as such', () => {
    expect(candidateStatus(RECOMMENDED, lien({ status: 'FAILED_FUNDS_MOVED' }))).toBe(
      'Failed funds moved',
    );
    expect(candidateStatus(candidate({ recommended: null }), undefined)).toBe('Not scored');
  });
});
