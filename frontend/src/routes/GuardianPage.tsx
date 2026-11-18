import { useState } from 'react';
import { Link } from 'react-router';

import { type SessionFilters, useGuardianSessions, useHypothesisStyles } from '../api/guardian';
import { MoneyText } from '../components/MoneyText';
import { ProblemAlert } from '../components/ProblemAlert';

const STATUSES = [
  'CREATED',
  'PASSIVE_PROBING',
  'PLANNING',
  'AWAITING_CUSTOMER',
  'DECIDED',
  'CANCELLED_BY_CUSTOMER',
] as const;
const DECISIONS = ['RELEASE', 'HOLD_FOR_REVIEW', 'PROTECTIVE_HOLD', 'DECLINE'] as const;

export function humanise(key: string): string {
  const text = key.replaceAll('_', ' ').toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
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
  const styles = useHypothesisStyles(
    sessions.isSuccess ? sessions.data.items[0]?.kb_version_id : undefined,
  );
  const names = styles.data ?? {};
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
              setFilters({ ...filters, status: e.target.value || undefined });
            }}
          >
            <option value="">Any</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {humanise(s)}
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
            {DECISIONS.map((d) => (
              <option key={d} value={d}>
                {humanise(d)}
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
        {sessions.isSuccess && sessions.data.items.length === 0 && (
          <p className="text-slate-600">No Guardian sessions match.</p>
        )}
        {sessions.isSuccess && sessions.data.items.length > 0 && (
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
              {sessions.data.items.map((s) => (
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
                      ? (names[s.leading_hypothesis]?.name ?? humanise(s.leading_hypothesis))
                      : '—'}
                    {s.unexplained && (
                      <span className="ml-2 rounded bg-violet-100 px-2 py-0.5 text-violet-900">
                        Unexplained
                      </span>
                    )}
                  </td>
                  <td className="py-2 pr-4">{humanise(s.status)}</td>
                  <td className="py-2 pr-4">{s.decision ? humanise(s.decision) : '—'}</td>
                  <td className="py-2 pr-4">
                    {s.baselines
                      ? `${humanise(s.baselines.B1)} / ${humanise(s.baselines.B2)}`
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
