import type { Candidate } from '../api/types';
import { MoneyText } from './MoneyText';
import { humanise, keyText } from '../lib/format';

/**
 * One planning step's candidates (docs/18 §3, docs/08 §6): source, eligible or why not, cost,
 * VoI, net value, settle %, and the selected badge (GREEDY/EXPLORE with its propensity). The
 * rows are the api's typed `CandidateView`s (`probe_runs.candidates`, docs/17, #198).
 */
function Money({ paise }: { paise: number | null | undefined }) {
  return paise === null || paise === undefined ? <span>—</span> : <MoneyText paise={paise} />;
}

export function CandidatesTable({
  candidates,
  selectedBy,
  propensity,
  caption,
  sourceNames = {},
}: {
  candidates: readonly Candidate[];
  /** The KB's display name of each source (docs/07), from the session's pinned KB version. */
  sourceNames?: Readonly<Record<string, string>>;
  selectedBy?: string | null;
  propensity?: number | null;
  caption: string;
}) {
  const rows = [...candidates].sort(
    (a, b) =>
      Number(b.eligible) - Number(a.eligible) ||
      (b.net_value_paise ?? -Infinity) - (a.net_value_paise ?? -Infinity) ||
      a.source_key.localeCompare(b.source_key),
  );
  return (
    <table className="w-full min-w-[640px] text-left text-[14px]">
      <caption className="sr-only">{caption}</caption>
      <thead className="border-b border-line text-[12px] text-muted">
        <tr>
          <th scope="col" className="py-2 pr-4 font-normal">
            Source
          </th>
          <th scope="col" className="py-2 pr-4 font-normal">
            Eligible
          </th>
          <th scope="col" className="py-2 pr-4 text-right font-normal">
            Cost
          </th>
          <th scope="col" className="py-2 pr-4 text-right font-normal">
            <abbr title="Value of information: what the answer is worth" className="no-underline">
              Value
            </abbr>
          </th>
          <th scope="col" className="py-2 pr-4 text-right font-normal">
            Net value
          </th>
          <th scope="col" className="py-2 pr-4 text-right font-normal">
            Settle %
          </th>
          <th scope="col" className="py-2 pr-4 font-normal">
            Selected
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((c) => (
          <tr
            key={c.source_key}
            className={`border-b border-line last:border-b-0 ${
              c.selected
                ? 'bg-raised text-ink [&>td:first-child]:shadow-[inset_2px_0_0_var(--color-accent)] [&>td:first-child]:pl-3'
                : c.eligible
                  ? 'text-soft'
                  : 'text-muted'
            }`}
          >
            <td className="py-2.5 pr-4">{sourceNames[c.source_key] ?? keyText(c.source_key)}</td>
            <td className="py-2.5 pr-4">
              {c.eligible ? 'Yes' : `No: ${humanise(c.ineligible_reason ?? 'not eligible')}`}
            </td>
            <td className="figure py-2.5 pr-4 text-right">
              <Money paise={c.cost_paise} />
            </td>
            <td className="figure py-2.5 pr-4 text-right">
              <Money paise={c.voi_myopic_paise} />
            </td>
            <td className="figure py-2.5 pr-4 text-right">
              <Money paise={c.net_value_paise} />
            </td>
            <td className="figure py-2.5 pr-4 text-right">
              {c.settle_pct === null || c.settle_pct === undefined
                ? '—'
                : `${c.settle_pct.toFixed(0)}%`}
            </td>
            <td className="py-2.5 pr-4">
              {c.selected && (
                <span className="rounded-sm border border-accent/50 px-2 py-0.5 text-[12px] text-accent">
                  {selectedBy ?? 'Selected'}
                  {propensity !== null &&
                    propensity !== undefined &&
                    ` · p=${propensity.toFixed(2)}`}
                </span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
