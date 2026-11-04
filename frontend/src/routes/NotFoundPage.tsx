import { Link } from 'react-router';

export function NotFoundPage() {
  return (
    <section aria-labelledby="not-found-title">
      <h1 id="not-found-title" className="text-2xl font-semibold">
        Page not found
      </h1>
      <p className="mt-2 text-slate-700">
        <Link to="/" className="underline">
          Go to the Overview
        </Link>
      </p>
    </section>
  );
}
