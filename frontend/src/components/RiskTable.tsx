import { MoneyText } from './MoneyText';
import { decisionText } from '../lib/format';

/**
 * The decision's risk table (docs/18 §3, docs/08 §6): each decision's expected loss in ₹, with
 * one row highlighted and marked in text too (docs/18 §5: never colour alone). Once a decision is
 * taken that row is marked "(decision taken)": a fail-safe decision need not be the lowest-risk
 * one. Before that, the lowest-risk row is marked "(recommended)".
 */

export function RiskTable({
  risks,
  taken,
  caption = 'Expected loss by decision',
}: {
  risks: Readonly<Record<string, number>>;
  /** The decision the session took, if any. */
  taken?: string | null;
  caption?: string;
}) {
  const rows = Object.entries(risks).sort((a, b) => a[1] - b[1] || a[0].localeCompare(b[0]));
  const chosen = taken ?? rows[0]?.[0];
  const marker = taken ? '(decision taken)' : '(recommended)';
  return (
    <table className="text-left text-sm">
      <caption className="sr-only">{caption}</caption>
      <thead className="border-b border-slate-300 text-slate-600">
        <tr>
          <th scope="col" className="py-1 pr-6">
            Decision
          </th>
          <th scope="col" className="py-1 pr-3 text-right">
            Expected loss
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([decision, paise]) => (
          <tr
            key={decision}
            aria-current={decision === chosen ? 'true' : undefined}
            className={`border-b border-slate-100 ${decision === chosen ? 'bg-emerald-50 font-semibold' : ''}`}
          >
            <td className="py-1 pr-6">
              {decisionText(decision)}
              {decision === chosen && <span className="ml-2 text-emerald-800">{marker}</span>}
            </td>
            <td className="py-1 pr-3 text-right">
              <MoneyText paise={Math.round(paise)} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
