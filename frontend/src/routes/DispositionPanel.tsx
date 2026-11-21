import { zodResolver } from '@hookform/resolvers/zod';
import { useState } from 'react';
import { useForm, useWatch } from 'react-hook-form';
import { z } from 'zod';

import { useDispose } from '../api/cases';
import { ApiError } from '../api/client';
import type { CaseDetail } from '../api/types';
import type { HypothesisStyles } from '../components/HypothesisBar';
import { ProblemAlert } from '../components/ProblemAlert';
import { RoleGate } from '../components/RoleGate';
import { decisionText, humanise } from '../lib/format';

/** docs/05 §4: an override of the recommendation needs a rationale this long. */
export const MIN_RATIONALE = 20;
/** CONFIRM_FRAUD may name a fraud the KB has no hypothesis for (#177). */
export const FRAUD_OTHER = 'FRAUD_OTHER';
const NOT_TYPOLOGIES = new Set(['GENUINE', 'UNEXPLAINED']);

/** What CONFIRM_FRAUD can name: the case's fraud hypotheses, then FRAUD_OTHER. */
export function typologies(detail: Pick<CaseDetail, 'posterior'>): string[] {
  const fraud = Object.entries(detail.posterior)
    .filter(([key]) => !NOT_TYPOLOGIES.has(key))
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([key]) => key);
  return [...fraud, FRAUD_OTHER];
}

function dispositionSchema(recommended: string) {
  return z
    .object({
      decision: z.string().min(1, 'Choose a decision.'),
      typology: z.string(),
      confidence: z.string().regex(/^[1-5]$/, 'Choose a confidence from 1 to 5.'),
      note: z.string(),
      rationale: z.string(),
    })
    .superRefine((v, ctx) => {
      if (v.decision === 'CONFIRM_FRAUD' && !v.typology) {
        ctx.addIssue({ code: 'custom', path: ['typology'], message: 'Choose the typology.' });
      }
      if (v.decision && v.decision !== recommended && v.rationale.trim().length < MIN_RATIONALE) {
        ctx.addIssue({
          code: 'custom',
          path: ['rationale'],
          message: `An override needs a rationale of at least ${String(MIN_RATIONALE)} characters.`,
        });
      }
    });
}
type DispositionForm = z.infer<ReturnType<typeof dispositionSchema>>;

/** What changed between the case the investigator saw and the current one (docs/18 §4). */
export function caseChanges(before: CaseDetail, after: CaseDetail): string[] {
  const changes: string[] = [];
  const field = (label: string, a: string, b: string) => {
    if (a !== b) changes.push(`${label}: ${a} → ${b}`);
  };
  field('Status', humanise(before.status), humanise(after.status));
  field('Leading hypothesis', before.leading_hypothesis_name, after.leading_hypothesis_name);
  field(
    'Recommendation',
    decisionText(before.recommended_decision),
    decisionText(after.recommended_decision),
  );
  field(
    'Evidence records',
    String(before.evidence_records.length),
    String(after.evidence_records.length),
  );
  field(
    'Disposition',
    before.disposition ? decisionText(before.disposition) : 'none',
    after.disposition ? decisionText(after.disposition) : 'none',
  );
  return changes;
}

function isConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 412;
}

const inputClass = 'mt-1 block w-full rounded-md border border-slate-300 px-3 py-2';

/** docs/09 §2 item 6: decide the case, or show how it was decided. */
export function DispositionPanel({
  detail,
  etag,
  styles,
  reload,
}: {
  detail: CaseDetail;
  etag: string;
  styles: HypothesisStyles;
  reload: () => Promise<unknown>;
}) {
  const dispose = useDispose(detail.id);
  // The case the investigator was looking at when a disposition came back 412.
  const [stale, setStale] = useState<CaseDetail | null>(null);
  const {
    register,
    handleSubmit,
    control,
    formState: { errors },
  } = useForm<DispositionForm>({
    resolver: zodResolver(dispositionSchema(detail.recommended_decision)),
    defaultValues: { decision: '', typology: '', confidence: '', note: '', rationale: '' },
  });
  const decision = useWatch({ control, name: 'decision' });

  if (detail.status === 'DISPOSED' && detail.disposition) {
    return (
      <section aria-labelledby="disposition-title">
        <h2 id="disposition-title" className="text-lg font-semibold">
          Disposition
        </h2>
        <p className="mt-2 text-sm" aria-live="polite">
          {decisionText(detail.disposition)}
          {detail.disposition_typology &&
            ` (${styles[detail.disposition_typology]?.name ?? humanise(detail.disposition_typology)})`}
          {detail.disposition_confidence != null &&
            `, confidence ${String(detail.disposition_confidence)}/5`}
          {detail.is_override && ' — an override of the recommendation'}
        </p>
        {detail.override_rationale && (
          <p className="mt-1 text-sm text-slate-700">Rationale: {detail.override_rationale}</p>
        )}
      </section>
    );
  }

  const decisions = Object.keys(detail.risk_by_decision);
  const onSubmit = handleSubmit((values) => {
    const seen = detail;
    dispose.mutate(
      {
        etag,
        request: {
          decision: values.decision,
          typology: values.decision === 'CONFIRM_FRAUD' ? values.typology : null,
          confidence: Number(values.confidence),
          note: values.note.trim() || null,
          override_rationale:
            values.decision === detail.recommended_decision ? null : values.rationale.trim(),
        },
      },
      {
        onError: (error) => {
          if (isConflict(error)) {
            setStale(seen);
            void reload();
          }
        },
      },
    );
  });

  const changes = stale ? caseChanges(stale, detail) : [];
  return (
    <section aria-labelledby="disposition-title">
      <h2 id="disposition-title" className="text-lg font-semibold">
        Disposition
      </h2>
      {stale && (
        <div
          role="alert"
          className="mt-2 rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950"
        >
          <p className="font-semibold">This case changed — reload</p>
          {changes.length > 0 ? (
            <ul className="mt-1 list-disc pl-5">
              {changes.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          ) : (
            <p className="mt-1">Someone else updated it while you were deciding.</p>
          )}
          <button
            type="button"
            className="mt-2 rounded-md border border-amber-400 px-3 py-1"
            onClick={() => {
              setStale(null);
              dispose.reset();
              void reload();
            }}
          >
            Reload
          </button>
        </div>
      )}
      {dispose.isError && !isConflict(dispose.error) && (
        <div className="mt-2">
          <ProblemAlert error={dispose.error} title="Could not decide the case" />
        </div>
      )}
      <RoleGate
        permission="case:write"
        fallback={<p className="mt-2 text-sm text-slate-600">Only investigators decide cases.</p>}
      >
        <form
          noValidate
          aria-label="Decide the case"
          className="mt-2 max-w-xl space-y-3 text-sm"
          onSubmit={(event) => {
            void onSubmit(event);
          }}
        >
          <fieldset disabled={dispose.isPending || stale !== null} className="space-y-3">
            <div role="radiogroup" aria-label="Decision" className="flex flex-wrap gap-2">
              {decisions.map((d) => (
                <label key={d} className="flex items-center gap-2 rounded-md border px-3 py-1">
                  <input type="radio" value={d} {...register('decision')} />
                  {decisionText(d)}
                  {d === detail.recommended_decision && ' (recommended)'}
                </label>
              ))}
            </div>
            {errors.decision && <p className="text-red-800">{errors.decision.message}</p>}

            {decision === 'CONFIRM_FRAUD' && (
              <label className="block">
                Typology
                <select className={inputClass} {...register('typology')}>
                  <option value="">Choose…</option>
                  {typologies(detail).map((t) => (
                    <option key={t} value={t}>
                      {t === FRAUD_OTHER ? 'Other fraud' : (styles[t]?.name ?? humanise(t))}
                    </option>
                  ))}
                </select>
                {errors.typology && <span className="text-red-800">{errors.typology.message}</span>}
              </label>
            )}

            <label className="block">
              Confidence (1–5)
              <select className={inputClass} {...register('confidence')}>
                <option value="">Choose…</option>
                {[1, 2, 3, 4, 5].map((c) => (
                  <option key={c} value={String(c)}>
                    {c}
                  </option>
                ))}
              </select>
              {errors.confidence && (
                <span className="text-red-800">{errors.confidence.message}</span>
              )}
            </label>

            {decision && decision !== detail.recommended_decision && (
              <label className="block">
                Override rationale
                <textarea className={inputClass} {...register('rationale')} />
                {errors.rationale && (
                  <span className="text-red-800">{errors.rationale.message}</span>
                )}
              </label>
            )}

            <label className="block">
              Note
              <textarea className={inputClass} {...register('note')} />
            </label>

            <button
              type="submit"
              className="rounded-md bg-slate-900 px-4 py-2 text-white disabled:opacity-50"
            >
              {dispose.isPending ? 'Deciding…' : 'Decide'}
            </button>
          </fieldset>
        </form>
      </RoleGate>
    </section>
  );
}
