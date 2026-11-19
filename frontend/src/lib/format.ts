/** Display helpers shared by the routes and components (docs/18 §1). */

/** A KB or enum key as words, sentence case: `HOLD_FOR_REVIEW` → "Hold for review". */
export function humanise(key: string): string {
  const text = key.replaceAll('_', ' ').toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}
