/**
 * A narrative with its citations as chips (docs/18 §3, docs/15 §4). The text cites evidence
 * records as `[<record_id>]`; each citation renders as a chip that scrolls to the cited record's
 * fact table and highlights it. The label says who wrote the text, so AI-written text is never
 * mistaken for a fact (docs/09 §2 item 5).
 */

import { useTrack } from '../api/traces';

const CITATION = /\[([0-9a-fA-F-]{36})\]/g;

export type NarrativePart = { text: string } | { citation: string };

/** The narrative split into text and citations, in order. */
export function parseNarrative(narrative: string): NarrativePart[] {
  const parts: NarrativePart[] = [];
  let last = 0;
  for (const match of narrative.matchAll(CITATION)) {
    const before = narrative.slice(last, match.index).trimEnd();
    if (before) parts.push({ text: before });
    parts.push({ citation: match[1] ?? '' });
    last = match.index + match[0].length;
  }
  const rest = narrative.slice(last).trim();
  if (rest) parts.push({ text: rest });
  return parts;
}

/** The DOM id of a record's fact table, the target of its citation chips. */
export function factsId(recordId: string): string {
  return `facts-${recordId}`;
}

const HIGHLIGHT = ['ring-2', 'ring-amber-400'];
const HIGHLIGHT_MS = 2_000;

/** Scroll to the cited fact table, highlight it for a moment and move focus there. */
export function showFacts(recordId: string): void {
  const target = document.getElementById(factsId(recordId));
  if (!target) return;
  target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  target.focus({ preventScroll: true });
  target.classList.add(...HIGHLIGHT);
  setTimeout(() => {
    target.classList.remove(...HIGHLIGHT);
  }, HIGHLIGHT_MS);
}

export function narrativeLabel(source: string | null): string {
  return source === 'LLM' ? 'AI-written summary' : 'Template summary';
}

export function NarrativeWithCitations({
  narrative,
  source,
  citationLabel,
  recordId,
}: {
  narrative: string;
  source: string | null;
  /** The evidence record the narrative is about: opening a citation records NARRATIVE_EXPANDED
   * for it (docs/09 §5). */
  recordId?: string;
  /** The chip's text for a cited record (e.g. its source's name); undefined when the record
   * is not on the page, which renders the citation as plain text instead of a dead button. */
  citationLabel: (recordId: string) => string | undefined;
}) {
  const track = useTrack();
  return (
    <figure className="text-sm">
      <figcaption className="text-xs font-medium uppercase tracking-wide text-slate-600">
        {narrativeLabel(source)}
      </figcaption>
      <p className="mt-1">
        {parseNarrative(narrative).map((part, i) => {
          if ('text' in part) return <span key={i}>{part.text} </span>;
          const label = citationLabel(part.citation);
          if (label === undefined) {
            return (
              <span key={i} className="mx-0.5 text-xs text-slate-600">
                (cited record not shown)
              </span>
            );
          }
          return (
            <button
              key={i}
              type="button"
              onClick={() => {
                showFacts(part.citation);
                if (recordId) track('NARRATIVE_EXPANDED', recordId);
              }}
              className="mx-0.5 rounded-full border border-slate-300 bg-slate-100 px-2 py-0.5 text-xs text-slate-800 hover:bg-slate-200 focus-visible:outline-2 focus-visible:outline-slate-900"
              aria-label={`Show the facts: ${label}`}
            >
              {label}
            </button>
          );
        })}
      </p>
    </figure>
  );
}
