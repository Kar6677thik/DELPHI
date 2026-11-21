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
