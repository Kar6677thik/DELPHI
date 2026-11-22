import { Link } from 'react-router';

/**
 * A full-page state in the app shell: no access, not found, or a screen still being built
 * (docs/18 §3). A small mono code, the plain-words title, what it means, and where to go next.
 */
export function StatePage({
  code,
  title,
  children,
}: {
  code: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby="state-title" className="max-w-[640px] pt-[12vh]">
      <p className="figure text-[13px] tracking-[0.2em] text-muted">{code}</p>
      <h1 id="state-title" className="mt-3 text-[32px] leading-tight font-semibold tracking-tight">
        {title}
      </h1>
      <div className="mt-3 text-[15px] leading-relaxed text-soft">{children}</div>
      <p className="mt-8 text-[14px]">
        <Link
          to="/"
          className="text-soft underline decoration-control underline-offset-4 transition-colors hover:text-ink hover:decoration-soft"
        >
          Go to the Overview
        </Link>
      </p>
    </section>
  );
}
