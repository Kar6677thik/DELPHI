import { MoneyText } from './MoneyText';
import { humanise } from '../lib/format';

/**
 * One planning step's candidates (docs/18 §3, docs/08 §6): source, eligible or why not, cost,
 * VoI, net value, settle %, and the selected badge (GREEDY/EXPLORE with its propensity). The
 * rows are as the Guardian stored them (`probe_runs.candidates`, docs/17).
 */
export interface Candidate {
  source_key: string;
  eligible: boolean;
  ineligible_reason?: string | null;
  cost_paise?: number | null;
  voi_myopic_paise?: number | null;
  net_value_paise?: number | null;
  settle_pct?: number | null;
  selected?: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function num(value: unknown): number | null {
  return typeof value === 'number' ? value : null;
}

/**
 * The stored candidates as table rows. The api types them as open objects for now
 * (`list[dict[str, Any]]`), so each field is checked rather than cast; #198 adds a typed
 * candidate model to the api, after which this goes.
 */
export function parseCandidates(rows: readonly unknown[]): Candidate[] {
  return rows.flatMap((r) =>
    isRecord(r) && typeof r.source_key === 'string'
      ? [
          {
            source_key: r.source_key,
            eligible: r.eligible !== false,
            ineligible_reason: typeof r.ineligible_reason === 'string' ? r.ineligible_reason : null,
            cost_paise: num(r.cost_paise),
            voi_myopic_paise: num(r.voi_myopic_paise),
            net_value_paise: num(r.net_value_paise),
            settle_pct: num(r.settle_pct),
            selected: r.selected === true,
          },
        ]
      : [],
  );
}

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
    <table className="w-full text-left text-sm">
      <caption className="sr-only">{caption}</caption>
      <thead className="border-b border-slate-300 text-slate-600">
        <tr>
          <th scope="col" className="py-1 pr-3">
            Source
          </th>
          <th scope="col" className="py-1 pr-3">
            Eligible
          </th>
          <th scope="col" className="py-1 pr-3 text-right">
            Cost
          </th>
          <th scope="col" className="py-1 pr-3 text-right">
            VoI
          </th>
          <th scope="col" className="py-1 pr-3 text-right">
            Net value
          </th>
          <th scope="col" className="py-1 pr-3 text-right">
            Settle %
          </th>
          <th scope="col" className="py-1 pr-3">
            Selected
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((c) => (
          <tr
            key={c.source_key}
            className={`border-b border-slate-100 ${c.selected ? 'bg-sky-50 font-medium' : ''}`}
          >
            <td className="py-1 pr-3">{sourceNames[c.source_key] ?? humanise(c.source_key)}</td>
            <td className="py-1 pr-3">
              {c.eligible ? 'Yes' : `No: ${humanise(c.ineligible_reason ?? 'not eligible')}`}
            </td>
            <td className="py-1 pr-3 text-right">
              <Money paise={c.cost_paise} />
            </td>
            <td className="py-1 pr-3 text-right">
              <Money paise={c.voi_myopic_paise} />
            </td>
            <td className="py-1 pr-3 text-right">
              <Money paise={c.net_value_paise} />
            </td>
            <td className="py-1 pr-3 text-right tabular-nums">
              {c.settle_pct === null || c.settle_pct === undefined
                ? '—'
                : `${c.settle_pct.toFixed(0)}%`}
            </td>
            <td className="py-1 pr-3">
              {c.selected && (
                <span className="rounded bg-sky-100 px-2 py-0.5 text-sky-900">
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
