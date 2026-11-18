import { MoneyText } from './MoneyText';

/**
 * The decision's risk table (docs/18 §3, docs/08 §6): each decision's expected loss in ₹, the
 * recommended (lowest-risk, or the one taken) decision highlighted, and marked in text too
 * (docs/18 §5: never colour alone).
 */
function humanise(key: string): string {
  const text = key.replaceAll('_', ' ').toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function RiskTable({
  risks,
  recommended,
  caption = 'Expected loss by decision',
}: {
  risks: Readonly<Record<string, number>>;
  recommended?: string | null;
  caption?: string;
}) {
  const rows = Object.entries(risks).sort((a, b) => a[1] - b[1] || a[0].localeCompare(b[0]));
  const chosen = recommended ?? rows[0]?.[0];
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
              {humanise(decision)}
              {decision === chosen && <span className="ml-2 text-emerald-800">(recommended)</span>}
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
