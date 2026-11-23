import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';

import { useCase, useRequestEvidence } from '../api/cases';
import { useKbDisplay } from '../api/guardian';
import { TraceContext, useTraceQueue, useTrack } from '../api/traces';
import type { CaseDetail, EvidenceRow, Flip } from '../api/types';
import { EvidenceCard } from '../components/EvidenceCard';
import { HypothesisBar, type HypothesisStyles, percent } from '../components/HypothesisBar';
import { MoneyText } from '../components/MoneyText';
import { ProblemAlert } from '../components/ProblemAlert';
import { RiskTable } from '../components/RiskTable';
import { RoleGate } from '../components/RoleGate';
import { decisionText, humanise } from '../lib/format';
import { useSeen } from '../lib/useSeen';
import { DispositionPanel } from './DispositionPanel';

/** The outreach call is recorded by the investigator, not fetched (docs/09 §3). */
export const OUTREACH = 'CUSTOMER_OUTREACH_CALL';

/** docs/09 §2 item 3; the banner follows it with the expected loss in ₹. */
export function sufficiencyText(detail: CaseDetail): string {
  return `Evidence sufficient — recommend ${decisionText(detail.recommended_decision)}`;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-slate-600">{label}</dt>
      <dd className="font-medium">{children}</dd>
    </div>
  );
}

/** The Navigator console (docs/18 §2 `/cases/:id`, docs/09 §2). */
export function CaseConsolePage() {
  const { id = '' } = useParams();
  const found = useCase(id);
  const kb = useKbDisplay(found.data?.detail.kb_version_id);
  return (
    <section aria-labelledby="console-title">
      <p className="text-sm">
        <Link to="/cases" className="underline">
          Case queue
        </Link>
      </p>
      <h1 id="console-title" className="mt-1 text-2xl font-semibold">
        Navigator console
      </h1>
      {found.isPending && <p className="mt-4 text-slate-600">Loading the case…</p>}
      {found.isError && (
        <div className="mt-4">
          <ProblemAlert error={found.error} title="Could not load the case" />
        </div>
      )}
      {found.isSuccess && (
        <Console
          detail={found.data.detail}
          etag={found.data.etag}
          styles={kb.data?.styles ?? {}}
          outreachOutcomes={kb.data?.outcomes[OUTREACH] ?? []}
          reload={() => found.refetch()}
        />
      )}
    </section>
  );
}

function Console({
  detail,
  etag,
  styles,
  outreachOutcomes,
  reload,
}: {
  detail: CaseDetail;
  etag: string;
  styles: HypothesisStyles;
  outreachOutcomes: readonly string[];
  reload: () => Promise<unknown>;
}) {
  const disposed = detail.status === 'DISPOSED';
  const names: Record<string, string> = Object.fromEntries(
    detail.evidence_records.map((r) => [r.id, humanise(r.source_key)]),
  );
  const citationLabel = (recordId: string): string | undefined => names[recordId];
  const traces = useTraceQueue(detail.id); // docs/09 §5: the investigator's view events
  useEffect(() => {
    traces.track('CASE_VIEWED');
  }, [traces]);
  return (
    <TraceContext.Provider value={traces.track}>
      <div className="mt-4 space-y-8">
        <dl className="grid grid-cols-2 gap-4 text-sm md:grid-cols-5">
          <Field label="Type">{humanise(detail.type)}</Field>
          <Field label="Protocol">
            {detail.protocol === 'COERCION' ? (
              <span className="rounded bg-amber-100 px-2 py-0.5 text-amber-900">Coercion</span>
            ) : (
              'Standard'
            )}
          </Field>
          <Field label="At stake">
            <MoneyText paise={detail.amount_at_stake_paise} />
          </Field>
          <Field label="Status">
            <span aria-live="polite">{humanise(detail.status)}</span>
          </Field>
          <Field label="SLA">{detail.sla_breached ? 'SLA breached' : 'Within SLA'}</Field>
        </dl>

        <section aria-labelledby="belief-title">
          <h2 id="belief-title" className="text-lg font-semibold">
            Hypotheses and recommendation
          </h2>
          <div className="mt-2 space-y-3">
            <HypothesisBar probs={detail.posterior} styles={styles} label="Posterior" />
            <RiskTable risks={detail.risk_by_decision} taken={detail.disposition} />
          </div>
        </section>

        {detail.evidence_sufficient && !disposed && <SufficiencyBanner detail={detail} />}

        {!disposed && (
          <NextBestEvidence
            caseId={detail.id}
            rows={detail.next_best_evidence}
            outreachOutcomes={outreachOutcomes}
          />
        )}

        {detail.what_would_change_my_mind && (
          <WhatWouldChange flip={detail.what_would_change_my_mind} />
        )}

        <section aria-labelledby="evidence-title">
          <h2 id="evidence-title" className="text-lg font-semibold">
            Evidence
          </h2>
          {detail.evidence_records.length === 0 ? (
            <p className="mt-2 text-slate-600">No evidence fetched yet.</p>
          ) : (
            <div className="mt-2 space-y-3">
              {detail.evidence_records.map((record) => (
                <EvidenceCard
                  key={record.id}
                  record={record}
                  styles={styles}
                  citationLabel={citationLabel}
                />
              ))}
            </div>
          )}
        </section>

        <DispositionPanel detail={detail} etag={etag} styles={styles} reload={reload} />
      </div>
    </TraceContext.Provider>
  );
}

/** docs/09 §2 item 3: the planner says STOP. Fetching more is still allowed. */
function SufficiencyBanner({ detail }: { detail: CaseDetail }) {
  const loss = detail.risk_by_decision[detail.recommended_decision] ?? 0;
  return (
    <p
      role="status"
      className="rounded-md border border-emerald-300 bg-emerald-50 p-4 text-sm font-medium text-emerald-900"
    >
      {sufficiencyText(detail)} (expected loss <MoneyText paise={Math.round(loss)} />)
    </p>
  );
}

/** docs/06 §12: the single observation most likely to flip the recommendation. */
function WhatWouldChange({ flip }: { flip: Flip }) {
  const track = useTrack();
  const ref = useRef<HTMLElement>(null);
  const seen = useCallback(() => {
    track('WCMM_VIEWED');
  }, [track]);
  useSeen(ref, seen); // docs/09 §5
  return (
    <section ref={ref} aria-labelledby="wcmm-title">
      <h2 id="wcmm-title" className="text-lg font-semibold">
        What would change my mind
      </h2>
      <p className="mt-2 text-sm">
        If {humanise(flip.source_key)} came back {humanise(flip.outcome)} (chance{' '}
        {percent(flip.probability)}), the recommendation would become{' '}
        {decisionText(flip.new_decision)}, changing the expected loss by{' '}
        <MoneyText paise={flip.risk_change_paise} />.
      </p>
    </section>
  );
}

/** docs/09 §2 item 2: eligible rows by net value; the protocol's pinned row first. */
function NextBestEvidence({
  caseId,
  rows,
  outreachOutcomes,
}: {
  caseId: string;
  rows: readonly EvidenceRow[];
  outreachOutcomes: readonly string[];
}) {
  const fetch = useRequestEvidence(caseId);
  const [calling, setCalling] = useState(false);
  return (
    <section aria-labelledby="next-title">
      <h2 id="next-title" className="text-lg font-semibold">
        Next best evidence
      </h2>
      {fetch.isError && (
        <div className="mt-2">
          <ProblemAlert error={fetch.error} title="Could not fetch the evidence" />
        </div>
      )}
      {rows.length === 0 ? (
        <p className="mt-2 text-slate-600">No more evidence to fetch.</p>
      ) : (
        <table className="mt-2 w-full text-left text-sm">
          <caption className="sr-only">Evidence to fetch next, best first</caption>
          <thead className="border-b border-slate-300 text-slate-600">
            <tr>
              <th scope="col" className="py-2 pr-4">
                Source
              </th>
              <th scope="col" className="py-2 pr-4 text-right">
                Minutes
              </th>
              <th scope="col" className="py-2 pr-4">
                Expected to settle
              </th>
              <th scope="col" className="py-2 pr-4 text-right">
                Net value
              </th>
              <th scope="col" className="py-2 pr-4">
                <span className="sr-only">Action</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.source_key} className="border-b border-slate-100">
                <td className="py-2 pr-4">
                  {humanise(row.source_key)}
                  {row.pinned_by_protocol && (
                    <span className="ml-2 rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-900">
                      Protocol
                    </span>
                  )}
                  {!row.eligible && row.ineligible_reason && (
                    <span className="block text-xs text-slate-600">
                      Not available: {humanise(row.ineligible_reason)}
                    </span>
                  )}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums">{row.expected_minutes}</td>
                <td className="py-2 pr-4">
                  {row.settle_pct === null
                    ? '—'
                    : `${String(row.settle_pct)}% of remaining uncertainty`}
                </td>
                <td className="py-2 pr-4 text-right">
                  {row.net_value_paise === null ? '—' : <MoneyText paise={row.net_value_paise} />}
                </td>
                <td className="py-2 pr-4">
                  <RoleGate permission="case:write">
                    {(allowed) => (
                      <button
                        type="button"
                        disabled={!allowed || !row.eligible || fetch.isPending}
                        onClick={() => {
                          if (row.source_key === OUTREACH) setCalling(true);
                          else
                            fetch.mutate({
                              source_key: row.source_key,
                              refresh: false,
                              simulate: false,
                            });
                        }}
                        className="rounded-md border border-slate-300 px-3 py-1 disabled:opacity-50"
                      >
                        {row.source_key === OUTREACH ? 'Record call' : 'Fetch'}
                      </button>
                    )}
                  </RoleGate>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {calling && (
        <OutreachForm
          outcomes={outreachOutcomes}
          pending={fetch.isPending}
          onCancel={() => {
            setCalling(false);
          }}
          onSubmit={(outcome, note) => {
            fetch.mutate(
              { source_key: OUTREACH, outcome, note, refresh: false, simulate: false },
              {
                onSuccess: () => {
                  setCalling(false);
                },
              },
            );
          }}
        />
      )}
    </section>
  );
}

/** docs/09 §3: the investigator records the call's result as one outcome, with a note. */
function OutreachForm({
  outcomes,
  pending,
  onCancel,
  onSubmit,
}: {
  outcomes: readonly string[];
  pending: boolean;
  onCancel: () => void;
  onSubmit: (outcome: string, note: string | null) => void;
}) {
  const [outcome, setOutcome] = useState('');
  const [note, setNote] = useState('');
  return (
    <form
      aria-label="Record the outreach call"
      className="mt-3 space-y-3 rounded-md border border-slate-200 p-4 text-sm"
      onSubmit={(event) => {
        event.preventDefault();
        if (outcome) onSubmit(outcome, note.trim() || null);
      }}
    >
      <fieldset>
        <legend className="font-medium">Call result</legend>
        {outcomes.map((o) => (
          <label key={o} className="mt-1 flex items-center gap-2">
            <input
              type="radio"
              name="outcome"
              value={o}
              checked={outcome === o}
              onChange={() => {
                setOutcome(o);
              }}
            />
            {humanise(o)}
          </label>
        ))}
      </fieldset>
      <label className="block">
        Note
        <textarea
          value={note}
          onChange={(event) => {
            setNote(event.target.value);
          }}
          className="mt-1 block w-full rounded-md border border-control bg-surface px-3 py-2 text-ink focus-visible:border-accent"
        />
      </label>
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={!outcome || pending}
          className="rounded-full bg-ink px-3 py-1 font-semibold text-ground hover:bg-soft disabled:opacity-50"
        >
          Record
        </button>
        <button type="button" onClick={onCancel} className="rounded-md border px-3 py-1">
          Cancel
        </button>
      </div>
    </form>
  );
}
