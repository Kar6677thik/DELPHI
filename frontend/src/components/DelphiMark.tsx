/**
 * DELPHI's mark: the oracle's eye. A ring for the possible explanations, and the one point the
 * engine has settled on, set off-centre the way a posterior leans. Drawn in the accent.
 */
export function DelphiMark({ size = 22 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden="true"
      className="shrink-0 text-accent"
    >
      <circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M2 12h20" stroke="currentColor" strokeWidth="1.5" opacity="0.35" />
      <circle cx="15.5" cy="12" r="3.5" fill="currentColor" />
    </svg>
  );
}
