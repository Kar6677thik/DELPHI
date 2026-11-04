/** Where to go after sign-in: a same-origin path only, never another site (open redirect). */
export function safeNext(next: string | null): string {
  if (next?.startsWith('/') && !next.startsWith('//') && !next.startsWith('/\\')) return next;
  return '/';
}
