import { screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import type { CampaignDetail, CampaignView } from '../src/api/types';
import { stagesReached } from '../src/components/F3Strip';
import { LIVE_QUERIES } from '../src/lib/liveQueries';
import { mockCase, problem, setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

const ID = '01930000-0000-7000-8000-0000000000c1';

function campaign(overrides: Partial<CampaignView> = {}): CampaignView {
  return {
    id: ID,
    leading_hypothesis: 'APP_DIGITAL_ARREST',
    case_count: 3,
    f3_stage_max: null,
    expected_loss_paise: 120_000_000,
    vulnerable_victims: 1,
    rank_score: 1_400_000_00,
    merged_into: null,
    created_at: '2026-11-14T10:00:00Z',
    updated_at: '2026-11-14T10:05:00Z',
    ...overrides,
  };
}

function detail(overrides: Partial<CampaignDetail> = {}): CampaignDetail {
  return {
    ...campaign(),
    cases: [
      {
        id: '01930000-0000-7000-8000-000000000001',
        type: 'PAYMENT_HOLD',
        status: 'OPEN',
        leading_hypothesis: 'APP_DIGITAL_ARREST',
        opened_at: '2026-11-14T10:00:00Z',
        amount_at_stake_paise: 48_000_000,
      },
    ],
    shared_entities: [
      { kind: 'account', value: '01930000-0000-7000-8000-0000000000ee', case_count: 3 },
      { kind: 'ip24', value: '10.0.0.0/24', case_count: 2 },
    ],
    ...overrides,
  };
}

describe('campaigns', () => {
  it('lists the campaigns with their F3 strip, cases, loss and vulnerable victims', async () => {
    setMockSession('viewer');
    server.use(
      http.get('/api/v1/campaigns', () =>
        HttpResponse.json({
          items: [campaign(), campaign({ id: `${ID.slice(0, -1)}2`, f3_stage_max: 'EXECUTION' })],
          next_cursor: null,
        }),
      ),
    );
    renderApp('/campaigns');
    expect(await screen.findByRole('heading', { name: 'Campaigns' })).toBeInTheDocument();
    const rows = within(await screen.findByRole('table'))
      .getAllByRole('row')
      .slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('APP digital arrest');
    expect(rows[0]).toHaveTextContent('₹12,00,000.00');
    expect(within(rows[0] as HTMLElement).getByRole('link')).toHaveAttribute(
      'href',
      `/campaigns/${ID}`,
    );
    expect(within(rows[0] as HTMLElement).getByLabelText('F3 stage not known yet')).toBeVisible();
    expect(
      within(rows[1] as HTMLElement).getByLabelText('F3 stage: Execution, 3 of 4'),
    ).toBeVisible();
  });

  it('says when there are no campaigns', async () => {
    setMockSession('viewer');
    server.use(
      http.get('/api/v1/campaigns', () => HttpResponse.json({ items: [], next_cursor: null })),
    );
    renderApp('/campaigns');
    expect(await screen.findByText(/No campaigns yet/)).toBeInTheDocument();
  });

  it('shows the problem when the list cannot load', async () => {
    setMockSession('viewer');
    server.use(
      http.get('/api/v1/campaigns', ({ request }) =>
        problem(422, 'Unprocessable Content', 'VALIDATION_FAILED', 'Bad cursor.', request.url),
      ),
    );
    renderApp('/campaigns');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load the campaigns');
    expect(alert).toHaveTextContent('VALIDATION_FAILED');
  });

  it('shows one campaign: totals, shared infrastructure and its cases', async () => {
    setMockSession('viewer');
    server.use(
      http.get(`/api/v1/campaigns/${ID}`, () =>
        HttpResponse.json(detail({ case_count: 250, f3_stage_max: 'POSITIONING' })),
      ),
    );
    renderApp(`/campaigns/${ID}`);
    expect(await screen.findByRole('heading', { name: 'APP digital arrest' })).toBeInTheDocument();
    expect(screen.getByLabelText('F3 stage: Positioning, 2 of 4')).toBeVisible();
    expect(screen.getByText('250')).toBeInTheDocument();
    const [entities, cases] = screen.getAllByRole('table');
    expect(entities).toHaveTextContent('Counterparty account');
    expect(entities).toHaveTextContent('Login network (/24)');
    expect(entities).toHaveTextContent('10.0.0.0/24');
    expect(within(cases as HTMLElement).getAllByRole('row')).toHaveLength(2);
    expect(within(cases as HTMLElement).getByRole('link')).toHaveAttribute(
      'href',
      '/cases/01930000-0000-7000-8000-000000000001',
    );
    // More cases than the detail holds: the case queue, filtered, has all of them.
    expect(screen.getByRole('link', { name: 'All of them in the case queue' })).toHaveAttribute(
      'href',
      `/cases?campaign_id=${ID}`,
    );
  });

  it('points a merged campaign to the one that took its cases', async () => {
    setMockSession('viewer');
    const survivor = `${ID.slice(0, -1)}9`;
    server.use(
      http.get(`/api/v1/campaigns/${ID}`, () =>
        HttpResponse.json(detail({ merged_into: survivor, case_count: 0, cases: [] })),
      ),
    );
    renderApp(`/campaigns/${ID}`);
    expect(await screen.findByRole('link', { name: 'open that campaign' })).toHaveAttribute(
      'href',
      `/campaigns/${survivor}`,
    );
    expect(screen.getByText('No case is in this campaign now.')).toBeInTheDocument();
  });

  it('shows a 404 as a problem', async () => {
    setMockSession('viewer');
    server.use(
      http.get(`/api/v1/campaigns/${ID}`, ({ request }) =>
        problem(404, 'Not Found', 'NOT_FOUND', 'No campaign.', request.url),
      ),
    );
    renderApp(`/campaigns/${ID}`);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load the campaign');
    expect(alert).toHaveTextContent('NOT_FOUND');
  });

  it('links a case in the queue to its campaign, and filters the queue by campaign', async () => {
    setMockSession('viewer');
    let asked = '';
    server.use(
      http.get('/api/v1/cases', ({ request }) => {
        asked = new URL(request.url).searchParams.get('campaign_id') ?? '';
        return HttpResponse.json({
          items: [mockCase({ campaign_id: ID })],
          next_cursor: null,
        });
      }),
    );
    renderApp(`/cases?campaign_id=${ID}`);
    const row = within(await screen.findByRole('table')).getAllByRole('row')[1] as HTMLElement;
    expect(within(row).getByRole('link', { name: 'View' })).toHaveAttribute(
      'href',
      `/campaigns/${ID}`,
    );
    expect(asked).toBe(ID);
    expect(screen.getByRole('link', { name: 'Back to the campaign' })).toBeInTheDocument();
  });

  it('refreshes campaigns and cases on campaign.updated, and maps the F3 stages', () => {
    expect(LIVE_QUERIES['campaign.updated']).toEqual([['campaigns'], ['cases']]);
    expect(stagesReached(null)).toBe(0);
    expect(stagesReached('INITIAL_ACCESS')).toBe(1);
    expect(stagesReached('MONETIZATION')).toBe(4);
    expect(stagesReached('SOMETHING_ELSE')).toBe(0);
  });
});
