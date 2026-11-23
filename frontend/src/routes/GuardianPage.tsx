import { useState } from 'react';
import { Link } from 'react-router';

import {
  type SessionFilters,
  useActiveKbDisplay,
  useGuardianSessions,
  useKbDisplays,
} from '../api/guardian';
import type { SessionMode, SessionStatus } from '../api/types';
import { forDark } from '../components/HypothesisBar';
import { MoneyText } from '../components/MoneyText';
import { ProblemAlert } from '../components/ProblemAlert';
import { decisionText, humanise } from '../lib/format';

/** Every session status in the schema: a status added to the api fails to compile here. */
const STATUSES: Readonly<Record<SessionStatus, string>> = {
  CREATED: 'Created',
  PASSIVE_PROBING: 'Passive probing',
  PLANNING: 'Planning',
  AWAITING_CUSTOMER: 'Awaiting customer',
  DECIDED: 'Decided',
  CANCELLED_BY_CUSTOMER: 'Cancelled by customer',
};

const MODES: Readonly<Record<SessionMode, string>> = {
  LIVE: 'Live',
  AUTOPLAY: 'Autoplay',
  EVAL: 'Evaluation',
};

function isStatus(value: string): value is SessionStatus {
  return value in STATUSES;
}

function isMode(value: string): value is SessionMode {
  return value in MODES;
}

const SELECT =
  'rounded-md border border-control bg-raised px-3 py-1.5 text-[13px] text-ink transition-colors hover:border-soft focus:border-accent focus:outline-none';

/** Statuses still in flight: the row carries a live mark. */
const IN_FLIGHT = new Set<string>(['CREATED', 'PASSIVE_PROBING', 'PLANNING', 'AWAITING_CUSTOMER']);

const DECISION_TONE: Readonly<Record<string, string>> = {
  RELEASE: 'text-positive',
  HOLD_FOR_REVIEW: 'text-warning',
  PROTECTIVE_HOLD: 'text-negative',
  DECLINE: 'text-negative',
};

const TIME = new Intl.DateTimeFormat('en-IN', {
  dateStyle: 'medium',
  timeStyle: 'medium',
  timeZone: 'Asia/Kolkata',
});

/** The Guardian monitor (docs/18 §2 `/guardian`): sessions newest first, live via SSE. */
export function GuardianPage() {
  const [filters, setFilters] = useState<SessionFilters>({});
  const sessions = useGuardianSessions(filters);
  const items = sessions.data?.pages.flatMap((p) => p.items) ?? [];
  // Each row is named by its own pinned KB version (docs/07 §4).
  const kbs = useKbDisplays([...new Set(items.map((s) => s.kb_version_id))]);
  // The decisions the api accepts are the ACTIVE KB's GUARDIAN_PAYMENT ones (docs/16).
  const active = useActiveKbDisplay();
  return (
    <section aria-labelledby="guardian-title" className="max-w-[1320px]">
      <h1 id="guardian-title" className="text-[26px] font-semibold tracking-tight">
        Guardian monitor
      </h1>
      <p className="mt-1 text-[14px] text-muted">
        Every payment DELPHI weighs, newest first, as it happens.
      </p>
      <form
        className="mt-6 flex flex-wrap items-end gap-4 text-[12px] text-muted"
        aria-label="Filters"
        onSubmit={(e) => {
          e.preventDefault();
        }}
      >
        <label className="flex flex-col gap-1">
          Status
          <select
            className={SELECT}
            value={filters.status ?? ''}
            onChange={(e) => {
              const v = e.target.value;
              setFilters({ ...filters, status: isStatus(v) ? v : undefined });
            }}
          >
            <option value="">Any</option>
            {Object.entries(STATUSES).map(([value, text]) => (
              <option key={value} value={value}>
                {text}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          Decision
          <select
            className={SELECT}
            value={filters.decision ?? ''}
            onChange={(e) => {
              setFilters({ ...filters, decision: e.target.value || undefined });
            }}
          >
            <option value="">Any</option>
            {(active.data?.decisions ?? []).map((d) => (
              <option key={d} value={d}>
                {decisionText(d)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          Mode
          <select
            className={SELECT}
            value={filters.mode ?? ''}
            onChange={(e) => {
              const v = e.target.value;
              setFilters({ ...filters, mode: isMode(v) ? v : undefined });
            }}
          >
            <option value="">Any</option>
            {Object.entries(MODES).map(([value, text]) => (
              <option key={value} value={value}>
                {text}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 pb-2 text-[13px] text-soft">
          <input
            type="checkbox"
            className="accent-[var(--color-accent)]"
            checked={filters.unexplained ?? false}
            onChange={(e) => {
              setFilters({ ...filters, unexplained: e.target.checked || undefined });
            }}
          />
          Unexplained only
        </label>
      </form>
      <div className="mt-5 rounded-md bg-surface px-8 py-6">
        {sessions.isPending && <p className="text-[14px] text-muted">Loading sessions…</p>}
        {sessions.isError && (
          <ProblemAlert error={sessions.error} title="Could not load the sessions" />
        )}
        {sessions.isSuccess && items.length === 0 && (
          <p className="text-[14px] text-muted">No Guardian sessions match.</p>
        )}
        {sessions.isSuccess && items.length > 0 && (
          <>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[860px] text-left text-[14px]">
                <caption className="sr-only">Guardian sessions, newest first</caption>
                <thead className="border-b border-line text-[12px] text-muted">
                  <tr>
                    <th scope="col" className="py-2 pr-4 font-normal">
                      Started
                    </th>
                    <th scope="col" className="py-2 pr-6 text-right font-normal">
                      Amount
                    </th>
                    <th scope="col" className="py-2 pr-4 font-normal">
                      Leading hypothesis
                    </th>
                    <th scope="col" className="py-2 pr-4 font-normal">
                      Status
                    </th>
                    <th scope="col" className="py-2 pr-4 font-normal">
                      Decision
                    </th>
                    <th scope="col" className="py-2 pr-4 font-normal">
                      B1 / B2
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((s) => (
                    <tr
                      key={s.id}
                      className="border-b border-line text-soft transition-colors last:border-b-0 hover:bg-raised/50"
                    >
                      <td className="py-3 pr-4">
                        <Link
                          to={`/guardian/${s.id}`}
                          className="figure text-[13px] text-ink underline decoration-control underline-offset-4 hover:decoration-soft"
                        >
                          {TIME.format(new Date(s.started_at))}
                        </Link>
                      </td>
                      <td className="figure py-3 pr-6 text-right text-ink">
                        <MoneyText paise={s.payment.amount_paise} />
                      </td>
                      <td className="py-3 pr-4">
                        {s.leading_hypothesis ? (
                          <span className="inline-flex items-center gap-2">
                            <span
                              aria-hidden
                              className="size-2 shrink-0 rounded-full"
                              style={{
                                background: forDark(
                                  kbs.get(s.kb_version_id)?.styles[s.leading_hypothesis]?.color ??
                                    '#8A8C92',
                                ),
                              }}
                            />
                            {kbs.get(s.kb_version_id)?.styles[s.leading_hypothesis]?.name ??
                              humanise(s.leading_hypothesis)}
                          </span>
                        ) : (
                          '—'
                        )}
                        {s.unexplained && (
                          <span className="ml-2 rounded-sm bg-raised px-2 py-0.5 text-[12px] text-soft">
                            Unexplained
                          </span>
                        )}
                      </td>
                      <td className="py-3 pr-4">
                        <span className="inline-flex items-center gap-2">
                          {IN_FLIGHT.has(s.status) && (
                            <span aria-hidden className="relative flex size-2">
                              <span className="absolute inline-flex size-full animate-ping rounded-full bg-accent opacity-60" />
                              <span className="relative inline-flex size-2 rounded-full bg-accent" />
                            </span>
                          )}
                          {isStatus(s.status) ? STATUSES[s.status] : humanise(s.status)}
                        </span>
                      </td>
                      <td
                        className={`py-3 pr-4 ${s.decision ? (DECISION_TONE[s.decision] ?? '') : ''}`}
                      >
                        {s.decision ? decisionText(s.decision) : '—'}
                      </td>
                      <td className="py-3 pr-4 text-muted">
                        {s.baselines
                          ? `${decisionText(s.baselines.B1)} / ${decisionText(s.baselines.B2)}`
                          : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {sessions.hasNextPage && (
              <p className="mt-4 flex items-center gap-3 text-[13px] text-muted">
                Showing the newest {items.length}; more sessions match.
                <button
                  type="button"
                  className="rounded-md border border-control px-3 py-1.5 text-soft transition-colors hover:border-soft hover:text-ink"
                  disabled={sessions.isFetchingNextPage}
                  onClick={() => {
                    void sessions.fetchNextPage();
                  }}
                >
                  {sessions.isFetchingNextPage ? 'Loading…' : 'Load more sessions'}
                </button>
              </p>
            )}
          </>
        )}
      </div>
    </section>
  );
}
