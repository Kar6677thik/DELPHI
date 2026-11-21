/**
 * The hypothesis bar (docs/18 §3, docs/08 §6): a horizontal bar stacked to 100%, one segment per
 * hypothesis in its fixed KB colour (docs/07 §4). Every segment of at least 4% shows its label
 * and percentage as text, so no information is carried by colour alone (docs/18 §5): the segment
 * carries its percentage, and a legend under the bar, in the same order, carries the label and
 * percentage in full (it wraps, so long KB names are never cut). Smaller segments are grouped into
 * one "Other" segment whose members are listed in its tooltip and for screen readers.
 * `HypothesisTimeline` stacks one bar per step.
 */

/** A hypothesis's display name and colour, from the session's pinned KB version. */
export interface HypothesisStyle {
  name: string;
  color: string;
}

export type HypothesisStyles = Readonly<Record<string, HypothesisStyle>>;

/** docs/18 §3: segments under this share are grouped into "Other". */
export const LABEL_MIN_SHARE = 0.04;
const OTHER_COLOR = '#1E293B'; // slate-800: dark, so it never reads as the light fallback grey
const FALLBACK_COLOR = '#94A3B8'; // slate-400, for a key the KB doesn't style (e.g. UNEXPLAINED)

export interface Segment {
  key: string;
  name: string;
  color: string;
  share: number;
  /** For "Other": the hypotheses it groups, largest first. */
  members?: readonly { name: string; share: number }[];
}

function luminance(hex: string): number {
  const channel = (i: number) => {
    const c = Number.parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

/** White or near-black, whichever contrasts more with the segment (docs/18 §5: ≥ 4.5:1). */
export function textColor(background: string): string {
  if (!/^#[0-9a-fA-F]{6}$/.test(background)) return '#FFFFFF';
  const l = luminance(background);
  return 1.05 / (l + 0.05) >= (l + 0.05) / 0.05 ? '#FFFFFF' : '#0F172A';
}

export function percent(share: number): string {
  return `${(share * 100).toFixed(share >= 0.1 ? 0 : 1)}%`;
}

/** The bar's segments, largest first, with the small ones grouped (docs/18 §3). */
export function segments(
  probs: Readonly<Record<string, number>>,
  styles: HypothesisStyles,
): Segment[] {
  const total = Object.values(probs).reduce((a, b) => a + b, 0);
  const ranked = Object.entries(probs)
    .map(([key, p]) => ({
      key,
      name: styles[key]?.name ?? key,
      color: styles[key]?.color ?? FALLBACK_COLOR,
      share: total > 0 ? p / total : 0,
    }))
    .sort((a, b) => b.share - a.share || a.key.localeCompare(b.key));
  const big = ranked.filter((s) => s.share >= LABEL_MIN_SHARE);
  const small = ranked.filter((s) => s.share < LABEL_MIN_SHARE && s.share > 0);
  if (small.length === 0) return big;
  const other: Segment = {
    key: 'OTHER',
    name: 'Other',
    color: OTHER_COLOR,
    share: small.reduce((a, s) => a + s.share, 0),
    members: small.map((s) => ({ name: s.name, share: s.share })),
  };
  return [...big, other];
}

function tooltip(segment: Segment): string {
  if (!segment.members) return `${segment.name}: ${percent(segment.share)}`;
  return segment.members.map((m) => `${m.name}: ${percent(m.share)}`).join('\n');
}

export function HypothesisBar({
  probs,
  styles,
  label,
}: {
  probs: Readonly<Record<string, number>>;
  styles: HypothesisStyles;
  /** The bar's accessible name, e.g. "Posterior" or "Step 2". */
  label: string;
}) {
  const parts = segments(probs, styles);
  return (
    <div className="w-full min-w-0">
      {/* The bar repeats the legend's percentages; the legend is what assistive tech reads. */}
      <div aria-hidden="true" className="flex h-6 w-full overflow-hidden rounded">
        {parts.map((s) => (
          <div
            key={s.key}
            title={tooltip(s)}
            data-testid={`segment-${s.key}`}
            className="flex min-w-0 items-center justify-center overflow-hidden whitespace-nowrap px-1 text-xs font-medium"
            style={{
              width: `${String(s.share * 100)}%`,
              backgroundColor: s.color,
              color: textColor(s.color),
            }}
          >
            <span className="truncate">{percent(s.share)}</span>
          </div>
        ))}
      </div>
      <ul
        aria-label={label}
        className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-slate-700"
      >
        {parts.map((s) => (
          <li
            key={s.key}
            title={tooltip(s)}
            data-testid={`legend-${s.key}`}
            className="flex items-center gap-1"
          >
            <span
              aria-hidden="true"
              className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm"
              style={{ backgroundColor: s.color }}
            />
            <span>
              {s.name} {percent(s.share)}
              {s.members && <span className="sr-only">: {tooltip(s).replaceAll('\n', ', ')}</span>}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** One bar per step (docs/18 §3 `timeline` variant): the prior, then each observation. */
export function HypothesisTimeline({
  steps,
  styles,
}: {
  steps: readonly { label: string; probs: Readonly<Record<string, number>> }[];
  styles: HypothesisStyles;
}) {
  return (
    <ol aria-label="Posterior by step" className="space-y-2">
      {steps.map((step, i) => (
        <li
          key={`${String(i)}-${step.label}`}
          className="grid grid-cols-[10rem_1fr] items-start gap-3"
        >
          <span className="break-words text-sm text-slate-700">{step.label}</span>
          <HypothesisBar probs={step.probs} styles={styles} label={step.label} />
        </li>
      ))}
    </ol>
  );
}
