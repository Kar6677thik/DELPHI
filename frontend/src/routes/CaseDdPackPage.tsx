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
      <table className="text-left text-[13px]">
        <tbody>
          {Object.entries(value).map(([key, item]) => (
            <tr key={key} className="border-b border-line align-top last:border-0">
              <th scope="row" className="py-1.5 pr-6 font-normal text-muted">
                {humanise(key)}
              </th>
              <td className="py-1.5 text-soft">
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
    <section
      aria-labelledby={titleId}
      className="break-inside-avoid rounded-md bg-surface p-8 print:bg-transparent print:p-0"
    >
      <h2 id={titleId} className="flex flex-wrap items-baseline gap-x-3 text-[18px] text-ink">
        <span>
          <span className="figure text-muted">{section.number}.</span> {section.title}
        </span>
        <span className={`text-[12px] ${empty ? 'text-negative' : 'text-muted'}`}>
          {section.required ? 'Required' : 'Optional'}
          {empty && ' · missing'}
        </span>
      </h2>
      {section.note && <p className="mt-1 text-[13px] text-muted">{section.note}</p>}
      <div className="mt-4 text-[14px] text-soft">
        {section.content === NOT_APPLICABLE ? (
          <span className="text-muted">{NOT_APPLICABLE}</span>
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
    <section aria-labelledby="ddpack-title" className="max-w-[1040px] print:text-black">
      <p className="text-[13px] text-muted print:hidden">
        <Link to="/cases" className="transition-colors hover:text-ink">
          Case queue
        </Link>{' '}
        /{' '}
        <Link to={`/cases/${id}`} className="transition-colors hover:text-ink">
          Case
        </Link>{' '}
        /
      </p>
      <h1 id="ddpack-title" className="mt-1 text-[26px] font-semibold tracking-tight">
        Due-diligence pack
      </h1>
      {pack.isPending && <p className="mt-4 text-[14px] text-muted">Loading the pack…</p>}
      {pack.isError && (
        <div className="mt-4">
          <ProblemAlert error={pack.error} title="Could not load the DD pack" />
        </div>
      )}
      {pack.isSuccess && (
        <div className="mt-1 space-y-6">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-[14px]">
            <p role="status" className="text-muted">
              Case <span className="figure text-soft">{pack.data.caseId}</span> ·{' '}
              <span className={pack.data.complete ? 'text-positive' : 'text-warning'}>
                {pack.data.complete ? 'Complete' : 'Incomplete'}
              </span>
            </p>
            <button
              type="button"
              onClick={() => {
                window.print();
              }}
              className="ml-auto rounded-full bg-ink px-4 py-1.5 text-[13px] font-semibold text-ground transition-colors hover:bg-soft print:hidden"
            >
              Print or save as PDF
            </button>
            <a
              className="text-[13px] text-soft underline decoration-control underline-offset-4 transition-colors hover:text-ink print:hidden"
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
