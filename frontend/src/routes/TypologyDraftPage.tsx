import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router';

import { ApiError } from '../api/client';
import { useKbContent, useKbVersions } from '../api/kb';
import { useBacktest, useDraft, useSaveDraft } from '../api/scout';
import type { DraftIssue, DraftPatch, DraftView, LikelihoodRow } from '../api/types';
import { ProblemAlert } from '../components/ProblemAlert';
import { RoleGate } from '../components/RoleGate';
import { humanise, keyText } from '../lib/format';

/**
 * The typology draft editor (docs/18 §2 `/scout/drafts/:id`, docs/11 §3–4, #118): the draft's
 * name, key, description, colour and open mass share, its likelihood rows (weights per outcome,
 * normalised live, with the elicitation helper's confidence → ESS), the KB issues it has, and
 * its backtest. Saving sends the ETag; the server validates ACTIVE + the draft and refuses a
 * draft that breaks a KB rule. Shadow evaluation and promotion are #119's.
 */

const INPUT =
  'w-full rounded-md border border-control bg-raised px-3 py-1.5 text-[13px] text-ink focus:border-accent focus:outline-none';
const PRIMARY =
  'rounded-full bg-ink px-4 py-1.5 text-[13px] font-semibold text-ground transition-colors hover:bg-soft disabled:opacity-50';
const SECONDARY =
  'rounded-full border border-control px-4 py-1.5 text-[13px] text-soft transition-colors hover:border-soft hover:text-ink disabled:opacity-50';
const PERCENT = 100;

/** docs/11 §3: the elicitation helper's confidence → ESS. */
export const CONFIDENCE = [
  { label: 'Low', ess: 10 },
  { label: 'Medium', ess: 20 },
  { label: 'High', ess: 50 },
] as const;

const EDITABLE = new Set(['CANDIDATE', 'DRAFT', 'BACKTESTED']);

interface Spec {
  name: string;
  key: string;
  description: string;
  color: string;
  open_mass_share: number;
  likelihoods: Record<string, LikelihoodRow>;
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function isRow(value: unknown): value is LikelihoodRow {
  if (typeof value !== 'object' || value === null) return false;
  const r = value as Record<string, unknown>;
  return (
    Array.isArray(r.probs) &&
    r.probs.every((p) => typeof p === 'number') &&
    typeof r.ess === 'number'
  );
}

/** The editable part of a stored spec, read leniently (docs/11 §3: stored specs may grow). */
export function readSpec(spec: Record<string, unknown>): Spec {
  const rows =
    typeof spec.likelihoods === 'object' && spec.likelihoods !== null ? spec.likelihoods : {};
  const likelihoods: Record<string, LikelihoodRow> = {};
  for (const [source, row] of Object.entries(rows)) if (isRow(row)) likelihoods[source] = row;
  return {
    name: str(spec.name),
    key: str(spec.key),
    description: str(spec.description),
    color: str(spec.color) || '#7C3AED',
    open_mass_share: typeof spec.open_mass_share === 'number' ? spec.open_mass_share : 0.5,
    likelihoods,
  };
}

/** Weights → probabilities; all-zero weights give a uniform row. */
export function normalise(weights: readonly number[]): number[] {
  const clean = weights.map((w) => (Number.isFinite(w) && w > 0 ? w : 0));
  const total = clean.reduce((a, b) => a + b, 0);
  return total > 0 ? clean.map((w) => w / total) : clean.map(() => 1 / clean.length);
}

function pct(p: number): string {
  return `${(p * PERCENT).toFixed(p > 0 && p < 0.01 ? 1 : 0)}%`;
}

function useActiveOutcomes(): Record<string, string[]> {
  const versions = useKbVersions();
  const activeId = versions.data?.items.find((v) => v.status === 'ACTIVE')?.id;
  const content = useKbContent(activeId);
  return useMemo(
    () => Object.fromEntries((content.data?.content.sources ?? []).map((s) => [s.key, s.outcomes])),
    [content.data],
  );
}

export function TypologyDraftPage() {
  const { id = '' } = useParams();
  const draft = useDraft(id);
  return (
    <section aria-labelledby="draft-title" className="max-w-[1320px]">
      <p className="text-[13px]">
        <Link to="/scout" className="text-accent underline-offset-2 hover:underline">
          Typology Scout
        </Link>
      </p>
      {draft.isPending && <p className="mt-3 text-[13px] text-muted">Loading the draft…</p>}
      {draft.isError && (
        <>
          <h1 id="draft-title" className="mt-2 text-[26px] font-semibold tracking-tight">
            Typology draft
          </h1>
          <div className="mt-3">
            <ProblemAlert error={draft.error} title="Could not load the draft" />
          </div>
        </>
      )}
      {draft.isSuccess && (
        <Editor key={`${draft.data.id}-${String(draft.data.version)}`} draft={draft.data} />
      )}
    </section>
  );
}

function Editor({ draft }: { draft: DraftView }) {
  const stored = readSpec(draft.spec);
  const [spec, setSpec] = useState<Spec>(stored);
  const outcomes = useActiveOutcomes();
  const save = useSaveDraft(draft.id);
  const editable = EDITABLE.has(draft.status);
  const set = (patch: Partial<Spec>) => {
    setSpec((s) => ({ ...s, ...patch }));
  };
  const setRow = (source: string, row: LikelihoodRow) => {
    setSpec((s) => ({ ...s, likelihoods: { ...s.likelihoods, [source]: row } }));
  };
  const patch = (): DraftPatch => ({
    name: spec.name,
    key: spec.key,
    description: spec.description,
    color: spec.color,
    open_mass_share: spec.open_mass_share,
    likelihoods: Object.fromEntries(
      Object.entries(spec.likelihoods).map(([source, row]) => [
        source,
        { probs: normalise(row.probs), ess: row.ess },
      ]),
    ),
  });
  const saveErrors =
    save.error instanceof ApiError ? (save.error.problem?.errors ?? []) : ([] as DraftIssue[]);
  return (
    <>
      <h1 id="draft-title" className="mt-2 text-[26px] font-semibold tracking-tight">
        {stored.name || 'Unnamed typology draft'}
      </h1>
      <p className="mt-1 text-[13px] text-muted">
        {humanise(draft.status)} · revision <span className="figure">{draft.revision}</span>
        {draft.editors.length > 0 && ` · edited by ${String(draft.editors.length)}`}
      </p>

      <Issues issues={draft.issues} />

      <form
        aria-label="Draft"
        className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)]"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate({ version: draft.version, patch: patch() });
        }}
      >
        <fieldset disabled={!editable} className="space-y-4 rounded-md bg-surface p-6">
          <legend className="sr-only">Typology</legend>
          <Field label="Display name" id="draft-name">
            <input
              id="draft-name"
              className={INPUT}
              value={spec.name}
              onChange={(e) => {
                set({ name: e.target.value });
              }}
            />
          </Field>
          <Field label="Key (UPPER_SNAKE_CASE)" id="draft-key">
            <input
              id="draft-key"
              className={`${INPUT} font-mono`}
              value={spec.key}
              onChange={(e) => {
                set({ key: e.target.value.toUpperCase() });
              }}
            />
          </Field>
          <Field label="Description" id="draft-description">
            <textarea
              id="draft-description"
              className={INPUT}
              rows={3}
              value={spec.description}
              onChange={(e) => {
                set({ description: e.target.value });
              }}
            />
          </Field>
          <Field label="Colour" id="draft-color">
            <input
              id="draft-color"
              type="color"
              className="h-8 w-16 rounded border border-control bg-raised"
              value={/^#[0-9a-fA-F]{6}$/.test(spec.color) ? spec.color : '#7C3AED'}
              onChange={(e) => {
                set({ color: e.target.value.toUpperCase() });
              }}
            />
          </Field>
          <Field label="Open mass share" id="draft-open-mass">
            <input
              id="draft-open-mass"
              type="number"
              min={0}
              max={1}
              step={0.05}
              className={`${INPUT} figure`}
              value={spec.open_mass_share}
              onChange={(e) => {
                set({ open_mass_share: Number(e.target.value) });
              }}
            />
          </Field>
        </fieldset>

        <fieldset disabled={!editable} className="min-w-0 rounded-md bg-surface p-6">
          <legend className="text-[16px] text-ink">Likelihood rows</legend>
          <p className="mt-1 text-[12px] text-muted">
            Weights per outcome, shown normalised. Confidence sets how much evidence the row is
            worth (ESS).
          </p>
          {Object.keys(spec.likelihoods).length === 0 && (
            <p className="mt-3 text-[13px] text-muted">The cluster observed no source.</p>
          )}
          <div className="mt-3 space-y-5">
            {Object.entries(spec.likelihoods).map(([source, row]) => (
              <RowEditor
                key={source}
                source={source}
                row={row}
                outcomes={outcomes[source]}
                onChange={(next) => {
                  setRow(source, next);
                }}
              />
            ))}
          </div>
        </fieldset>

        <RoleGate permission="fde">
          <div className="flex flex-wrap items-center gap-3 lg:col-span-2">
            <button type="submit" className={PRIMARY} disabled={!editable || save.isPending}>
              Save as revision {draft.revision + 1}
            </button>
            {!editable && (
              <p className="text-[12px] text-muted">
                A {humanise(draft.status).toLowerCase()} draft can't be edited.
              </p>
            )}
            {save.isSuccess && (
              <p role="status" className="text-[13px] text-soft">
                Saved as revision {save.data.revision}.
              </p>
            )}
          </div>
        </RoleGate>
      </form>
      {save.isError && (
        <div className="mt-3 space-y-2">
          <ProblemAlert error={save.error} title="Could not save the draft" />
          {saveErrors.length > 0 && <Issues issues={saveErrors} title="What the KB rules refuse" />}
        </div>
      )}

      <Backtest draft={draft} />
    </>
  );
}

function Field({ label, id, children }: { label: string; id: string; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="block text-[12px] text-muted">
        {label}
      </label>
      <div className="mt-1">{children}</div>
    </div>
  );
}

function Issues({
  issues,
  title = 'KB issues to fix before a backtest',
}: {
  issues: DraftIssue[];
  title?: string;
}) {
  if (issues.length === 0) return null;
  return (
    <div className="mt-4 rounded-md border border-warning/40 p-4 text-[13px]">
      <p className="text-ink">{title}</p>
      <ul className="mt-2 space-y-1 text-soft">
        {issues.map((i) => (
          <li key={`${i.path}-${i.code}`}>
            <span className="font-mono text-[12px] text-muted">{i.path}</span> {i.message}
          </li>
        ))}
      </ul>
    </div>
  );
}

function RowEditor({
  source,
  row,
  outcomes,
  onChange,
}: {
  source: string;
  row: LikelihoodRow;
  outcomes: string[] | undefined;
  onChange: (row: LikelihoodRow) => void;
}) {
  const probs = normalise(row.probs);
  const known = CONFIDENCE.some((c) => c.ess === row.ess);
  const titleId = `row-${source}`;
  return (
    <div role="group" aria-labelledby={titleId}>
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h3 id={titleId} className="text-[14px] text-ink">
          {keyText(source)}
        </h3>
        <label className="flex items-center gap-2 text-[12px] text-muted">
          Confidence
          <select
            className="rounded-md border border-control bg-raised px-2 py-1 text-[12px] text-ink"
            value={String(row.ess)}
            onChange={(e) => {
              onChange({ ...row, ess: Number(e.target.value) });
            }}
          >
            {!known && (
              <option value={String(row.ess)}>Empirical (ESS {figureEss(row.ess)})</option>
            )}
            {CONFIDENCE.map((c) => (
              <option key={c.ess} value={String(c.ess)}>
                {c.label} (ESS {c.ess})
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="mt-2 grid grid-cols-[repeat(auto-fill,minmax(140px,1fr))] gap-3">
        {row.probs.map((w, i) => {
          const outcome = outcomes?.[i] ?? `Outcome ${String(i + 1)}`;
          const inputId = `${source}-${String(i)}`;
          return (
            <div key={inputId}>
              <label htmlFor={inputId} className="block text-[12px] text-muted">
                {keyText(outcome)}
              </label>
              <div className="mt-1 flex items-center gap-2">
                <input
                  id={inputId}
                  type="number"
                  min={0}
                  step="any"
                  className={`${INPUT} figure`}
                  value={w}
                  onChange={(e) => {
                    const next = [...row.probs];
                    next[i] = Number(e.target.value);
                    onChange({ ...row, probs: next });
                  }}
                />
                <output htmlFor={inputId} className="figure w-12 text-right text-[12px] text-soft">
                  {pct(probs[i] ?? 0)}
                </output>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function figureEss(ess: number): string {
  return Number.isInteger(ess) ? String(ess) : ess.toFixed(1);
}

interface Report {
  attributed: number;
  attributed_labelled: number;
  attributed_labelled_precision: number | null;
  genuine_disturbed: number;
  decision_changes: Record<string, number>;
  unexplained_rate: { before: number | null; after: number | null };
  sessions: number;
  cases: number;
  skipped: number;
  limitations: string[];
}

function rate(p: number | null): string {
  return p === null ? '—' : pct(p);
}

function Backtest({ draft }: { draft: DraftView }) {
  const backtest = useBacktest(draft.id);
  const report = draft.backtest_report as Report | null;
  const stale = report !== null && draft.backtested_revision !== draft.revision;
  const canRun =
    (draft.status === 'DRAFT' || draft.status === 'BACKTESTED') && draft.issues.length === 0;
  return (
    <section aria-labelledby="backtest-title" className="mt-8 rounded-md bg-surface p-6">
      <div className="flex flex-wrap items-baseline justify-between gap-4">
        <h2 id="backtest-title" className="text-[20px] text-muted">
          Backtest
        </h2>
        <RoleGate permission="fde">
          <button
            type="button"
            className={SECONDARY}
            disabled={!canRun || backtest.isPending}
            onClick={() => {
              backtest.mutate(undefined);
            }}
          >
            Backtest revision {draft.revision}
          </button>
        </RoleGate>
      </div>
      <p className="mt-1 max-w-[80ch] text-[12px] text-muted">
        Replays the last 30 days' sessions and cases under the ACTIVE knowledge base and under it
        plus this draft. There is no pass mark: you judge the result.
        {!canRun && ' A draft must be saved, and have no KB issues, before a backtest.'}
      </p>
      {backtest.isSuccess && (
        <p role="status" className="mt-3 text-[13px] text-soft">
          Backtest started. The report appears here when it ends.
        </p>
      )}
      {backtest.isError && (
        <div className="mt-3">
          <ProblemAlert error={backtest.error} title="Could not start the backtest" />
        </div>
      )}
      {report === null ? (
        <p className="mt-4 text-[13px] text-muted">Not backtested yet.</p>
      ) : (
        <div className="mt-4">
          {stale && (
            <p className="mb-3 text-[13px] text-warning">
              This report is for revision {draft.backtested_revision}; the draft has changed since.
            </p>
          )}
          <dl className="grid grid-cols-2 gap-4 text-[13px] md:grid-cols-4">
            <Stat label="Queue items it now explains" value={String(report.attributed)} />
            <Stat
              label="Of those labelled, fraud"
              value={`${rate(report.attributed_labelled_precision)} of ${String(report.attributed_labelled)}`}
            />
            <Stat label="Genuine payments disturbed" value={String(report.genuine_disturbed)} />
            <Stat
              label="Unexplained rate"
              value={`${rate(report.unexplained_rate.before)} → ${rate(report.unexplained_rate.after)}`}
            />
          </dl>
          <table className="mt-5 text-left text-[13px]">
            <caption className="text-left text-[12px] text-muted">
              Decisions, ACTIVE → with the draft ({report.sessions} sessions, {report.cases} cases
              {report.skipped > 0 && `, ${String(report.skipped)} skipped`})
            </caption>
            <thead className="text-[12px] text-muted">
              <tr>
                <th scope="col" className="py-1 pr-6 font-normal">
                  Change
                </th>
                <th scope="col" className="py-1 font-normal">
                  Items
                </th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(report.decision_changes).map(([change, n]) => (
                <tr key={change} className="border-t border-line">
                  <td className="py-1 pr-6">{change.split('→').map(humanise).join(' → ')}</td>
                  <td className="figure py-1">{n}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {report.limitations.map((l) => (
            <p key={l} className="mt-3 text-[12px] text-muted">
              {l}
            </p>
          ))}
        </div>
      )}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="figure mt-1 text-[17px]">{value}</dd>
    </div>
  );
}
