import { screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import type { MetricsSummary } from '../src/api/types';
import { duration } from '../src/lib/format';
import { MOCK_STATUS, MOCK_SUMMARY, setMockSession } from './mocks/handlers';
import { server } from './mocks/node';
import { renderApp } from './renderApp';

/** The Overview (docs/18 §2 `/`, docs/22 §7, docs/20 §4, #126). */

const BUSY: MetricsSummary = {
  ...MOCK_SUMMARY,
  guardian: {
    decided: 142,
    decisions: { RELEASE: 118, HOLD_FOR_REVIEW: 13, PROTECTIVE_HOLD: 8, DECLINE: 3 },
    median_session_seconds: 7.4,
    active_probes_per_session: 0.61,
    info_yield_bits: { CALL_STATUS: 0.74, SPELL_BREAKER_DIGITAL_ARREST: 1.18 },
    held: 24,
    label_coverage_held: 0.83,
    dd_pack_completeness: 1,
  },
  cases: {
    open_cases: 6,
    open_by_protocol: { COERCION: 2, STANDARD: 4 },
    sla_breaches: 1,
    disposed: 15,
    median_hold_resolution_minutes: 23.5,
  },
};

describe('Overview', () => {
  it('leads with the finding, then the KPI strip, the decisions and the queue', async () => {
    setMockSession('viewer');
    server.use(http.get('/api/v1/metrics/summary', () => HttpResponse.json(BUSY)));
    renderApp('/');
    expect(
      await screen.findByText(
        '142 payments decided in the last 24 hours; 1 case is past their SLA.',
      ),
    ).toBeInTheDocument();
    const strip = screen.getByText('Payments decided').closest('dl');
    expect(strip).toHaveTextContent('142');
    expect(strip).toHaveTextContent('7 s'); // median Guardian time
    expect(strip).toHaveTextContent('83%');
    expect(strip).toHaveTextContent('of 24 held, within 7 days');
    expect(strip).toHaveTextContent('23 m 30 s'); // median hold resolution
    const decisions = screen.getByRole('list', { name: 'Decisions by type' });
    expect(within(decisions).getAllByRole('listitem')[0]).toHaveTextContent('Release118');
    const yields = screen.getByRole('list', { name: 'Mean information yield per probe' });
    expect(within(yields).getAllByRole('listitem')[0]).toHaveTextContent('1.18 bits');
    expect(screen.getByText('Coercion (30-minute SLA)')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows dashes and empty frames, never zeros, when nothing was decided', async () => {
    setMockSession('viewer');
    renderApp('/');
    expect(
      await screen.findByText('No payments decided in the last 24 hours.'),
    ).toBeInTheDocument();
    const strip = screen.getByText('Median Guardian time').closest('dl');
    expect(within(strip as HTMLElement).getAllByText('—').length).toBeGreaterThanOrEqual(4);
    expect(
      screen.getByText(/Not tracked yet: backup age, live-update lag, probe-yield drift/),
    ).toBeInTheDocument();
  });

  it('raises the docs/20 §4 alerts in a banner', async () => {
    setMockSession('viewer');
    server.use(
      http.get('/api/v1/metrics/summary', () =>
        HttpResponse.json({
          ...BUSY,
          alerts: [
            { code: 'JOB_FAILURES', message: '2 job(s) failed in the last hour.', count: 2 },
            {
              code: 'LLM_FALLBACK',
              message: '14 of the last 20 narratives fell back to the template.',
              count: 14,
            },
          ],
        }),
      ),
    );
    renderApp('/');
    const banner = await screen.findByRole('alert');
    expect(banner).toHaveTextContent('2 job(s) failed in the last hour.');
    expect(banner).toHaveTextContent('14 of the last 20 narratives fell back to the template.');
    expect(within(banner).getByRole('link', { name: 'See jobs' })).toHaveAttribute(
      'href',
      '/admin',
    );
  });

  it('says it is initialising until the data and the models are ready', async () => {
    setMockSession('viewer');
    server.use(
      http.get('/api/v1/status', () =>
        HttpResponse.json({ ...MOCK_STATUS, models_ready: false, seed_progress: 0.64 }),
      ),
    );
    renderApp('/');
    expect(await screen.findByText(/Initialising… 64%\. Training the models/)).toBeInTheDocument();
  });

  it('warns when a model is below its quality gate', async () => {
    setMockSession('viewer');
    server.use(
      http.get('/api/v1/status', () =>
        HttpResponse.json({ ...MOCK_STATUS, models_below_gate: ['PRIOR'] }),
      ),
    );
    renderApp('/');
    expect(await screen.findByText('Sensing below its quality gate: Prior.')).toBeInTheDocument();
  });

  it('switches the window to 7 days', async () => {
    setMockSession('viewer');
    let asked: string | null = null;
    server.use(
      http.get('/api/v1/metrics/summary', ({ request }) => {
        asked = new URL(request.url).searchParams.get('hours');
        return HttpResponse.json(MOCK_SUMMARY);
      }),
    );
    const { user } = renderApp('/');
    await user.click(await screen.findByRole('button', { name: '7 days' }));
    expect(await screen.findByText('No payments decided in the last 7 days.')).toBeInTheDocument();
    expect(asked).toBe('168');
    expect(screen.getByRole('button', { name: '7 days' })).toHaveAttribute('aria-pressed', 'true');
  });
});

describe('duration', () => {
  it.each([
    [null, '—'],
    [7.4, '7 s'],
    [125, '2 m 05 s'],
    [3_900, '1 h 05 m'],
  ])('%s → %s', (seconds, text) => {
    expect(duration(seconds)).toBe(text);
  });
});
