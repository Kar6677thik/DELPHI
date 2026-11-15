/**
 * Money on screen (docs/18 §3): integer paise as ₹ in the Indian grouping, always with paise
 * (₹4,80,000.00). The rupees and paise are split with integer arithmetic, so no float rounding
 * can move a paisa (ADR-0014: money is integer paise).
 */
const RUPEES = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0, useGrouping: true });

export function formatPaise(paise: number): string {
  if (!Number.isSafeInteger(paise))
    throw new RangeError(`not a whole number of paise: ${String(paise)}`);
  const sign = paise < 0 ? '-' : '';
  const whole = Math.abs(paise);
  const rupees = Math.floor(whole / 100);
  const rest = whole % 100;
  return `${sign}₹${RUPEES.format(rupees)}.${String(rest).padStart(2, '0')}`;
}

export function MoneyText({ paise }: { paise: number }) {
  return <span className="tabular-nums">{formatPaise(paise)}</span>;
}
