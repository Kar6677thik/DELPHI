import { useEffect, useState } from 'react';
import { Link } from 'react-router';

import { useCases } from '../api/cases';
import type { CaseView } from '../api/types';
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

/** The case queue (docs/18 §2 `/cases`): the M4 stub lists the open and recent cases. */
export function CasesPage() {
  const cases = useCases();
  const now = useNow(TICK_MS);
  return (
    <section aria-labelledby="cases-title">
      <h1 id="cases-title" className="text-2xl font-semibold">
        Case queue
      </h1>
      <div className="mt-4">
        {cases.isPending && <p className="text-slate-600">Loading cases…</p>}
        {cases.isError && <ProblemAlert error={cases.error} title="Could not load the cases" />}
        {cases.isSuccess && cases.data.items.length === 0 && (
          <p className="text-slate-600">No cases yet. Guardian holds open them.</p>
        )}
        {cases.isSuccess && cases.data.items.length > 0 && (
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Cases, soonest SLA first</caption>
            <thead className="border-b border-slate-300 text-slate-600">
              <tr>
                <th scope="col" className="py-2 pr-4">
                  SLA
                </th>
                <th scope="col" className="py-2 pr-4">
                  Leading hypothesis
                </th>
                <th scope="col" className="py-2 pr-4">
                  Type
                </th>
                <th scope="col" className="py-2 pr-4">
                  Protocol
                </th>
                <th scope="col" className="py-2 pr-4 text-right">
                  At stake
                </th>
                <th scope="col" className="py-2 pr-4">
                  Status
                </th>
              </tr>
            </thead>
            <tbody>
              {cases.data.items.map((item) => (
                <tr key={item.id} className="border-b border-slate-100">
                  <td
                    className={`py-2 pr-4 ${item.sla_breached ? 'font-semibold text-red-700' : ''}`}
                  >
                    {slaLabel(item, now)}
                  </td>
                  <td className="py-2 pr-4">
                    <Link to={`/cases/${item.id}`} className="underline">
                      {item.leading_hypothesis_name}
                    </Link>
                  </td>
                  <td className="py-2 pr-4">{humanise(item.type)}</td>
                  <td className="py-2 pr-4">
                    {item.protocol === 'COERCION' ? (
                      <span className="rounded bg-amber-100 px-2 py-0.5 text-amber-900">
                        Coercion
                      </span>
                    ) : (
                      'Standard'
                    )}
                  </td>
                  <td className="py-2 pr-4 text-right">
                    <MoneyText paise={item.amount_at_stake_paise} />
                  </td>
                  <td className="py-2 pr-4">{humanise(item.status)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
