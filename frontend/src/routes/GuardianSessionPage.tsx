import { Link, useParams } from 'react-router';

import { API_BASE } from '../api/client';
import { useGuardianSession, useKbDisplay } from '../api/guardian';
import { CHANNEL_LABEL } from '../api/phone';
import type { GuardianSessionView } from '../api/types';
import { CandidatesTable } from '../components/CandidatesTable';
import {
  HypothesisBar,
  type HypothesisStyles,
  HypothesisTimeline,
  percent,
} from '../components/HypothesisBar';
import { MoneyText } from '../components/MoneyText';
import { ProblemAlert } from '../components/ProblemAlert';
import { RiskTable } from '../components/RiskTable';
import { decisionText, humanise } from '../lib/format';
import { BeliefView, hasWebGL } from '../theatre/BeliefView';

/** The prior, then the posterior after each completed observation (docs/08 §6). */
function steps(session: GuardianSessionView) {
  const out: { label: string; probs: Record<string, number> }[] = [];
  if (session.prior.probs) out.push({ label: 'Prior', probs: session.prior.probs });
  for (const run of session.probe_runs) {
    if (run.posterior_after) {
      const outcome = run.outcome ? `: ${humanise(run.outcome)}` : '';
      out.push({
        label: `${String(run.seq)}. ${humanise(run.probe_key)}${outcome}`,
        probs: run.posterior_after,
      });
    }
  }
  return out;
}

/**
 * The decision with its terminal reason, or the terminal reason alone when the session ended
 * without one (a customer cancel), or "Not decided yet".
 */
function outcomeText(session: GuardianSessionView): string {
  const reason = session.terminal_reason ? humanise(session.terminal_reason) : null;
  if (session.decision) {
    return reason
      ? `${decisionText(session.decision)} (${reason})`
      : decisionText(session.decision);
  }
  return reason ?? 'Not decided yet';
}

const DECISION_TONE: Readonly<Record<string, string>> = {
  RELEASE: 'text-positive',
  HOLD_FOR_REVIEW: 'text-warning',
  PROTECTIVE_HOLD: 'text-negative',
  DECLINE: 'text-negative',
};

const WHEN = new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short' });

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 px-6 py-5 first:pl-0 sm:border-l sm:border-line sm:first:border-l-0">
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="mt-2 text-[17px] leading-tight text-ink">{children}</dd>
    </div>
  );
}

function Card({
  id,
  title,
  note,
  children,
  className = '',
}: {
  id: string;
  title: string;
  note?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section aria-labelledby={id} className={`min-w-0 rounded-md bg-surface p-8 ${className}`}>
      <h2 id={id} className="text-[20px] text-muted">
        {title}
      </h2>
      {note && <p className="mt-1 max-w-[70ch] text-[13px] text-muted">{note}</p>}
      <div className="mt-5">{children}</div>
    </section>
  );
}

/** Session detail (docs/18 §2 `/guardian/:id`, docs/08 §6). */
export function GuardianSessionPage() {
  const { id = '' } = useParams();
  const session = useGuardianSession(id);
  const kb = useKbDisplay(session.data?.kb_version_id);
  return (
    <section aria-labelledby="session-title" className="max-w-[1320px]">
      <p className="text-[13px] text-muted">
        <Link to="/guardian" className="transition-colors hover:text-ink">
          Guardian monitor
        </Link>{' '}
        /
      </p>
      <h1 id="session-title" className="mt-1 text-[26px] font-semibold tracking-tight">
        Guardian session
      </h1>
      {session.isPending && <p className="mt-4 text-[14px] text-muted">Loading the session…</p>}
      {session.isError && (
        <div className="mt-4 max-w-xl">
          <ProblemAlert error={session.error} title="Could not load the session" />
        </div>
      )}
      {session.isSuccess && <SessionDetail session={session.data} styles={kb.data?.styles ?? {}} />}
    </section>
  );
}

function SessionDetail({
  session,
  styles,
}: {
  session: GuardianSessionView;
  styles: HypothesisStyles;
}) {
  const planned = session.probe_runs.filter((r) => r.candidates.length > 0);
  const answered = session.probe_runs.filter((r) => r.posterior_after !== null);
  const timeline = steps(session);
  const posterior = session.posterior ?? session.prior.probs ?? null;
  const leading = session.leading_hypothesis;
  const leadingName = leading ? (styles[leading]?.name ?? humanise(leading)) : null;
  const leadingShare = leading && posterior ? posterior[leading] : undefined;

  return (
    <div className="mt-1 space-y-6">
      <p className="text-[14px] text-muted">
        Started {WHEN.format(new Date(session.started_at))}
        {leadingName && leadingShare !== undefined && (
          <>
            {' '}
            · DELPHI leans to <span className="text-soft">{leadingName}</span> at{' '}
            <span className="figure text-soft">{percent(leadingShare)}</span>
          </>
        )}
        .
      </p>

      <dl className="grid grid-cols-2 rounded-md bg-surface px-8 md:grid-cols-4">
        <Field label="Amount">
          <span className="figure">
            <MoneyText paise={session.payment.amount_paise} />
          </span>
        </Field>
        <Field label="Channel">
          {CHANNEL_LABEL[session.payment.channel] ?? humanise(session.payment.channel)}
        </Field>
        <Field label="Status">{humanise(session.status)}</Field>
        <Field label="Decision">
          <span
            aria-live="polite"
            className={session.decision ? (DECISION_TONE[session.decision] ?? '') : 'text-soft'}
          >
            {outcomeText(session)}
          </span>
          {session.unexplained && (
            <span className="ml-2 rounded-sm bg-raised px-2 py-0.5 text-[12px] text-soft">
              Unexplained
            </span>
          )}
        </Field>
      </dl>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
        {posterior && (
          <Card
            id="belief-title"
            title="What DELPHI believes"
            note="Each point of light is a share of the probability; each answer moves the light between the explanations."
          >
            <div className="space-y-4">
              <BeliefView
                posterior={posterior}
                styles={styles}
                decision={session.decision}
                answers={answered.length}
                fallback={
                  <HypothesisBar probs={posterior} styles={styles} label="Posterior" size="lead" />
                }
              />
              {hasWebGL() && <HypothesisBar probs={posterior} styles={styles} label="Posterior" />}
            </div>
          </Card>
        )}
        <Card id="timeline-title" title="Hypotheses over time">
          {timeline.length > 0 ? (
            <HypothesisTimeline steps={timeline} styles={styles} />
          ) : (
            <p className="text-[14px] text-muted">No observations yet.</p>
          )}
        </Card>
      </div>

      <div className={`grid gap-6 ${session.risk_by_decision ? 'lg:grid-cols-2' : ''}`}>
        {session.risk_by_decision && (
          <Card
            id="risk-title"
            title="Decision and risk"
            note="The expected loss of each decision under the current belief; DELPHI takes the lowest."
          >
            <RiskTable risks={session.risk_by_decision} taken={session.decision} />
          </Card>
        )}
        <Card
          id="baselines-title"
          title="Baseline comparison"
          note="What the bank's rules would have done with the same payment."
        >
          <table className="w-full text-left text-[14px]">
            <caption className="sr-only">What each policy decides for this payment</caption>
            <thead className="text-[12px] text-muted">
              <tr className="border-b border-line">
                <th scope="col" className="py-2 pr-6 font-normal">
                  Policy
                </th>
                <th scope="col" className="py-2 pr-6 font-normal">
                  Decision
                </th>
              </tr>
            </thead>
            <tbody>
              <tr className="border-b border-line">
                <td className="py-3 pr-6 text-ink">DELPHI</td>
                <td
                  className={`py-3 pr-6 ${session.decision ? (DECISION_TONE[session.decision] ?? 'text-ink') : 'text-muted'}`}
                >
                  {session.decision ? decisionText(session.decision) : '—'}
                </td>
              </tr>
              <tr className="border-b border-line">
                <td className="py-3 pr-6 text-soft">B1 (static rules)</td>
                <td className="py-3 pr-6 text-soft">
                  {session.baselines ? decisionText(session.baselines.B1) : '—'}
                </td>
              </tr>
              <tr>
                <td className="py-3 pr-6 text-soft">B2 (score + OTP)</td>
                <td className="py-3 pr-6 text-soft">
                  {session.baselines
                    ? session.baselines.B2 === 'OTP_STEP_UP'
                      ? `${decisionText('OTP_STEP_UP')} (released if the OTP passes)`
                      : decisionText(session.baselines.B2)
                    : '—'}
                </td>
              </tr>
            </tbody>
          </table>
        </Card>
      </div>

      {planned.length > 0 && (
        <Card
          id="candidates-title"
          title="Candidates at each planning step"
          note="At each step DELPHI prices every question it could ask: what the answer is worth, less what asking costs. It asks the best, or stops."
        >
          <div className="space-y-8">
            {planned.map((run) => (
              <div key={run.id}>
                <h3 className="text-[14px] text-ink">
                  Step <span className="figure">{run.seq}</span>: {humanise(run.probe_key)}
                </h3>
                <div className="mt-2 overflow-x-auto">
                  <CandidatesTable
                    caption={`Step ${String(run.seq)} candidates`}
                    candidates={run.candidates}
                    selectedBy={run.selected_by}
                    propensity={run.propensity}
                  />
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <p className="text-[14px] text-muted">
        <a
          className="text-soft underline decoration-control underline-offset-4 transition-colors hover:text-ink hover:decoration-soft"
          href={`${API_BASE}/guardian/sessions/${session.id}/dd-pack?format=html`}
          target="_blank"
          rel="noreferrer"
        >
          Open the DD pack
        </a>
        {session.dd_pack_complete ? ' (complete)' : ' (incomplete)'}
      </p>
    </div>
  );
}
