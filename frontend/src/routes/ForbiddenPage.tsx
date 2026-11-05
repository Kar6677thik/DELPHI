export function ForbiddenPage() {
  return (
    <section aria-labelledby="forbidden-title">
      <h1 id="forbidden-title" className="text-2xl font-semibold">
        No access
      </h1>
      <p className="mt-2 text-slate-700">Your role does not include this screen.</p>
    </section>
  );
}
