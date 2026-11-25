import { useState } from 'react';

import { type AlertAction, useActOnPoliceAlert } from '../api/cases';
import { ApiError } from '../api/client';
import type { CaseDetail } from '../api/types';
import { MoneyText } from '../components/MoneyText';
import { ProblemAlert } from '../components/ProblemAlert';
import { RoleGate } from '../components/RoleGate';
import { humanise } from '../lib/format';
import { useSession } from '../lib/session';

/**
 * A coercion case's police alert draft (docs/09 §6, FR-NAV-10): what it would report, and Send
 * or Discard, each confirmed first. Nothing is sent automatically, and in the simulation "send"
 * is recorded as SENT_SIMULATED. Either action is final: a second one is 409.
 */

type Alert = NonNullable<CaseDetail['police_alert']>;

const WHEN = new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short' });

const STATUS_TEXT: Readonly<Record<Alert['status'], string>> = {
  DRAFT: 'Draft, not sent',
  SENT_SIMULATED: 'Sent (simulated)',
  DISCARDED: 'Discarded',
};

const CONFIRM_TEXT: Readonly<Record<AlertAction, string>> = {
  send: 'Send this alert to the police? This cannot be undone.',
  discard: 'Discard this alert? It cannot be sent afterwards.',
};

export interface AlertFacts {
  victim: string | null;
  amountPaise: number | null;
  channel: string | null;
  beneficiary: string | null;
  initiatedAt: string | null;
  evidence: { source: string; outcome: string; at: string | null }[];
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null;
}

function when(value: string | null): string {
  if (value === null) return '—';
  const at = Date.parse(value);
  return Number.isNaN(at) ? value : WHEN.format(at);
}

/** The draft's payload (docs/09 §6), read field by field: the api types it as an open object. */
export function alertFacts(payload: Readonly<Record<string, unknown>>): AlertFacts {
  const amount = payload.amount_paise;
  const evidence = Array.isArray(payload.evidence) ? (payload.evidence as unknown[]) : [];
  return {
    victim: text(payload.victim),
    amountPaise: typeof amount === 'number' && Number.isSafeInteger(amount) ? amount : null,
    channel: text(payload.channel),
    beneficiary: text(payload.beneficiary_account_id),
    initiatedAt: text(payload.initiated_at),
    evidence: evidence.flatMap((e) => {
      if (typeof e !== 'object' || e === null) return [];
      const item = e as Record<string, unknown>;
      const source = text(item.source_key);
      const outcome = text(item.outcome);
      return source && outcome ? [{ source, outcome, at: text(item.at) }] : [];
    }),
  };
}

function isConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
}

export function PoliceAlertPanel({ caseId, alert }: { caseId: string; alert: Alert }) {
  const act = useActOnPoliceAlert(caseId);
  const me = useSession();
  const [confirming, setConfirming] = useState<AlertAction | null>(null);
  const facts = alertFacts(alert.payload);
  const acted = alert.status !== 'DRAFT';
  const actor = alert.acted_by === null ? null : alert.acted_by === me?.id ? 'you' : alert.acted_by;
  return (
    <section
      aria-labelledby="alert-title"
      className="rounded-md border border-warning bg-warning-bg p-4 text-sm"
    >
      <h2 id="alert-title" className="text-lg font-semibold text-ink">
        Police alert
      </h2>
      <p className="mt-1 font-medium text-warning" aria-live="polite">
        {STATUS_TEXT[alert.status]}
        {acted && actor && ` by ${actor}`}
        {acted && alert.acted_at && `, ${when(alert.acted_at)}`}
      </p>

      <dl className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-5">
        <div>
          <dt className="text-muted">Victim</dt>
          <dd className="font-medium">{facts.victim ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-muted">Amount</dt>
          <dd className="font-medium">
            {facts.amountPaise === null ? '—' : <MoneyText paise={facts.amountPaise} />}
          </dd>
        </div>
        <div>
          <dt className="text-muted">Channel</dt>
          <dd className="font-medium">{facts.channel ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-muted">Beneficiary account</dt>
          <dd className="break-all font-medium">{facts.beneficiary ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-muted">Payment time</dt>
          <dd className="font-medium">{when(facts.initiatedAt)}</dd>
        </div>
      </dl>

      {alert.status === 'SENT_SIMULATED' && (
        <div className="mt-3">
          <h3 className="font-medium">Evidence sent</h3>
          {facts.evidence.length === 0 ? (
            <p className="text-muted">No evidence had been fetched.</p>
          ) : (
            <ul className="mt-1 list-disc pl-5">
              {facts.evidence.map((e) => (
                <li key={e.source}>
                  {humanise(e.source)}: {humanise(e.outcome)}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {act.isError && (
        <div className="mt-3">
          {isConflict(act.error) ? (
            <p role="alert" className="text-negative">
              Someone already acted on this alert. The case has been reloaded.
            </p>
          ) : (
            <ProblemAlert error={act.error} title="Could not act on the alert" />
          )}
        </div>
      )}

      {!acted && (
        <RoleGate permission="case:write">
          {confirming === null ? (
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                onClick={() => {
                  act.reset();
                  setConfirming('send');
                }}
                className="rounded-full bg-ink px-3 py-1 font-semibold text-ground hover:bg-soft"
              >
                Send
              </button>
              <button
                type="button"
                onClick={() => {
                  act.reset();
                  setConfirming('discard');
                }}
                className="rounded-full border border-control px-3 py-1"
              >
                Discard
              </button>
            </div>
          ) : (
            <div role="group" aria-label="Confirm" className="mt-3 space-y-2">
              <p>{CONFIRM_TEXT[confirming]}</p>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={act.isPending}
                  onClick={() => {
                    act.mutate(confirming, {
                      onSettled: () => {
                        setConfirming(null);
                      },
                    });
                  }}
                  className="rounded-full bg-ink px-3 py-1 font-semibold text-ground hover:bg-soft disabled:opacity-50"
                >
                  {confirming === 'send' ? 'Yes, send it' : 'Yes, discard it'}
                </button>
                <button
                  type="button"
                  disabled={act.isPending}
                  onClick={() => {
                    setConfirming(null);
                  }}
                  className="rounded-full border border-control px-3 py-1"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </RoleGate>
      )}
    </section>
  );
}
