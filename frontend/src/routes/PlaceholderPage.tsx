/** A screen from docs/18 §2 that is routed and permission-gated but not built yet. */
export function PlaceholderPage({ title }: { title: string }) {
  return (
    <section aria-labelledby="page-title">
      <h1 id="page-title" className="text-2xl font-semibold">
        {title}
      </h1>
      <p className="mt-2 text-slate-700">This screen is not built yet.</p>
    </section>
  );
}
