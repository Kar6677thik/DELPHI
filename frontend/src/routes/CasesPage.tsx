import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router';

import { useCases } from '../api/cases';
import type { CaseSort, CaseView } from '../api/types';
import { MoneyText } from '../components/MoneyText';
import { ProblemAlert } from '../components/ProblemAlert';
import { humanise } from '../lib/format';

const MINUTE_MS = 60_000;
/** How often the countdown re-renders. */
const TICK_MS = 15_000;

/**
 * The SLA cell (docs/18 §2): "Due in 25 min", "Due in 3 h 10 min", "SLA breached", "Disposed".
 * Breached comes from the server (`sla_breached`, which knows DEMO time); the countdown rounds
 * up, so it never shows a breach early.
 */
export function slaLabel(item: CaseView, now: number): string {
  if (item.status === 'DISPOSED') return 'Disposed';
  if (item.sla_breached) return 'SLA breached';
  const minutes = Math.max(1, Math.ceil((Date.parse(item.sla_due_at) - now) / MINUTE_MS));
  if (minutes < 60) return `Due in ${String(minutes)} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `Due in ${String(hours)} h` : `Due in ${String(hours)} h ${String(rest)} min`;
}

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => {
      setNow(Date.now());
    }, intervalMs);
    return () => {
      clearInterval(id);
    };
  }, [intervalMs]);
  return now;
}

/**
 * The recommendation's expected loss in paise (docs/18 §2), which the backend stores from the
 * moment a case opens (#208). Null for a row without one.
 */
export function expectedLoss(item: CaseView): number | null {
  const decision = item.recommended_decision;
  const risk = decision ? item.risk_by_decision?.[decision] : undefined;
  return typeof risk === 'number' && Number.isFinite(risk) ? Math.round(risk) : null;
}

const SORTS: readonly { value: CaseSort; label: string }[] = [
  { value: 'sla_due_at', label: 'Soonest SLA' },
  { value: 'expected_loss', label: 'Highest expected loss' },
  { value: 'opened_at', label: 'Newest' },
];

/** The table's caption for each order. */
const CAPTIONS: Readonly<Record<CaseSort, string>> = {
  sla_due_at: 'Cases, soonest SLA first',
  expected_loss: 'Cases, highest expected loss first',
  opened_at: 'Cases, newest first',
};

const SELECT =
  'rounded-md border border-control bg-raised px-3 py-1.5 text-[13px] text-ink transition-colors hover:border-soft focus:border-accent focus:outline-none';

/**
 * The queue's one-line summary. It counts the rows loaded: with more pages to come it says so,
 * and never claims that none is past its SLA.
 */
export function summary(items: readonly CaseView[], more: boolean): string {
  const open = items.filter((c) => c.status !== 'DISPOSED').length;
  const breached = items.filter((c) => c.sla_breached && c.status !== 'DISPOSED').length;
  const late = `${String(breached)} past their SLA`;
  if (more) {
    return `Showing the first ${String(items.length)} cases${breached > 0 ? `; ${late}` : ''}.`;
  }
  const cases = `${String(open)} open ${open === 1 ? 'case' : 'cases'}`;
  return breached > 0 ? `${cases}, ${late}.` : `${cases}, none past SLA.`;
}

/** The case queue (docs/18 §2 `/cases`): soonest SLA, highest expected loss, or newest first. */
export function CasesPage() {
  const [sort, setSort] = useState<CaseSort>('sla_due_at');
  const [params] = useSearchParams();
  const campaignId = params.get('campaign_id');
  const cases = useCases(sort, campaignId);
  const now = useNow(TICK_MS);
  return (
    <section aria-labelledby="cases-title" className="max-w-[1320px]">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h1 id="cases-title" className="text-[26px] font-semibold tracking-tight">
            Case queue
          </h1>
          <p className="mt-1 text-[14px] text-muted">
            {cases.isSuccess && cases.data.items.length > 0
              ? summary(cases.data.items, cases.data.next_cursor !== null)
              : 'The payments Guardian held, for an investigator to decide.'}
          </p>
          {campaignId !== null && (
            <p className="mt-1 text-[13px] text-soft">
              One campaign's cases.{' '}
              <Link to={`/campaigns/${campaignId}`} className="underline">
                Back to the campaign
              </Link>{' '}
              ·{' '}
              <Link to="/cases" className="underline">
                All cases
              </Link>
            </p>
          )}
        </div>
        <label className="ml-auto flex items-center gap-2 text-[13px] text-muted">
          Order
          <select
            value={sort}
            onChange={(event) => {
              setSort(event.target.value as CaseSort);
            }}
            className={SELECT}
          >
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="mt-6">
        {cases.isPending && <p className="text-[14px] text-muted">Loading cases…</p>}
        {cases.isError && <ProblemAlert error={cases.error} title="Could not load the cases" />}
        {cases.isSuccess && cases.data.items.length === 0 && (
          <p className="rounded-md border border-dashed border-control px-4 py-3 text-[13px] text-muted">
            No cases yet. Guardian holds open them.
          </p>
        )}
        {cases.isSuccess && cases.data.items.length > 0 && (
          <div className="overflow-x-auto rounded-md bg-surface px-8 py-5">
            <table className="w-full min-w-[860px] text-left text-[14px]">
              <caption className="sr-only">{CAPTIONS[sort]}</caption>
              <thead className="text-[12px] text-muted">
                <tr className="border-b border-line">
                  <th scope="col" className="py-2 pr-6 font-normal">
                    SLA
                  </th>
                  <th scope="col" className="py-2 pr-6 font-normal">
                    Leading hypothesis
                  </th>
                  <th scope="col" className="py-2 pr-6 font-normal">
                    Type
                  </th>
                  <th scope="col" className="py-2 pr-6 font-normal">
                    Protocol
                  </th>
                  <th scope="col" className="py-2 pr-6 text-right font-normal">
                    At stake
                  </th>
                  <th scope="col" className="py-2 pr-6 text-right font-normal">
                    Expected loss
                  </th>
                  <th scope="col" className="py-2 pr-6 font-normal">
                    Status
                  </th>
                  <th scope="col" className="py-2 font-normal">
                    Campaign
                  </th>
                </tr>
              </thead>
              <tbody>
                {cases.data.items.map((item) => {
                  const loss = expectedLoss(item);
                  return (
                    <tr key={item.id} className="border-b border-line last:border-0">
                      <td
                        className={`py-3 pr-6 whitespace-nowrap ${
                          item.sla_breached && item.status !== 'DISPOSED'
                            ? 'text-negative'
                            : 'text-soft'
                        }`}
                      >
                        {slaLabel(item, now)}
                      </td>
                      <td className="py-3 pr-6">
                        <Link
                          to={`/cases/${item.id}`}
                          className="text-ink underline decoration-control underline-offset-4 transition-colors hover:decoration-soft"
                        >
                          {item.leading_hypothesis_name}
                        </Link>
                      </td>
                      <td className="py-3 pr-6 text-soft">{humanise(item.type)}</td>
                      <td className="py-3 pr-6">
                        {item.protocol === 'COERCION' ? (
                          <span className="rounded-sm bg-warning-bg px-2 py-0.5 text-[12px] text-warning">
                            Coercion
                          </span>
                        ) : (
                          <span className="text-soft">Standard</span>
                        )}
                      </td>
                      <td className="figure py-3 pr-6 text-right text-ink">
                        <MoneyText paise={item.amount_at_stake_paise} />
                      </td>
                      <td className="figure py-3 pr-6 text-right text-soft">
                        {loss === null ? '—' : <MoneyText paise={loss} />}
                      </td>
                      <td className="py-3 pr-6 text-soft">{humanise(item.status)}</td>
                      <td className="py-3 text-soft">
                        {item.campaign_id ? (
                          <Link
                            to={`/campaigns/${item.campaign_id}`}
                            className="underline decoration-control underline-offset-4 hover:decoration-soft"
                          >
                            View
                          </Link>
                        ) : (
                          '—'
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}
