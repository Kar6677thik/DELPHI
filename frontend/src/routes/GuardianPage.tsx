import { useState } from 'react';
import { Link } from 'react-router';

import {
  type SessionFilters,
  useActiveKbDisplay,
  useGuardianSessions,
  useKbDisplays,
} from '../api/guardian';
import type { SessionMode, SessionStatus } from '../api/types';
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
    <section aria-labelledby="guardian-title">
      <h1 id="guardian-title" className="text-2xl font-semibold">
        Guardian monitor
      </h1>
      <form
        className="mt-4 flex flex-wrap items-end gap-4 text-sm"
        aria-label="Filters"
        onSubmit={(e) => {
          e.preventDefault();
        }}
      >
        <label className="flex flex-col gap-1">
          Status
          <select
            className="rounded border border-slate-300 px-2 py-1"
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
            className="rounded border border-slate-300 px-2 py-1"
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
            className="rounded border border-slate-300 px-2 py-1"
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
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={filters.unexplained ?? false}
            onChange={(e) => {
              setFilters({ ...filters, unexplained: e.target.checked || undefined });
            }}
          />
          Unexplained only
        </label>
      </form>
      <div className="mt-4">
        {sessions.isPending && <p className="text-slate-600">Loading sessions…</p>}
        {sessions.isError && (
          <ProblemAlert error={sessions.error} title="Could not load the sessions" />
        )}
        {sessions.isSuccess && items.length === 0 && (
          <p className="text-slate-600">No Guardian sessions match.</p>
        )}
        {sessions.isSuccess && items.length > 0 && (
          <>
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Guardian sessions, newest first</caption>
              <thead className="border-b border-slate-300 text-slate-600">
                <tr>
                  <th scope="col" className="py-2 pr-4">
                    Started
                  </th>
                  <th scope="col" className="py-2 pr-4 text-right">
                    Amount
                  </th>
                  <th scope="col" className="py-2 pr-4">
                    Leading hypothesis
                  </th>
                  <th scope="col" className="py-2 pr-4">
                    Status
                  </th>
                  <th scope="col" className="py-2 pr-4">
                    Decision
                  </th>
                  <th scope="col" className="py-2 pr-4">
                    B1 / B2
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((s) => (
                  <tr key={s.id} className="border-b border-slate-100">
                    <td className="py-2 pr-4">
                      <Link to={`/guardian/${s.id}`} className="underline">
                        {TIME.format(new Date(s.started_at))}
                      </Link>
                    </td>
                    <td className="py-2 pr-4 text-right">
                      <MoneyText paise={s.payment.amount_paise} />
                    </td>
                    <td className="py-2 pr-4">
                      {s.leading_hypothesis
                        ? (kbs.get(s.kb_version_id)?.styles[s.leading_hypothesis]?.name ??
                          humanise(s.leading_hypothesis))
                        : '—'}
                      {s.unexplained && (
                        <span className="ml-2 rounded bg-violet-100 px-2 py-0.5 text-violet-900">
                          Unexplained
                        </span>
                      )}
                    </td>
                    <td className="py-2 pr-4">
                      {isStatus(s.status) ? STATUSES[s.status] : humanise(s.status)}
                    </td>
                    <td className="py-2 pr-4">{s.decision ? decisionText(s.decision) : '—'}</td>
                    <td className="py-2 pr-4">
                      {s.baselines
                        ? `${decisionText(s.baselines.B1)} / ${decisionText(s.baselines.B2)}`
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {sessions.hasNextPage && (
              <p className="mt-3 flex items-center gap-3 text-sm text-slate-600">
                Showing the newest {items.length}; more sessions match.
                <button
                  type="button"
                  className="rounded border border-slate-300 px-3 py-1 text-slate-900"
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
