import { useState } from 'react';
import { useSearchParams } from 'react-router';

import { API_BASE } from '../api/client';
import {
  afterAnswer,
  type KbContent,
  type KbHypothesis,
  type KbSource,
  separation,
  useKbContent,
  useKbDiff,
  useKbVersions,
} from '../api/kb';
import type { KbStatus, KbVersionPage, KbVersionView } from '../api/types';
import { forDark } from '../components/HypothesisBar';
import { ProblemAlert } from '../components/ProblemAlert';
import { humanise, keyText } from '../lib/format';

/**
 * The knowledge base (docs/18 §2 `/kb`, docs/07, part of #119): the rules DELPHI reasons with,
 * read-only. The versions and their lineage, the explanations it weighs, every signal with how
 * strongly it separates them, and, for the signal chosen, its likelihood table and what each
 * answer would do to an even belief. Promotion and rollback arrive with the shadow evaluations.
 */

const PERCENT = 100;
const GROUPS = [
  {
    key: 'passive',
    title: 'Passive checks',
    note: 'Read silently while the payment waits',
    test: (s: KbSource) => s.kind === 'PROBE' && s.mode === 'PASSIVE',
  },
  {
    key: 'active',
    title: 'Asked of the customer',
    note: 'Questions and step-ups, each with a friction cost',
    test: (s: KbSource) => s.kind === 'PROBE' && s.mode !== 'PASSIVE',
  },
  {
    key: 'evidence',
    title: 'Investigator evidence',
    note: 'Fetched on a case, after the hold',
    test: (s: KbSource) => s.kind === 'EVIDENCE',
  },
] as const;

const STATUS_TONE: Readonly<Record<KbStatus, string>> = {
  ACTIVE: 'text-positive',
  SHADOW: 'text-accent',
  CANDIDATE: 'text-warning',
  RETIRED: 'text-muted',
  REJECTED: 'text-muted',
};

const DATE = new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeZone: 'UTC' });

function pct(p: number): string {
  if (p > 0 && p < 0.01) return '<1%';
  return `${(p * PERCENT).toFixed(0)}%`;
}

/** The hypothesis colour as a translucent wash, `alpha` 0..1. */
function wash(hex: string, alpha: number): string {
  const c = forDark(hex);
  if (!/^#[0-9a-fA-F]{6}$/.test(c)) return 'transparent';
  const [r, g, b] = [1, 3, 5].map((i) => Number.parseInt(c.slice(i, i + 2), 16));
  return `rgba(${String(r)}, ${String(g)}, ${String(b)}, ${alpha.toFixed(3)})`;
}

function defaultVersion(page: KbVersionPage | undefined): number | undefined {
  const items = page?.items ?? [];
  return (items.find((v) => v.status === 'ACTIVE') ?? items[0])?.id;
}

export function KbPage() {
  const [params, setParams] = useSearchParams();
  const versions = useKbVersions();
  const asked = Number(params.get('v'));
  const chosen =
    Number.isInteger(asked) && versions.data?.items.some((v) => v.id === asked)
      ? asked
      : defaultVersion(versions.data);
  const kb = useKbContent(chosen);
  const error = versions.error ?? kb.error;

  return (
    <section aria-labelledby="kb-title" className="max-w-[1320px]">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h1 id="kb-title" className="text-[26px] font-semibold tracking-tight">
            Knowledge base
          </h1>
          <p className="mt-1 max-w-[70ch] text-[14px] text-muted">
            {kb.data
              ? `The rules DELPHI reasons with: ${String(kb.data.content.hypotheses.length)} explanations, ${String(kb.data.content.sources.length)} signals, and how strongly each signal tells them apart.`
              : 'The rules DELPHI reasons with.'}
          </p>
        </div>
        {chosen !== undefined && (
          <a
            href={`${API_BASE}/kb/versions/${String(chosen)}/export`}
            download
            className="ml-auto rounded-md border border-control px-4 py-2 text-[13px] text-soft transition-colors hover:border-soft hover:text-ink"
          >
            Export as YAML
          </a>
        )}
      </div>

      {error && (
        <div className="mt-6 max-w-xl">
          <ProblemAlert error={error} title="Could not load the knowledge base" />
        </div>
      )}

      <div className="mt-6 grid items-start gap-6 xl:grid-cols-[300px_minmax(0,1fr)]">
        <Versions
          page={versions.data}
          chosen={chosen}
          onChoose={(id) => {
            setParams(
              (p) => {
                p.set('v', String(id));
                return p;
              },
              { replace: true },
            );
          }}
        />
        <div className="min-w-0 space-y-6">
          {kb.data ? (
            <>
              <Explanations version={kb.data.version} content={kb.data.content} />
              <Signals key={kb.data.version.id} content={kb.data.content} />
              {kb.data.version.parent_id !== null && <Changes version={kb.data.version} />}
            </>
          ) : (
            !error && <p className="text-[14px] text-muted">Loading the knowledge base…</p>
          )}
        </div>
      </div>
    </section>
  );
}

function Card({
  id,
  title,
  note,
  action,
  children,
}: {
  id: string;
  title: string;
  note?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="rounded-md bg-surface p-8">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id={id} className="text-[20px] text-muted">
          {title}
        </h2>
        {action}
      </div>
      {note && <p className="mt-1 max-w-[70ch] text-[13px] text-muted">{note}</p>}
      <div className="mt-5">{children}</div>
    </section>
  );
}

function Versions({
  page,
  chosen,
  onChoose,
}: {
  page: KbVersionPage | undefined;
  chosen: number | undefined;
  onChoose: (id: number) => void;
}) {
  const items = page?.items ?? [];
  return (
    <div className="xl:sticky xl:top-6">
      <Card id="kb-versions" title="Versions">
        {items.length === 0 ? (
          <p className="text-[13px] text-muted">{page ? 'No versions stored yet.' : 'Loading…'}</p>
        ) : (
          <ol aria-label="KB versions, newest first" className="-mx-3 space-y-1">
            {items.map((v) => {
              const on = v.id === chosen;
              return (
                <li key={v.id}>
                  <button
                    type="button"
                    aria-current={on ? 'true' : undefined}
                    onClick={() => {
                      onChoose(v.id);
                    }}
                    className={`w-full rounded-md px-3 py-2.5 text-left transition-colors ${
                      on ? 'bg-raised' : 'hover:bg-raised/60'
                    }`}
                  >
                    <span className="flex items-baseline justify-between gap-3">
                      <span className="figure text-[14px] text-ink">v{v.id}</span>
                      <span className={`text-[12px] ${STATUS_TONE[v.status]}`}>
                        {humanise(v.status)}
                      </span>
                    </span>
                    <span className="mt-1 line-clamp-2 block text-[13px] text-soft">
                      {v.change_summary}
                    </span>
                    <span className="mt-1 block text-[12px] text-muted">
                      {v.parent_id === null ? 'Seed' : `From v${String(v.parent_id)}`} ·{' '}
                      {DATE.format(new Date(v.created_at))}
                    </span>
                  </button>
                </li>
              );
            })}
          </ol>
        )}
      </Card>
    </div>
  );
}

function Explanations({ version, content }: { version: KbVersionView; content: KbContent }) {
  return (
    <Card
      id="kb-explanations"
      title="What DELPHI can believe"
      note={content.description || undefined}
      action={
        <span className="text-[12px] text-muted">
          <span className="figure">v{version.id}</span> · {humanise(version.status)} ·{' '}
          <span className="figure" title={version.content_sha256}>
            {version.content_sha256.slice(0, 12)}
          </span>
        </span>
      }
    >
      <ul className="grid gap-x-8 gap-y-6 sm:grid-cols-2 2xl:grid-cols-3">
        {content.hypotheses.map((h) => (
          <li key={h.key} className="border-l-2 pl-4" style={{ borderColor: forDark(h.color) }}>
            <p className="text-[15px] text-ink">{h.name}</p>
            <p className="mt-0.5 text-[12px] text-muted">{humanise(h.kind)}</p>
            <p className="mt-2 text-[13px] leading-relaxed text-soft">{h.description}</p>
          </li>
        ))}
        <li className="border-l-2 border-dashed border-control pl-4">
          <p className="text-[15px] text-ink">Unexplained</p>
          <p className="mt-0.5 text-[12px] text-muted">Open world</p>
          <p className="mt-2 text-[13px] leading-relaxed text-soft">
            Whatever none of the above explains. It keeps a share of belief, so a new kind of fraud
            shows up as surprise instead of being forced into an old name.
          </p>
        </li>
      </ul>
    </Card>
  );
}

function Signals({ content }: { content: KbContent }) {
  const { hypotheses, sources } = content;
  const [picked, setPicked] = useState<string | undefined>(
    () => sources.find((s) => s.key === 'SPELL_BREAKER_DIGITAL_ARREST')?.key ?? sources[0]?.key,
  );
  const source = sources.find((s) => s.key === picked);
  const ceiling = Math.log2(Math.max(hypotheses.length, 2));

  return (
    <Card
      id="kb-signals"
      title="What each signal can tell apart"
      note="Each signal predicts different answers under each explanation. The wider those predictions sit apart, the more one answer settles. The bar is that spread, in bits."
    >
      <div className="grid items-start gap-8 lg:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
        <div className="space-y-6">
          {GROUPS.map((g) => {
            const rows = sources.filter(g.test);
            if (rows.length === 0) return null;
            return (
              <div key={g.key}>
                <h3 className="text-[13px] text-ink">{g.title}</h3>
                <p className="text-[12px] text-muted">{g.note}</p>
                <ul aria-label={g.title} className="-mx-3 mt-2">
                  {rows.map((s) => {
                    const bits = separation(s, hypotheses);
                    const on = s.key === picked;
                    return (
                      <li key={s.key}>
                        <button
                          type="button"
                          aria-pressed={on}
                          onClick={() => {
                            setPicked(s.key);
                          }}
                          className={`w-full rounded-md px-3 py-2 text-left transition-colors ${
                            on ? 'bg-raised' : 'hover:bg-raised/60'
                          }`}
                        >
                          <span className="flex items-baseline justify-between gap-3 text-[13px]">
                            <span className={`min-w-0 truncate ${on ? 'text-ink' : 'text-soft'}`}>
                              {keyText(s.key)}
                            </span>
                            <span className="figure shrink-0 text-[12px] text-muted">
                              {bits.toFixed(2)} bits
                            </span>
                          </span>
                          <span className="mt-1.5 block h-0.5 w-full rounded-full bg-line">
                            <span
                              className={`block h-full rounded-full ${on ? 'bg-accent' : 'bg-control'}`}
                              style={{ width: `${String((bits / ceiling) * PERCENT)}%` }}
                            />
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })}
        </div>
        {source && <SignalDetail key={source.key} source={source} hypotheses={hypotheses} />}
      </div>
    </Card>
  );
}

function outcomeText(source: KbSource, outcome: string): string {
  return source.answers[outcome] ?? keyText(outcome);
}

function SignalDetail({
  source,
  hypotheses,
}: {
  source: KbSource;
  hypotheses: readonly KbHypothesis[];
}) {
  const [answer, setAnswer] = useState(0);
  const belief = afterAnswer(source, answer, hypotheses);
  const even = 1 / Math.max(hypotheses.length, 1);
  const ranked = [...hypotheses].sort((a, b) => (belief[b.key] ?? 0) - (belief[a.key] ?? 0));
  const top = ranked[0];
  const topShare = top ? (belief[top.key] ?? 0) : 0;
  const runnerUp = ranked[1] ? (belief[ranked[1].key] ?? 0) : 0;
  // An answer "points to" an explanation when it lifts it above even and to at least twice
  // the runner-up; otherwise it moves belief too little to settle anything alone.
  const decisive = topShare > even && topShare >= 2 * runnerUp;
  const facts = [
    source.kind === 'EVIDENCE' ? 'Evidence' : source.mode === 'PASSIVE' ? 'Passive' : 'Active',
    source.tier !== 'NONE' ? `${humanise(source.tier)} friction` : 'No friction',
    source.expectedMinutes !== null ? `about ${String(source.expectedMinutes)} min` : null,
  ].filter(Boolean);

  return (
    <div aria-labelledby="kb-signal-title" role="region" className="min-w-0">
      <h3 id="kb-signal-title" className="text-[18px] text-ink">
        {keyText(source.key)}
      </h3>
      <p className="mt-1 text-[12px] text-muted">{facts.join(' · ')}</p>
      <p className="mt-3 max-w-[70ch] text-[14px] leading-relaxed text-soft">
        {source.description}
      </p>
      {source.question && (
        <blockquote className="mt-4 max-w-[60ch] border-l-2 border-accent pl-4 text-[15px] text-ink">
          “{source.question}”
        </blockquote>
      )}

      <div className="mt-6 overflow-x-auto">
        <table className="w-full min-w-[460px] border-separate border-spacing-1 text-[13px]">
          <caption className="mb-2 text-left text-[12px] text-muted">
            How likely each answer is, under each explanation (each column sums to 100%)
          </caption>
          <thead>
            <tr>
              <th scope="col" className="w-[28%] pb-2 text-left font-normal text-muted">
                Answer
              </th>
              {hypotheses.map((h) => (
                <th
                  key={h.key}
                  scope="col"
                  className="pb-2 text-left align-bottom text-[12px] font-normal leading-snug text-soft"
                >
                  <span
                    aria-hidden
                    className="mb-1.5 block h-0.5 w-5 rounded-full"
                    style={{ background: forDark(h.color) }}
                  />
                  {h.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {source.outcomes.map((o, i) => (
              <tr
                key={o}
                className={i === answer ? 'text-ink' : 'text-soft'}
                aria-selected={i === answer}
              >
                <th scope="row" className="py-0 pr-3 text-left font-normal">
                  <button
                    type="button"
                    onClick={() => {
                      setAnswer(i);
                    }}
                    className={`py-2 text-left transition-colors ${
                      i === answer ? 'selected-line text-ink' : 'text-soft hover:text-ink'
                    }`}
                  >
                    {outcomeText(source, o)}
                  </button>
                </th>
                {hypotheses.map((h) => {
                  const p = h.likelihoods[source.key]?.[i];
                  return (
                    <td
                      key={h.key}
                      // White on a wash of at most 45%: AA contrast whatever the hypothesis
                      // colour (E2E-A11Y, #131).
                      className="figure rounded-sm px-3 py-2 text-ink"
                      style={{ background: p === undefined ? undefined : wash(h.color, p * 0.45) }}
                    >
                      {p === undefined ? '—' : pct(p)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-6 rounded-md bg-ground p-6">
        <p className="text-[13px] text-muted">
          If the answer is{' '}
          <span className="text-ink">“{outcomeText(source, source.outcomes[answer] ?? '')}”</span>,
          starting from even odds:
        </p>
        <ul aria-label="Belief after this answer" className="mt-4 space-y-3">
          {hypotheses.map((h) => {
            const p = belief[h.key] ?? 0;
            const factor = p / even;
            return (
              <li
                key={h.key}
                className="grid grid-cols-[minmax(0,230px)_minmax(0,1fr)_88px] items-center gap-4"
              >
                <span className="truncate text-[13px] text-soft">{h.name}</span>
                <span className="h-1.5 rounded-full bg-raised">
                  <span
                    className="block h-full rounded-full transition-[width] duration-500"
                    style={{ width: `${String(p * PERCENT)}%`, background: forDark(h.color) }}
                  />
                </span>
                <span className="figure text-right text-[13px]">
                  {pct(p)}{' '}
                  <span className={factor >= 1 ? 'text-soft' : 'text-muted'}>
                    ×{factor.toFixed(factor >= 10 ? 0 : 1)}
                  </span>
                </span>
              </li>
            );
          })}
        </ul>
        {top && (
          <p className="mt-5 text-[13px] text-soft">
            {decisive
              ? `This answer points to ${top.name.toLowerCase()}: ${pct(topShare)}, ${(topShare / even).toFixed(1)}× its even share.`
              : 'This answer barely moves belief on its own; DELPHI would ask something else first.'}
          </p>
        )}
      </div>
    </div>
  );
}

function value(v: unknown): string {
  if (v === undefined) return '';
  const text = typeof v === 'string' ? v : JSON.stringify(v);
  return text.length > 80 ? `${text.slice(0, 77)}…` : text;
}

const OP_TONE: Readonly<Record<string, string>> = {
  added: 'text-positive',
  removed: 'text-negative',
  changed: 'text-warning',
};

function Changes({ version }: { version: KbVersionView }) {
  const diff = useKbDiff(version.id, version.parent_id);
  const changes = diff.data?.changes ?? [];
  return (
    <Card
      id="kb-changes"
      title={`What changed from v${String(version.parent_id)}`}
      note={version.change_summary}
    >
      {diff.isError ? (
        <ProblemAlert error={diff.error} title="Could not load the changes" />
      ) : !diff.data ? (
        <p className="text-[13px] text-muted">Loading…</p>
      ) : changes.length === 0 ? (
        <p className="text-[13px] text-muted">The content is identical to its parent.</p>
      ) : (
        <ul aria-label="Changes" className="divide-y divide-line text-[13px]">
          {changes.map((c) => (
            <li
              key={`${c.op}:${c.path}`}
              className="grid gap-x-4 gap-y-1 py-2.5 md:grid-cols-[72px_minmax(0,1fr)_minmax(0,1fr)]"
            >
              <span className={`text-[12px] ${OP_TONE[c.op] ?? 'text-muted'}`}>
                {humanise(c.op)}
              </span>
              <span className="figure min-w-0 break-all text-soft">{c.path}</span>
              <span className="figure min-w-0 break-all text-muted">
                {c.op === 'changed' ? (
                  <>
                    {value(c.before)} <span className="text-soft">→</span>{' '}
                    <span className="text-ink">{value(c.after)}</span>
                  </>
                ) : (
                  value(c.op === 'removed' ? c.before : c.after)
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
