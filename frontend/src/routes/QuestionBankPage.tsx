import { useState } from 'react';

import { useKbContent, useKbVersions } from '../api/kb';
import {
  useApproveBatch,
  useGenerateVariants,
  useRefreshQuestionBank,
  useReviewVariant,
  useVariants,
} from '../api/questionBank';
import type { QuestionVariantView } from '../api/types';
import { ProblemAlert } from '../components/ProblemAlert';
import { RoleGate } from '../components/RoleGate';
import { keyText } from '../lib/format';

/**
 * The question bank review (docs/18 §2 `/kb/questions`, docs/07 §9, #110): drafted wordings and
 * translations of the questions the phone asks, each beside the English original. An FDE
 * approves (optionally editing the question), or rejects, each one; "Approve batch" puts the
 * approved ones into a CANDIDATE KB version. Nothing here reaches a customer until that version
 * is promoted (FR-KB-05). Everyone else can read the queue.
 */

const SELECT =
  'rounded-md border border-control bg-raised px-3 py-1.5 text-[13px] text-ink transition-colors hover:border-soft focus:border-accent focus:outline-none';
const PRIMARY =
  'rounded-full bg-ink px-4 py-1.5 text-[13px] font-semibold text-ground transition-colors hover:bg-soft disabled:opacity-50';
const SECONDARY =
  'rounded-full border border-control px-4 py-1.5 text-[13px] text-soft transition-colors hover:border-soft hover:text-ink disabled:opacity-50';
const MAX_CHARS = 300;

/** The question probes of the ACTIVE KB version: the sources the customer answers (docs/07 §9). */
function useQuestionProbes() {
  const versions = useKbVersions();
  const active = versions.data?.items.find((v) => v.status === 'ACTIVE')?.id;
  const kb = useKbContent(active);
  const probes = (kb.data?.content.sources ?? [])
    .filter((src) => src.question !== null)
    .map((src) => src.key);
  return { probes, error: versions.error ?? kb.error };
}

export const LANGUAGES = [
  { code: 'hi', name: 'Hindi' },
  { code: 'te', name: 'Telugu' },
  { code: 'ta', name: 'Tamil' },
  { code: 'en', name: 'English' },
] as const;
type LanguageCode = (typeof LANGUAGES)[number]['code'];

function languageName(code: string): string {
  return LANGUAGES.find((l) => l.code === code)?.name ?? code;
}

export function QuestionBankPage() {
  return (
    <section aria-labelledby="questions-title" className="max-w-[1320px]">
      <h1 id="questions-title" className="text-[26px] font-semibold tracking-tight">
        Question bank review
      </h1>
      <p className="mt-1 max-w-[80ch] text-[14px] text-muted">
        Drafted wordings and translations of the questions the phone asks. No draft reaches a
        customer until it is approved here and the KB version holding it is promoted.
      </p>
      <RoleGate permission="fde">
        <Draft />
      </RoleGate>
      <Queue />
      <RoleGate permission="fde">
        <Batch />
      </RoleGate>
    </section>
  );
}

function Draft() {
  const generate = useGenerateVariants();
  const questions = useQuestionProbes();
  const [chosen, setProbe] = useState<string | null>(null);
  const probe = chosen ?? questions.probes[0] ?? '';
  const [languages, setLanguages] = useState<ReadonlySet<LanguageCode>>(new Set(['te']));
  return (
    <div className="mt-6 rounded-md bg-surface p-6">
      <h2 className="text-[16px] text-ink">Draft new variants</h2>
      <p className="mt-1 text-[12px] text-muted">
        The model drafts one variant per language; without a model, none are drafted.
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-4">
        <label className="sr-only" htmlFor="draft-probe">
          Question
        </label>
        <select
          id="draft-probe"
          className={SELECT}
          value={probe}
          onChange={(e) => {
            setProbe(e.target.value);
          }}
        >
          {questions.probes.map((p) => (
            <option key={p} value={p}>
              {keyText(p)}
            </option>
          ))}
        </select>
        <fieldset className="flex flex-wrap gap-3 text-[13px]">
          <legend className="sr-only">Languages</legend>
          {LANGUAGES.map((l) => (
            <label key={l.code} className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={languages.has(l.code)}
                onChange={(e) => {
                  const next = new Set(languages);
                  if (e.target.checked) next.add(l.code);
                  else next.delete(l.code);
                  setLanguages(next);
                }}
              />
              {l.name}
            </label>
          ))}
        </fieldset>
        <button
          type="button"
          className={PRIMARY}
          disabled={generate.isPending || languages.size === 0 || probe === ''}
          onClick={() => {
            generate.mutate({ probe_key: probe, languages: [...languages] });
          }}
        >
          Draft
        </button>
      </div>
      {questions.error && (
        <div className="mt-3">
          <ProblemAlert error={questions.error} title="Could not load the questions" />
        </div>
      )}
      {generate.isError && (
        <div className="mt-3">
          <ProblemAlert error={generate.error} title="Could not start the drafting" />
        </div>
      )}
      {generate.isSuccess && (
        <p role="status" className="mt-3 text-[13px] text-soft">
          Drafting started. The drafts appear below when the job ends.
        </p>
      )}
    </div>
  );
}

function Queue() {
  const pending = useVariants('PENDING_REVIEW');
  const items = pending.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <div className="mt-6">
      <h2 className="text-[20px] text-muted">Waiting for review</h2>
      {pending.isError && (
        <div className="mt-3">
          <ProblemAlert error={pending.error} title="Could not load the drafts" />
        </div>
      )}
      {pending.isPending && <p className="mt-3 text-[13px] text-muted">Loading the drafts…</p>}
      {pending.isSuccess && items.length === 0 && (
        <p className="mt-3 text-[13px] text-muted">No draft is waiting for review.</p>
      )}
      <ul className="mt-3 space-y-4">
        {items.map((v) => (
          <li key={v.id}>
            <VariantCard variant={v} />
          </li>
        ))}
      </ul>
      {pending.hasNextPage && (
        <button
          type="button"
          className={`mt-4 ${SECONDARY}`}
          disabled={pending.isFetchingNextPage}
          onClick={() => {
            void pending.fetchNextPage();
          }}
        >
          {pending.isFetchingNextPage ? 'Loading…' : 'Load more'}
        </button>
      )}
    </div>
  );
}

function OptionList({ options }: { options: QuestionVariantView['options'] }) {
  return (
    <ul className="mt-2 space-y-0.5 text-[12px] text-soft">
      {options.map((o) => (
        <li key={o.key}>
          <span className="text-muted">{keyText(o.key)}:</span> {o.text}
        </li>
      ))}
    </ul>
  );
}

function VariantCard({ variant }: { variant: QuestionVariantView }) {
  const review = useReviewVariant();
  const refresh = useRefreshQuestionBank();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(variant.text);
  const titleId = `variant-${variant.id}`;
  return (
    <article aria-labelledby={titleId} className="rounded-md bg-surface p-6">
      <h3 id={titleId} className="text-[14px] text-ink">
        {keyText(variant.probe_key)} · {languageName(variant.language)}
        {variant.generated_by === 'LLM' && (
          <span className="ml-2 rounded-full border border-control px-2 py-0.5 text-[11px] text-muted">
            AI-drafted
          </span>
        )}
      </h3>
      <div className="mt-3 grid gap-6 md:grid-cols-2">
        <div>
          <p className="text-[12px] text-muted">
            English original{variant.original ? ` (${variant.original.variant_id})` : ''}
          </p>
          {variant.original ? (
            <>
              <p lang="en" className="mt-1 text-[14px]">
                {variant.original.text}
              </p>
              <OptionList options={variant.original.options} />
            </>
          ) : (
            <p className="mt-1 text-[13px] text-muted">The ACTIVE KB has no English original.</p>
          )}
        </div>
        <div>
          <p className="text-[12px] text-muted">Draft</p>
          {editing ? (
            <>
              <label className="sr-only" htmlFor={`edit-${variant.id}`}>
                Edited question
              </label>
              <textarea
                id={`edit-${variant.id}`}
                disabled={review.isPending}
                lang={variant.language}
                className="mt-1 w-full rounded-md border border-control bg-raised p-2 text-[14px]"
                rows={3}
                maxLength={MAX_CHARS}
                value={text}
                onChange={(e) => {
                  setText(e.target.value);
                }}
              />
            </>
          ) : (
            <p lang={variant.language} className="mt-1 text-[14px]">
              {variant.text}
            </p>
          )}
          <OptionList options={variant.options} />
        </div>
      </div>
      <RoleGate permission="fde">
        <div className="mt-4 flex flex-wrap gap-3">
          <button
            type="button"
            className={PRIMARY}
            disabled={review.isPending || text.trim() === ''}
            onClick={() => {
              review.mutate({
                id: variant.id,
                decision: 'APPROVE',
                edited_text: editing && text !== variant.text ? text : null,
              });
            }}
          >
            {editing ? 'Approve with edit' : 'Approve'}
          </button>
          {!editing && (
            <button
              type="button"
              className={SECONDARY}
              disabled={review.isPending}
              onClick={() => {
                setEditing(true);
              }}
            >
              Edit
            </button>
          )}
          <button
            type="button"
            className={SECONDARY}
            disabled={review.isPending}
            onClick={() => {
              review.mutate({ id: variant.id, decision: 'REJECT', edited_text: null });
            }}
          >
            Reject
          </button>
        </div>
      </RoleGate>
      {review.isError && (
        <div className="mt-3">
          {/* The lists are not refreshed on a refusal, so this stays until the reviewer does. */}
          <ProblemAlert error={review.error} title="Could not record the review" />
          <button type="button" className={`mt-2 ${SECONDARY}`} onClick={refresh}>
            Refresh the queue
          </button>
        </div>
      )}
    </article>
  );
}

function Batch() {
  // What the batch would take, counted by the server (`included=false`), not page 1 filtered here.
  const approved = useVariants('APPROVED', false);
  const batch = useApproveBatch();
  const waiting = approved.data?.pages.flatMap((p) => p.items) ?? [];
  const more = approved.hasNextPage;
  return (
    <div className="mt-8 rounded-md bg-surface p-6">
      <h2 className="text-[16px] text-ink">Approve batch</h2>
      <p className="mt-1 text-[12px] text-muted">
        {more
          ? `More than ${String(waiting.length)} approved variants are waiting for a KB version.`
          : waiting.length === 1
            ? '1 approved variant is waiting for a KB version.'
            : `${String(waiting.length)} approved variants are waiting for a KB version.`}{' '}
        The batch becomes a candidate version, to be promoted on the KB page.
      </p>
      <button
        type="button"
        className={`mt-3 ${PRIMARY}`}
        disabled={batch.isPending || waiting.length === 0}
        onClick={() => {
          batch.mutate(undefined);
        }}
      >
        Approve batch
      </button>
      {batch.isError && (
        <div className="mt-3">
          <ProblemAlert error={batch.error} title="Could not make the KB version" />
        </div>
      )}
      {batch.isSuccess && (
        <p role="status" className="mt-3 text-[13px] text-soft">
          Candidate KB version {batch.data.kb_version_id} holds{' '}
          {batch.data.variant_ids.length === 1
            ? '1 variant'
            : `${String(batch.data.variant_ids.length)} variants`}
          .
        </p>
      )}
    </div>
  );
}
