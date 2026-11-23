/** Display helpers shared by the routes and components (docs/18 §1). */

/** A KB or enum key as words, sentence case: `HOLD_FOR_REVIEW` → "Hold for review". */
export function humanise(key: string): string {
  const text = key.replaceAll('_', ' ').toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** A decision or baseline verdict as words. B2's middle band reads "OTP step-up" everywhere. */
export function decisionText(key: string): string {
  return key === 'OTP_STEP_UP' ? 'OTP step-up' : humanise(key);
}

const ACRONYMS = new Set([
  'OTP',
  'CRM',
  'KYC',
  'UPI',
  'ATO',
  'APP',
  'SMS',
  'IP',
  'LLM',
  'KB',
  'DD',
]);

/** A KB key as words, keeping its acronyms: OTP_STEP_UP → "OTP step up". */
export function keyText(key: string): string {
  return key
    .split('_')
    .map((w, i) =>
      ACRONYMS.has(w) ? w : i === 0 ? w.charAt(0) + w.slice(1).toLowerCase() : w.toLowerCase(),
    )
    .join(' ');
}

const SECONDS_PER_MINUTE = 60;
const MINUTES_PER_HOUR = 60;

/** A span of time, coarsest two units: 42 → "42 s", 1410 → "23 m 30 s"; null → "—". */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < SECONDS_PER_MINUTE) return `${String(Math.round(seconds))} s`;
  const m = Math.floor(seconds / SECONDS_PER_MINUTE);
  if (m < MINUTES_PER_HOUR) {
    return `${String(m)} m ${String(Math.round(seconds % SECONDS_PER_MINUTE)).padStart(2, '0')} s`;
  }
  return `${String(Math.floor(m / MINUTES_PER_HOUR))} h ${String(m % MINUTES_PER_HOUR).padStart(2, '0')} m`;
}
