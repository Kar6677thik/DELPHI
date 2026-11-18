import { useParams } from 'react-router';

import { API_BASE } from '../api/client';
import { useGuardianSession, useHypothesisStyles } from '../api/guardian';
import type { GuardianSessionView } from '../api/types';
import { type Candidate, CandidatesTable } from '../components/CandidatesTable';
import { HypothesisTimeline } from '../components/HypothesisBar';
import { MoneyText } from '../components/MoneyText';
import { ProblemAlert } from '../components/ProblemAlert';
import { RiskTable } from '../components/RiskTable';
import { humanise } from './GuardianPage';

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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-slate-600">{label}</dt>
      <dd className="font-medium">{children}</dd>
    </div>
  );
}

/** Session detail (docs/18 §2 `/guardian/:id`, docs/08 §6). */
export function GuardianSessionPage() {
  const { id = '' } = useParams();
  const session = useGuardianSession(id);
  const styles = useHypothesisStyles(session.data?.kb_version_id);
  return (
    <section aria-labelledby="session-title">
      <h1 id="session-title" className="text-2xl font-semibold">
        Guardian session
      </h1>
      {session.isPending && <p className="mt-4 text-slate-600">Loading the session…</p>}
      {session.isError && (
        <div className="mt-4">
          <ProblemAlert error={session.error} title="Could not load the session" />
        </div>
      )}
      {session.isSuccess && <SessionDetail session={session.data} styles={styles.data ?? {}} />}
    </section>
  );
}

function SessionDetail({
  session,
  styles,
}: {
  session: GuardianSessionView;
  styles: Record<string, { name: string; color: string }>;
}) {
  const planned = session.probe_runs.filter((r) => r.candidates.length > 0);
  return (
    <div className="mt-4 space-y-8">
      <dl className="grid grid-cols-2 gap-4 text-sm md:grid-cols-4">
        <Field label="Amount">
          <MoneyText paise={session.payment.amount_paise} />
        </Field>
        <Field label="Channel">{humanise(session.payment.channel)}</Field>
        <Field label="Status">{humanise(session.status)}</Field>
        <Field label="Decision">
          <span aria-live="polite">
            {session.decision ? humanise(session.decision) : 'Not decided yet'}
            {session.terminal_reason && ` (${humanise(session.terminal_reason)})`}
          </span>
          {session.unexplained && (
            <span className="ml-2 rounded bg-violet-100 px-2 py-0.5 text-violet-900">
              Unexplained
            </span>
          )}
        </Field>
      </dl>

      <section aria-labelledby="timeline-title">
        <h2 id="timeline-title" className="text-lg font-semibold">
          Hypotheses over time
        </h2>
        <div className="mt-2">
          <HypothesisTimeline steps={steps(session)} styles={styles} />
        </div>
      </section>

      {session.risk_by_decision && (
        <section aria-labelledby="risk-title">
          <h2 id="risk-title" className="text-lg font-semibold">
            Decision and risk
          </h2>
          <div className="mt-2">
            <RiskTable risks={session.risk_by_decision} recommended={session.decision} />
          </div>
        </section>
      )}

      <section aria-labelledby="baselines-title">
        <h2 id="baselines-title" className="text-lg font-semibold">
          Baseline comparison
        </h2>
        <table className="mt-2 text-left text-sm">
          <caption className="sr-only">What each policy decides for this payment</caption>
          <thead className="border-b border-slate-300 text-slate-600">
            <tr>
              <th scope="col" className="py-1 pr-6">
                Policy
              </th>
              <th scope="col" className="py-1 pr-6">
                Decision
              </th>
            </tr>
          </thead>
          <tbody>
            <tr className="border-b border-slate-100 font-semibold">
              <td className="py-1 pr-6">DELPHI</td>
              <td className="py-1 pr-6">{session.decision ? humanise(session.decision) : '—'}</td>
            </tr>
            <tr className="border-b border-slate-100">
              <td className="py-1 pr-6">B1 (static rules)</td>
              <td className="py-1 pr-6">
                {session.baselines ? humanise(session.baselines.B1) : '—'}
              </td>
            </tr>
            <tr className="border-b border-slate-100">
              <td className="py-1 pr-6">B2 (score + OTP)</td>
              <td className="py-1 pr-6">
                {session.baselines
                  ? session.baselines.B2 === 'OTP_STEP_UP'
                    ? 'OTP step-up (released if the OTP passes)'
                    : humanise(session.baselines.B2)
                  : '—'}
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      {planned.length > 0 && (
        <section aria-labelledby="candidates-title">
          <h2 id="candidates-title" className="text-lg font-semibold">
            Candidates at each planning step
          </h2>
          {planned.map((run) => (
            <div key={run.id} className="mt-3">
              <h3 className="text-sm font-medium">
                Step {run.seq}: {humanise(run.probe_key)}
              </h3>
              <CandidatesTable
                caption={`Step ${String(run.seq)} candidates`}
                candidates={run.candidates as unknown as Candidate[]}
                selectedBy={run.selected_by}
                propensity={run.propensity}
              />
            </div>
          ))}
        </section>
      )}

      <p>
        <a
          className="underline"
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
