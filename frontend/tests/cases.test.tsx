import { act, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { slaLabel } from '../src/routes/CasesPage';
import { FakeEventSource } from './fakeEventSource';
import { mockCase, problem, setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

describe('case queue', () => {
  it('lists the cases with their SLA, protocol and amount', async () => {
    setMockSession('viewer');
    server.use(
      http.get('/api/v1/cases', () =>
        HttpResponse.json({
          items: [
            mockCase({ sla_breached: true }),
            mockCase({
              id: '01930000-0000-7000-8000-000000000002',
              protocol: 'STANDARD',
              leading_hypothesis: 'ATO',
              leading_hypothesis_name: 'Account takeover',
              amount_at_stake_paise: 1_250_000,
              sla_due_at: '2099-01-01T00:00:00Z',
            }),
          ],
          next_cursor: null,
        }),
      ),
    );
    renderApp('/cases');
    expect(await screen.findByRole('heading', { name: 'Case queue' })).toBeInTheDocument();
    const rows = within(await screen.findByRole('table'))
      .getAllByRole('row')
      .slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('SLA breached');
    expect(rows[0]).toHaveTextContent('Coercion');
    expect(rows[0]).toHaveTextContent('Coerced victim — digital arrest');
    expect(rows[0]).toHaveTextContent('₹4,80,000.00');
    expect(within(rows[0] as HTMLElement).getByRole('link')).toHaveAttribute(
      'href',
      '/cases/01930000-0000-7000-8000-000000000001',
    );
    expect(rows[1]).toHaveTextContent('Standard');
    expect(rows[1]).toHaveTextContent('Account takeover');
    expect(rows[1]).toHaveTextContent('₹12,500.00');
  });

  it('says when there are no cases', async () => {
    setMockSession('viewer');
    server.use(
      http.get('/api/v1/cases', () => HttpResponse.json({ items: [], next_cursor: null })),
    );
    renderApp('/cases');
    expect(await screen.findByText('No cases yet. Guardian holds open them.')).toBeInTheDocument();
  });

  it('shows the problem when the queue cannot load', async () => {
    setMockSession('viewer');
    server.use(
      http.get('/api/v1/cases', ({ request }) =>
        problem(
          422,
          'Unprocessable Content',
          'VALIDATION_FAILED',
          'The cursor is not valid.',
          request.url,
        ),
      ),
    );
    renderApp('/cases');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load the cases');
    expect(alert).toHaveTextContent('VALIDATION_FAILED');
  });
});

describe('slaLabel', () => {
  const now = Date.parse('2026-11-14T10:00:00Z');

  it('counts down in minutes, then hours, rounding up', () => {
    expect(slaLabel(mockCase({ sla_due_at: '2026-11-14T10:25:00Z' }), now)).toBe('Due in 25 min');
    expect(slaLabel(mockCase({ sla_due_at: '2026-11-14T10:24:20Z' }), now)).toBe('Due in 25 min');
    expect(slaLabel(mockCase({ sla_due_at: '2026-11-14T13:10:00Z' }), now)).toBe(
      'Due in 3 h 10 min',
    );
    expect(slaLabel(mockCase({ sla_due_at: '2026-11-14T14:00:00Z' }), now)).toBe('Due in 4 h');
  });

  it('takes the breach from the server, never from the browser clock', () => {
    // 20 s left by the browser clock: still counting, not breached early.
    expect(slaLabel(mockCase({ sla_due_at: '2026-11-14T10:00:20Z' }), now)).toBe('Due in 1 min');
    // The server says breached (DEMO time): believe it.
    expect(
      slaLabel(mockCase({ sla_breached: true, sla_due_at: '2099-01-01T00:00:00Z' }), now),
    ).toBe('SLA breached');
    expect(slaLabel(mockCase({ status: 'DISPOSED' }), now)).toBe('Disposed');
  });
});

describe('live updates', () => {
  it('re-fetches the queue when a case opens (docs/18 §1)', async () => {
    setMockSession('viewer');
    let calls = 0;
    server.use(
      http.get('/api/v1/cases', () => {
        calls += 1;
        return HttpResponse.json({
          items: calls === 1 ? [] : [mockCase()],
          next_cursor: null,
        });
      }),
    );
    renderApp('/cases');
    expect(await screen.findByText('No cases yet. Guardian holds open them.')).toBeInTheDocument();
    act(() => {
      FakeEventSource.latest().emit('case.opened', '7');
    });
    expect(await screen.findByText('Coerced victim — digital arrest')).toBeInTheDocument();
    expect(calls).toBe(2);
  });
});
