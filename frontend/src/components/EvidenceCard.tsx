import type { EvidenceRecord } from '../api/types';
import { humanise } from '../lib/format';
import { type HypothesisStyles, percent } from './HypothesisBar';
import { formatPaise } from './MoneyText';
import { NarrativeWithCitations, factsId } from './NarrativeWithCitations';

/**
 * One fetched evidence record (docs/09 §2 item 5): the outcome chip, the posterior shift for the
 * top two hypotheses, the deterministic fact table, and the narrative with its citations.
 */

/** The two hypotheses most likely after the record, with their shares before and after it. */
export function posteriorShift(
  record: Pick<EvidenceRecord, 'posterior_before' | 'posterior_after'>,
): { key: string; before: number; after: number }[] {
  return Object.entries(record.posterior_after)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 2)
    .map(([key, after]) => ({ key, before: record.posterior_before[key] ?? 0, after }));
}

/** A fact's value as text: `*_paise` facts as ₹, `*_share` fractions as %, missing ones "—". */
export function factText(key: string, value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (key.endsWith('_paise') && typeof value === 'number' && Number.isSafeInteger(value)) {
    return formatPaise(value);
  }
  if (key.endsWith('_share') && typeof value === 'number') return percent(value);
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number' || typeof value === 'string') return String(value);
  return JSON.stringify(value);
}

function factName(key: string): string {
  return humanise(key.replace(/_paise$/, ''));
}

export function EvidenceCard({
  record,
  styles,
  citationLabel,
}: {
  record: EvidenceRecord;
  styles: HypothesisStyles;
  citationLabel: (recordId: string) => string | undefined;
}) {
  const titleId = `evidence-${record.id}`;
  return (
    <article aria-labelledby={titleId} className="rounded-md border border-slate-200 p-4">
      <header className="flex flex-wrap items-center gap-2">
        <h3 id={titleId} className="font-semibold">
          {humanise(record.source_key)}
        </h3>
        <span className="rounded bg-raised px-2 py-0.5 text-xs font-medium text-ink">
          {humanise(record.outcome)}
        </span>
      </header>

      <ul aria-label="Posterior shift" className="mt-2 text-sm text-slate-700">
        {posteriorShift(record).map((s) => (
          <li key={s.key}>
            {styles[s.key]?.name ?? s.key}: {percent(s.before)} → {percent(s.after)}
          </li>
        ))}
      </ul>

      <table
        id={factsId(record.id)}
        tabIndex={-1}
        className="mt-3 rounded text-left text-sm transition-shadow"
      >
        <caption className="sr-only">Facts of {humanise(record.source_key)}</caption>
        <tbody>
          {Object.entries(record.facts).map(([key, value]) => (
            <tr key={key} className="border-b border-slate-100">
              <th scope="row" className="py-1 pr-6 font-normal text-slate-600">
                {factName(key)}
              </th>
              <td className="py-1 pr-3">{factText(key, value)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="mt-3">
        {record.narrative ? (
          <NarrativeWithCitations
            narrative={record.narrative}
            source={record.narrative_source}
            citationLabel={citationLabel}
          />
        ) : (
          <p className="text-sm text-slate-600">The summary is being written…</p>
        )}
      </div>
    </article>
  );
}
