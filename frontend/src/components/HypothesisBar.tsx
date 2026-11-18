/**
 * The hypothesis bar (docs/18 §3, docs/08 §6): a horizontal bar stacked to 100%, one segment per
 * hypothesis in its fixed KB colour (docs/07 §4). Every segment of at least 4% carries its label
 * and percentage, so no information is carried by colour alone (docs/18 §5); smaller segments are
 * grouped into one "Other" segment whose tooltip lists them. `HypothesisTimeline` stacks one bar
 * per step.
 */

/** A hypothesis's display name and colour, from the session's pinned KB version. */
export interface HypothesisStyle {
  name: string;
  color: string;
}

export type HypothesisStyles = Readonly<Record<string, HypothesisStyle>>;

/** docs/18 §3: segments under this share are grouped into "Other". */
export const LABEL_MIN_SHARE = 0.04;
const OTHER_COLOR = '#475569'; // slate-600
const FALLBACK_COLOR = '#64748B'; // slate-500, for a key the KB doesn't style (e.g. UNEXPLAINED)

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
  const summary = parts.map((s) => `${s.name} ${percent(s.share)}`).join(', ');
  return (
    <div
      role="img"
      aria-label={`${label}: ${summary}`}
      className="flex h-8 w-full overflow-hidden rounded"
    >
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
          <span aria-hidden="true" className="truncate">
            {s.name} {percent(s.share)}
          </span>
        </div>
      ))}
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
    <ol className="space-y-2">
      {steps.map((step, i) => (
        <li
          key={`${String(i)}-${step.label}`}
          className="grid grid-cols-[10rem_1fr] items-center gap-3"
        >
          <span className="truncate text-sm text-slate-700">{step.label}</span>
          <HypothesisBar probs={step.probs} styles={styles} label={step.label} />
        </li>
      ))}
    </ol>
  );
}
