import { Link, useParams } from 'react-router';

import { type DdPackSection, useCaseDdPack } from '../api/cases';
import { API_BASE } from '../api/client';
import { ProblemAlert } from '../components/ProblemAlert';
import { humanise } from '../lib/format';

/**
 * The case's due-diligence pack (docs/18 §2 `/cases/:id/dd-pack`, docs/09 §7): every section in
 * order with its completeness, readable on screen and printable (the app's navigation and this
 * page's buttons are hidden in print). Opening it records DD_PACK_VIEWED (docs/09 §5).
 */

const NOT_APPLICABLE = 'Not applicable';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** A section's content as the server's printable page shows it: tables, lists and text. */
export function Value({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span>—</span>;
  if (Array.isArray(value)) {
    const items: unknown[] = value;
    if (items.length === 0) return <span>None</span>;
    return (
      <ol className="list-decimal space-y-1 pl-5">
        {items.map((item, i) => (
          <li key={i}>
            <Value value={item} />
          </li>
        ))}
      </ol>
    );
  }
  if (isRecord(value)) {
    return (
      <table className="text-left text-sm">
        <tbody>
          {Object.entries(value).map(([key, item]) => (
            <tr key={key} className="border-b border-slate-100 align-top">
              <th scope="row" className="py-1 pr-4 font-normal text-slate-600">
                {humanise(key)}
              </th>
              <td className="py-1">
                <Value value={item} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  if (typeof value === 'boolean') return <span>{value ? 'Yes' : 'No'}</span>;
  const text =
    typeof value === 'string' || typeof value === 'number' ? String(value) : JSON.stringify(value);
  return <span className="break-words">{text}</span>;
}

function Section({ section }: { section: DdPackSection }) {
  const titleId = `dd-section-${String(section.number)}`;
  const empty = section.required && (section.content === null || section.content === undefined);
  return (
    <section aria-labelledby={titleId} className="break-inside-avoid">
      <h2 id={titleId} className="text-lg font-semibold">
        {section.number}. {section.title}
        <span className="ml-2 text-xs font-normal text-slate-600">
          {section.required ? 'Required' : 'Optional'}
          {empty && ' · missing'}
        </span>
      </h2>
      {section.note && <p className="mt-1 text-sm italic text-slate-600">{section.note}</p>}
      <div className="mt-2 text-sm">
        {section.content === NOT_APPLICABLE ? (
          <span className="text-slate-600">{NOT_APPLICABLE}</span>
        ) : (
          <Value value={section.content} />
        )}
      </div>
    </section>
  );
}

export function CaseDdPackPage() {
  const { id = '' } = useParams();
  const pack = useCaseDdPack(id);
  return (
    <section aria-labelledby="ddpack-title" className="print:text-black">
      <p className="text-sm print:hidden">
        <Link to={`/cases/${id}`} className="underline">
          Back to the case
        </Link>
      </p>
      <h1 id="ddpack-title" className="mt-1 text-2xl font-semibold">
        Due-diligence pack
      </h1>
      {pack.isPending && <p className="mt-4 text-slate-600">Loading the pack…</p>}
      {pack.isError && (
        <div className="mt-4">
          <ProblemAlert error={pack.error} title="Could not load the DD pack" />
        </div>
      )}
      {pack.isSuccess && (
        <div className="mt-4 space-y-6">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <p role="status" className="font-medium">
              Case {pack.data.caseId} · {pack.data.complete ? 'Complete' : 'Incomplete'}
            </p>
            <button
              type="button"
              onClick={() => {
                window.print();
              }}
              className="rounded-full bg-ink px-3 py-1 font-semibold text-ground hover:bg-soft print:hidden"
            >
              Print or save as PDF
            </button>
            <a
              className="underline print:hidden"
              href={`${API_BASE}/cases/${encodeURIComponent(id)}/dd-pack?format=html`}
              target="_blank"
              rel="noreferrer"
            >
              Open the printable page
            </a>
          </div>
          {pack.data.sections.map((section) => (
            <Section key={section.number} section={section} />
          ))}
        </div>
      )}
    </section>
  );
}
