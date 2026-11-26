/** The F3 kill chain (docs/02): the stages in order. */
export const F3_STAGES = ['INITIAL_ACCESS', 'POSITIONING', 'EXECUTION', 'MONETIZATION'] as const;

const LABELS: Readonly<Record<(typeof F3_STAGES)[number], string>> = {
  INITIAL_ACCESS: 'Initial access',
  POSITIONING: 'Positioning',
  EXECUTION: 'Execution',
  MONETIZATION: 'Monetization',
};

/** How far along the kill chain `furthest` is: 1–4, or 0 when no stage is known. */
export function stagesReached(furthest: string | null): number {
  const at = F3_STAGES.indexOf(furthest as (typeof F3_STAGES)[number]);
  return at < 0 ? 0 : at + 1;
}

/**
 * The F3 stage strip (docs/18 §2 `/campaigns`): the four stages in order, filled up to the
 * furthest one the campaign's cases reached. With no stage known it says so in words, so the
 * strip never reads as "nothing happened".
 */
export function F3Strip({
  furthest,
  compact = false,
}: {
  furthest: string | null;
  compact?: boolean;
}) {
  const reached = stagesReached(furthest);
  const summary =
    reached === 0
      ? 'F3 stage not known yet'
      : `F3 stage: ${LABELS[F3_STAGES[reached - 1] as (typeof F3_STAGES)[number]]}, ${String(reached)} of 4`;
  return (
    <div className="flex items-center gap-2">
      <ol aria-label={summary} className="flex gap-1">
        {F3_STAGES.map((stage, i) => (
          <li
            key={stage}
            title={LABELS[stage]}
            className={`h-2 rounded-sm ${compact ? 'w-5' : 'w-10'} ${
              i < reached ? 'bg-accent' : 'border border-control bg-transparent'
            }`}
          >
            <span className="sr-only">
              {LABELS[stage]}: {i < reached ? 'reached' : 'not reached'}
            </span>
          </li>
        ))}
      </ol>
      {!compact && <span className="text-[12px] text-muted">{summary}</span>}
    </div>
  );
}
