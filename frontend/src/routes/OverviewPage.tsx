import { useState } from 'react';
import { Link } from 'react-router';

import { useKbDisplays } from '../api/guardian';
import { useMetricsSummary, useRecentDecisions, useStatus } from '../api/overview';
import type { MetricsSummary, StatusView } from '../api/types';
import { MoneyText } from '../components/MoneyText';
import { ProblemAlert } from '../components/ProblemAlert';
import { decisionText, duration, humanise } from '../lib/format';

/**
 * The Overview (docs/18 §2 `/`, docs/22 §7, docs/20 §4, #126). Its one question: is DELPHI
 * working right now, and does anything need me? So the alerts come first, then one strip of
 * live KPIs, then where the decisions went and the case queue, what each probe is worth, and
 * the latest decisions. Every figure is over the window chosen; a figure with nothing to count
 * is a dash, never a 0.
 */

const WINDOWS = [
  { hours: 24, label: '24 hours' },
  { hours: 24 * 7, label: '7 days' },
] as const;

const NOT_TRACKED: Readonly<Record<string, string>> = {
  BACKUP_AGE: 'backup age',
  SSE_OUTBOX_LAG: 'live-update lag',
  PROBE_YIELD_DEGRADED: 'probe-yield drift',
};

const SECONDS_PER_MINUTE = 60;
const PERCENT = 100;

function share(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * PERCENT).toFixed(0)}%`;
}

function plural(n: number, one: string, many: string): string {
  return `${String(n)} ${n === 1 ? one : many}`;
}

export function OverviewPage() {
  const [hours, setHours] = useState<number>(WINDOWS[0].hours);
  const summary = useMetricsSummary(hours);
  const status = useStatus();
  const window = WINDOWS.find((w) => w.hours === hours)?.label ?? `${String(hours)} hours`;

  return (
    <section aria-labelledby="overview-title" className="max-w-[1320px]">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h1 id="overview-title" className="text-[26px] font-semibold tracking-tight">
            Overview
          </h1>
          <p className="mt-1 text-[14px] text-muted">
            {summary.data ? finding(summary.data, window) : `The last ${window} of decisions.`}
          </p>
        </div>
        <div role="group" aria-label="Window" className="ml-auto flex gap-5">
          {WINDOWS.map((w) => (
            <button
              key={w.hours}
              type="button"
              aria-pressed={hours === w.hours}
              onClick={() => {
                setHours(w.hours);
              }}
              className={`py-1 text-[13px] transition-colors ${
                hours === w.hours ? 'selected-line text-ink' : 'text-muted hover:text-ink'
              }`}
            >
              {w.label}
            </button>
          ))}
        </div>
      </div>

      {status.data && <StatusBanner status={status.data} />}
      {summary.data && summary.data.alerts.length > 0 && <Alerts summary={summary.data} />}

      {summary.isError && (
        <div className="mt-6 max-w-xl">
          <ProblemAlert error={summary.error} title="Could not load the live figures" />
        </div>
      )}

      <KpiStrip summary={summary.data} />

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Decisions summary={summary.data} window={window} />
        <Queue summary={summary.data} />
        <Yields summary={summary.data} window={window} />
        <Recent />
      </div>

      {summary.data && (
        <p className="mt-6 text-[12px] text-muted">
          Not tracked yet:{' '}
          {summary.data.alerts_not_tracked.map((c) => NOT_TRACKED[c] ?? humanise(c)).join(', ')}.
          The golden-hour ratio arrives with the Chase loop.
        </p>
      )}
    </section>
  );
}

function finding(summary: MetricsSummary, window: string): string {
  const parts = [
    `${plural(summary.guardian.decided, 'payment', 'payments')} decided in the last ${window}`,
  ];
  if (summary.cases.sla_breaches > 0) {
    parts.push(`${plural(summary.cases.sla_breaches, 'case is', 'cases are')} past their SLA`);
  } else if (summary.cases.open_cases > 0) {
    parts.push(`${plural(summary.cases.open_cases, 'case', 'cases')} open, none past SLA`);
  }
  return `${parts.join('; ')}.`;
}

function StatusBanner({ status }: { status: StatusView }) {
  if (!status.dataset_ready || !status.models_ready) {
    const progress =
      status.seed_progress === null ? '' : ` ${(status.seed_progress * PERCENT).toFixed(0)}%`;
    return (
      <p role="status" className="mt-6 rounded-md bg-warning-bg px-5 py-3 text-[14px] text-warning">
        Initialising…{progress}.{' '}
        {status.dataset_ready ? 'Training the models' : 'Generating the demo data'}; decisions start
        once both are ready.
      </p>
    );
  }
  if (status.models_below_gate.length > 0) {
    return (
      <p role="status" className="mt-6 rounded-md bg-warning-bg px-5 py-3 text-[14px] text-warning">
        Sensing below its quality gate: {status.models_below_gate.map(humanise).join(', ')}.
      </p>
    );
  }
  return null;
}

function Alerts({ summary }: { summary: MetricsSummary }) {
  return (
    <ul role="alert" aria-label="Alerts" className="mt-6 space-y-2">
      {summary.alerts.map((a) => (
        <li key={a.code} className="rounded-md bg-negative-bg px-5 py-3 text-[14px] text-negative">
          {a.message}
          {a.code.startsWith('JOB') && (
            <>
              {' '}
              <Link to="/admin" className="underline underline-offset-2">
                See jobs
              </Link>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

/** A figure with its units set apart: "23 m 30 s" → mono digits, small grey units. */
function Figure({ text }: { text: string }) {
  const parts = text.split(/(\d[\d.,]*)/).filter((p) => p !== '');
  return (
    <>
      {parts.map((part, i) =>
        /^\d/.test(part) ? (
          <span key={i} className="figure">
            {part}
          </span>
        ) : (
          <span key={i} className="font-sans text-[14px] text-muted">
            {part}
          </span>
        ),
      )}
    </>
  );
}

function Kpi({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="min-w-0 px-6 py-5 first:pl-0 sm:border-l sm:border-line sm:first:border-l-0">
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="mt-2 text-[24px] leading-none">
        <Figure text={value} />
      </dd>
      {note && <dd className="mt-2 text-[12px] text-muted">{note}</dd>}
    </div>
  );
}

function KpiStrip({ summary }: { summary: MetricsSummary | undefined }) {
  const g = summary?.guardian;
  const c = summary?.cases;
  return (
    <dl className="mt-6 grid grid-cols-2 rounded-md bg-surface px-8 sm:grid-cols-3 xl:grid-cols-6">
      <Kpi label="Payments decided" value={g ? String(g.decided) : '—'} />
      <Kpi label="Median Guardian time" value={duration(g?.median_session_seconds)} />
      <Kpi
        label="Active probes per payment"
        value={
          g?.active_probes_per_session === null || g?.active_probes_per_session === undefined
            ? '—'
            : g.active_probes_per_session.toFixed(2)
        }
      />
      <Kpi
        label="Label coverage, held"
        value={share(g?.label_coverage_held)}
        note={g ? `of ${String(g.held)} held, within 7 days` : undefined}
      />
      <Kpi label="DD pack complete" value={share(g?.dd_pack_completeness)} />
      <Kpi
        label="Median hold resolution"
        value={duration(
          c?.median_hold_resolution_minutes === null ||
            c?.median_hold_resolution_minutes === undefined
            ? null
            : c.median_hold_resolution_minutes * SECONDS_PER_MINUTE,
        )}
        note={c ? `${String(c.disposed)} disposed` : undefined}
      />
    </dl>
  );
}

function Card({
  id,
  title,
  action,
  children,
}: {
  id: string;
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="rounded-md bg-surface p-8">
      <div className="flex items-baseline justify-between gap-4">
        <h2 id={id} className="text-[20px] text-muted">
          {title}
        </h2>
        {action}
      </div>
      <div className="mt-5">{children}</div>
    </section>
  );
}

/** One row per item, its bar as long as its share of the largest (a ranked bar list). */
function BarList({
  rows,
  format,
  tone,
  label,
}: {
  rows: readonly { key: string; name: string; value: number }[];
  format: (v: number) => string;
  tone: 'ink' | 'accent';
  label: string;
}) {
  const max = Math.max(...rows.map((r) => r.value), 0);
  return (
    <ul aria-label={label} className="space-y-3.5">
      {rows.map((r) => (
        <li key={r.key}>
          <div className="flex items-baseline justify-between gap-4 text-[14px]">
            <span className="min-w-0 truncate">{r.name}</span>
            <span className="figure shrink-0 text-soft">{format(r.value)}</span>
          </div>
          <div className="mt-1.5 h-1 w-full rounded-full bg-raised">
            <div
              className={`h-full rounded-full transition-[width] duration-500 ${tone === 'accent' ? 'bg-accent' : 'bg-soft'}`}
              style={{ width: `${String(max > 0 ? (r.value / max) * 100 : 0)}%` }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-md border border-dashed border-control px-4 py-3 text-[13px] text-muted">
      {children}
    </p>
  );
}

function Decisions({ summary, window }: { summary: MetricsSummary | undefined; window: string }) {
  const rows = Object.entries(summary?.guardian.decisions ?? {})
    .map(([key, value]) => ({ key, name: decisionText(key), value }))
    .sort((a, b) => b.value - a.value);
  return (
    <Card id="decisions-title" title="Where the decisions went">
      {!summary ? (
        <p className="text-muted">Loading…</p>
      ) : rows.length === 0 ? (
        <Empty>No payments decided in the last {window}.</Empty>
      ) : (
        <BarList rows={rows} format={String} tone="ink" label="Decisions by type" />
      )}
    </Card>
  );
}

function Queue({ summary }: { summary: MetricsSummary | undefined }) {
  const c = summary?.cases;
  return (
    <Card
      id="queue-title"
      title="Case queue"
      action={
        <Link to="/cases" className="text-[13px] text-soft hover:text-ink">
          Open the queue
        </Link>
      }
    >
      {!c ? (
        <p className="text-muted">Loading…</p>
      ) : (
        <dl className="grid grid-cols-2 gap-x-8 gap-y-6">
          <div>
            <dt className="text-[12px] text-muted">Open cases</dt>
            <dd className="figure mt-2 text-[32px] leading-none">{c.open_cases}</dd>
          </div>
          <div>
            <dt className="text-[12px] text-muted">Past their SLA</dt>
            <dd
              className={`figure mt-2 text-[32px] leading-none ${c.sla_breaches > 0 ? 'text-negative' : ''}`}
            >
              {c.sla_breaches}
            </dd>
          </div>
          {Object.entries(c.open_by_protocol).map(([protocol, n]) => (
            <div key={protocol}>
              <dt className="text-[12px] text-muted">
                {protocol === 'COERCION' ? 'Coercion (30-minute SLA)' : 'Standard (4-hour SLA)'}
              </dt>
              <dd className="figure mt-1 text-[18px]">{n}</dd>
            </div>
          ))}
        </dl>
      )}
    </Card>
  );
}

function Yields({ summary, window }: { summary: MetricsSummary | undefined; window: string }) {
  const rows = Object.entries(summary?.guardian.info_yield_bits ?? {})
    .map(([key, value]) => ({ key, name: humanise(key), value }))
    .sort((a, b) => b.value - a.value);
  return (
    <Card id="yield-title" title="What each probe is worth">
      {!summary ? (
        <p className="text-muted">Loading…</p>
      ) : rows.length === 0 ? (
        <Empty>No probe answered in the last {window}.</Empty>
      ) : (
        <BarList
          rows={rows}
          format={(v) => `${v.toFixed(2)} bits`}
          tone="accent"
          label="Mean information yield per probe"
        />
      )}
    </Card>
  );
}

function Recent() {
  const recent = useRecentDecisions();
  const kbs = useKbDisplays([...new Set((recent.data?.items ?? []).map((s) => s.kb_version_id))]);
  return (
    <Card
      id="recent-title"
      title="Latest decisions"
      action={
        <Link to="/guardian" className="text-[13px] text-soft hover:text-ink">
          View all
        </Link>
      }
    >
      {recent.isPending && <p className="text-muted">Loading…</p>}
      {recent.isError && <ProblemAlert error={recent.error} title="Could not load the decisions" />}
      {recent.data && recent.data.items.length === 0 && <Empty>No decisions yet.</Empty>}
      {recent.data && recent.data.items.length > 0 && (
        <ul aria-label="Latest decisions">
          {recent.data.items.map((s) => (
            <li key={s.id} className="border-b border-rule last:border-0">
              <Link
                to={`/guardian/${s.id}`}
                className="grid grid-cols-[1fr_auto] items-baseline gap-x-4 py-3 hover:text-ink"
              >
                <span className="min-w-0">
                  <span className="block truncate text-[14px]">
                    {s.decision ? decisionText(s.decision) : 'Undecided'}
                  </span>
                  <span className="block truncate text-[12px] text-muted">
                    {s.leading_hypothesis
                      ? (kbs.get(s.kb_version_id)?.styles[s.leading_hypothesis]?.name ??
                        humanise(s.leading_hypothesis))
                      : '—'}{' '}
                    ·{' '}
                    {new Date(s.decided_at ?? s.started_at).toLocaleTimeString('en-IN', {
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </span>
                </span>
                <span className="figure text-[14px] text-soft">
                  <MoneyText paise={s.payment.amount_paise} />
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
