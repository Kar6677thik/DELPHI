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
/** The lead bar writes a segment's percentage inside it only from this share up. */
const LEAD_LABEL_MIN_SHARE = 0.09;
const OTHER_COLOR = '#3A3A3E'; // a dark neutral, so it never reads as the fallback grey
const FALLBACK_COLOR = '#8A8C92'; // the muted grey, for a key the KB doesn't style (UNEXPLAINED)
/** The lightest a hypothesis colour is drawn: KB colours are set for light paper (docs/07 §4). */
const MIN_LIGHTNESS = 0.6;
const MAX_SATURATION = 0.75;

export interface Segment {
  key: string;
  name: string;
  color: string;
  share: number;
  /** For "Other": the hypotheses it groups, largest first. */
  members?: readonly { name: string; share: number }[];
}

/**
 * A KB colour as drawn on the dark ground: the same hue, lifted to at least 60% lightness, so
 * each hypothesis keeps its identity and reads against near-black. Display only; the KB's value
 * is unchanged (changing it is a KB change, docs/07).
 */
export function forDark(hex: string): string {
  if (!/^#[0-9a-fA-F]{6}$/.test(hex)) return hex;
  const [r, g, b] = [1, 3, 5].map((i) => Number.parseInt(hex.slice(i, i + 2), 16) / 255) as [
    number,
    number,
    number,
  ];
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  const d = max - min;
  const s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
  let h = 0;
  if (d !== 0) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
  }
  const L = Math.max(l, MIN_LIGHTNESS);
  const S = Math.min(s, MAX_SATURATION);
  const c = (1 - Math.abs(2 * L - 1)) * S;
  const x = c * (1 - Math.abs((h % 2) - 1));
  const m = L - c / 2;
  const sector = Math.floor(((h % 6) + 6) % 6);
  const [r1, g1, b1] = [
    [c, x, 0],
    [x, c, 0],
    [0, c, x],
    [0, x, c],
    [x, 0, c],
    [c, 0, x],
  ][sector] as [number, number, number];
  const hex2 = (v: number) =>
    Math.round((v + m) * 255)
      .toString(16)
      .padStart(2, '0');
  return `#${hex2(r1)}${hex2(g1)}${hex2(b1)}`.toUpperCase();
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
  return 1.05 / (l + 0.05) >= (l + 0.05) / 0.05 ? '#FFFFFF' : '#131217';
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
      color: forDark(styles[key]?.color ?? FALLBACK_COLOR),
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
  size = 'normal',
}: {
  probs: Readonly<Record<string, number>>;
  styles: HypothesisStyles;
  /** The bar's accessible name, e.g. "Posterior" or "Step 2". */
  label: string;
  /** `lead`: the screen's main figure (the phone's live panel): a taller bar, larger legend. */
  size?: 'normal' | 'lead';
}) {
  const lead = size === 'lead';
  const parts = segments(probs, styles);
  return (
    <div className="w-full min-w-0">
      {/* The bar repeats the legend's percentages; the legend is what assistive tech reads. */}
      <div
        aria-hidden="true"
        className={`flex w-full gap-px overflow-hidden rounded-[3px] ${lead ? 'h-14' : 'h-6'}`}
      >
        {parts.map((s) => (
          <div
            key={s.key}
            title={tooltip(s)}
            data-testid={`segment-${s.key}`}
            className={`figure flex min-w-0 items-center overflow-hidden whitespace-nowrap transition-[width] duration-500 ease-out ${lead ? 'justify-start px-3 text-[15px] font-medium' : 'justify-center px-1 text-xs font-medium'}`}
            style={{
              width: `${String(s.share * 100)}%`,
              backgroundColor: s.color,
              color: textColor(s.color),
            }}
          >
            {/* In the lead bar a narrow segment shows no clipped label; the legend has it. */}
            {(!lead || s.share >= LEAD_LABEL_MIN_SHARE) && (
              <span className="truncate">{percent(s.share)}</span>
            )}
          </div>
        ))}
      </div>
      <ul
        aria-label={label}
        className={`flex flex-wrap gap-x-4 gap-y-1 text-soft ${lead ? 'mt-3 text-[13px]' : 'mt-1.5 text-xs'}`}
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
              className="inline-block size-2 shrink-0 rounded-[2px]"
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
          <span className="break-words text-[13px] text-soft">{step.label}</span>
          <HypothesisBar probs={step.probs} styles={styles} label={step.label} />
        </li>
      ))}
    </ol>
  );
}
