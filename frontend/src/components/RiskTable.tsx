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
    <table className="w-full text-left text-[14px]">
      <caption className="sr-only">{caption}</caption>
      <thead className="border-b border-line text-[12px] text-muted">
        <tr>
          <th scope="col" className="py-2 pr-6 font-normal">
            Decision
          </th>
          <th scope="col" className="py-2 text-right font-normal">
            Expected loss
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([decision, paise]) => (
          <tr
            key={decision}
            aria-current={decision === chosen ? 'true' : undefined}
            className={`border-b border-line last:border-b-0 ${
              decision === chosen
                ? 'bg-raised text-ink [&>td:first-child]:pl-3 [&>td:first-child]:shadow-[inset_2px_0_0_var(--color-accent)]'
                : 'text-soft'
            }`}
          >
            <td className="py-2.5 pr-6">
              {decisionText(decision)}
              {decision === chosen && (
                <span className="ml-2 text-[12px] text-accent">{marker}</span>
              )}
            </td>
            <td className="figure py-2.5 pr-3 text-right">
              <MoneyText paise={Math.round(paise)} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
