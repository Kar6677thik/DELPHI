import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useState } from 'react';

import { ApiError } from '../api/client';
import {
  GUARDIAN_QUERY_KEY,
  useActiveKbDisplay,
  useGuardianSession,
  useKbDisplay,
} from '../api/guardian';
import {
  CHANNELS,
  CHANNEL_LABEL,
  type PayChannel,
  type Persona,
  type PhoneEvent,
  type ProbeAnswer,
  type ClientSignals,
  type ProbeRequest,
  useAnswerProbe,
  usePersonas,
  useStartPayment,
} from '../api/phone';
import type { GuardianSessionView } from '../api/types';
import { CandidatesTable, parseCandidates } from '../components/CandidatesTable';
import {
  forDark,
  HypothesisBar,
  type HypothesisStyles,
  percent,
} from '../components/HypothesisBar';
import { MoneyText, formatPaise } from '../components/MoneyText';
import { ProblemAlert } from '../components/ProblemAlert';
import { RiskTable } from '../components/RiskTable';
import { decisionText, humanise } from '../lib/format';
import { DecisionScreen } from '../phone/DecisionScreen';
import { BeliefView, hasWebGL } from '../theatre/BeliefView';
import { PhoneFrame } from '../phone/PhoneFrame';
import { ProbeScreen } from '../phone/ProbeScreens';
import { usePhoneStream } from '../phone/usePhoneStream';

const PAISE_PER_RUPEE = 100;

/** What the phone shows for the payment in flight. */
interface Flight {
  sessionId: string;
  probe: ProbeRequest | null;
  sms: string | null;
  attemptsRemaining: number | null;
  /** The decision, `null` for a customer cancel; `undefined` while the Guardian is working. */
  decided?: string | null;
}

function clockNow(): string {
  return new Date().toTimeString().slice(0, 5);
}

/**
 * The mock phone's presenter view (docs/18 §2 `/phone`, FR-UI-02): the customer's phone on the
 * left, what DELPHI is thinking about the same payment on the right. "Phone only" hides the
 * rest for the judges' projector.
 */
export function PhonePage() {
  const personas = usePersonas();
  const [personaId, setPersonaId] = useState<string | null>(null);
  const [phoneOnly, setPhoneOnly] = useState(false);
  const [flight, setFlight] = useState<Flight | null>(null);
  const queryClient = useQueryClient();

  const persona =
    personas.data?.find((p) => p.customer_id === personaId) ?? personas.data?.[0] ?? null;

  const onEvent = useCallback(
    (event: PhoneEvent) => {
      setFlight((current) => {
        if (current?.sessionId !== event.data.session_id) return current;
        switch (event.type) {
          case 'probe.requested':
            return { ...current, probe: event.data, sms: null, attemptsRemaining: null };
          case 'otp.sms':
            return { ...current, sms: event.data.code };
          case 'probe.attempt_result':
            return { ...current, attemptsRemaining: event.data.attempts_remaining };
          case 'session.decided':
            return { ...current, probe: null, sms: null, decided: event.data.decision };
        }
      });
      void queryClient.invalidateQueries({ queryKey: GUARDIAN_QUERY_KEY });
    },
    [queryClient],
  );
  usePhoneStream(persona?.customer_id ?? null, onEvent);

  useEffect(() => {
    if (!phoneOnly) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPhoneOnly(false);
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
    };
  }, [phoneOnly]);

  const phone = persona ? (
    <Phone key={persona.customer_id} persona={persona} flight={flight} onFlight={setFlight} />
  ) : null;

  if (phoneOnly && phone) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center overflow-auto bg-black p-6">
        <button
          type="button"
          onClick={() => {
            setPhoneOnly(false);
          }}
          className="absolute right-6 top-6 h-9 rounded-full px-4 text-[13px] text-muted hover:text-ink"
        >
          Exit phone only
        </button>
        {phone}
      </div>
    );
  }

  return (
    <section aria-labelledby="phone-title" className="max-w-[1320px]">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h1 id="phone-title" className="text-[26px] font-semibold tracking-tight">
            Mock phone
          </h1>
          <p className="mt-1 text-[14px] text-muted">
            {persona
              ? `${persona.name} pays from their phone; DELPHI decides before the money moves.`
              : 'The customer’s side of a payment, and what DELPHI makes of it.'}
          </p>
        </div>
        {personas.data && personas.data.length > 0 && (
          <div className="ml-auto flex items-end gap-3">
            <label className="text-[12px] text-muted">
              Customer
              <select
                value={persona?.customer_id ?? ''}
                onChange={(e) => {
                  setPersonaId(e.target.value);
                  setFlight(null);
                }}
                className="mt-1 block h-[34px] rounded-full border border-control bg-surface px-4 text-[13px] text-ink"
              >
                {personas.data.map((p) => (
                  <option key={p.customer_id} value={p.customer_id}>
                    {p.name}
                    {p.vulnerable ? ' (vulnerable)' : ''}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              aria-pressed={phoneOnly}
              onClick={() => {
                setPhoneOnly(true);
              }}
              className="h-[34px] rounded-full border border-control px-4 text-[13px] text-soft transition-colors hover:border-soft hover:text-ink"
            >
              Phone only
            </button>
          </div>
        )}
      </div>

      {personas.isPending && <p className="mt-8 text-muted">Loading the demo customers…</p>}
      {personas.isError && (
        <div className="mt-8 max-w-xl space-y-2">
          <ProblemAlert error={personas.error} title="The phone could not load its customers" />
          {personas.error instanceof ApiError && personas.error.status === 404 && (
            <p className="text-[13px] text-muted">
              The phone’s endpoints arrive with the Guardian API (#98).
            </p>
          )}
        </div>
      )}
      {personas.isSuccess && personas.data.length === 0 && (
        <p className="mt-8 text-muted">
          No demo customers yet. Seed the demo dataset (Admin) to create them.
        </p>
      )}

      {phone && (
        <div className="mt-8 grid items-start gap-8 lg:grid-cols-[372px_minmax(0,1fr)]">
          {phone}
          <DelphiPanel sessionId={flight?.sessionId ?? null} persona={persona} />
        </div>
      )}
    </section>
  );
}

/** The phone itself: the payment form, then each probe, then the decision. */
function Phone({
  persona,
  flight,
  onFlight,
}: {
  persona: Persona;
  flight: Flight | null;
  onFlight: (next: Flight | null) => void;
}) {
  const start = useStartPayment();
  const answer = useAnswerProbe();
  const queryClient = useQueryClient();
  /** Each POST returns the session as it now stands: the panel shows it at once. */
  const remember = (session: GuardianSessionView) => {
    queryClient.setQueryData([...GUARDIAN_QUERY_KEY, 'session', session.id], session);
  };

  const respond = (probe: ProbeRequest, value: ProbeAnswer, signals: ClientSignals) => {
    answer.mutate(
      { sessionId: probe.session_id, probeRunId: probe.probe_run_id, answer: value, signals },
      {
        onSuccess: (session) => {
          remember(session);
          if (!flight) return;
          // A wrong OTP keeps the run waiting; anything else clears the screen for what's next.
          const waiting = session.probe_runs.some(
            (r) => r.id === probe.probe_run_id && r.status === 'AWAITING_CUSTOMER',
          );
          onFlight({
            ...flight,
            probe: waiting ? flight.probe : null,
            sms: waiting ? flight.sms : null,
            decided: session.decision ?? flight.decided,
          });
        },
      },
    );
  };

  let screen;
  if (!flight) {
    screen = (
      <PayScreen
        persona={persona}
        busy={start.isPending}
        error={start.error}
        onPay={(draft) => {
          start.mutate(draft, {
            onSuccess: (session) => {
              remember(session);
              onFlight({
                sessionId: session.id,
                probe: null,
                sms: null,
                attemptsRemaining: null,
                decided: session.decision ?? undefined,
              });
            },
          });
        }}
      />
    );
  } else if (flight.decided !== undefined) {
    screen = (
      <DecisionScreen
        decision={flight.decided}
        onDone={() => {
          onFlight(null);
        }}
      />
    );
  } else if (flight.probe) {
    const probe = flight.probe;
    screen = (
      <>
        <ProbeScreen
          key={probe.probe_run_id}
          probe={probe}
          busy={answer.isPending}
          attemptsRemaining={flight.attemptsRemaining}
          onAnswer={(value, signals) => {
            respond(probe, value, signals);
          }}
        />
        {answer.isError && (
          <p role="alert" className="mt-3 text-[13px] text-[#f08a9b]">
            The answer didn’t go through. Try again.
          </p>
        )}
      </>
    );
  } else {
    screen = (
      <div className="flex h-full flex-col items-center justify-center text-center">
        <span
          aria-hidden="true"
          className="size-10 animate-spin rounded-full border-2 border-white/15 border-t-white"
        />
        <p className="mt-5 text-[16px]">Checking this payment</p>
        <p className="mt-1 text-[13px] text-white/60">This takes a few seconds.</p>
      </div>
    );
  }

  return (
    <PhoneFrame clock={clockNow()} sms={flight?.sms}>
      {screen}
    </PhoneFrame>
  );
}

/** The bank app's payment form: from, to whom, how much, how. */
function PayScreen({
  persona,
  busy,
  error,
  onPay,
}: {
  persona: Persona;
  busy: boolean;
  error: Error | null;
  onPay: (draft: {
    payer_account_id: string;
    payee_account_id: string;
    amount_paise: number;
    channel: PayChannel;
    device_id: string;
  }) => void;
}) {
  const account = persona.accounts[0];
  const device = persona.devices[0];
  const [payee, setPayee] = useState(persona.payees[0]?.account_id ?? '');
  const [rupees, setRupees] = useState('');
  const [channel, setChannel] = useState<PayChannel>('UPI_PUSH');
  const amount = Math.round(Number(rupees.replace(/,/g, '')) * PAISE_PER_RUPEE);
  const valid = Boolean(account && device && payee) && Number.isFinite(amount) && amount > 0;

  return (
    <form
      className="flex h-full flex-col"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid && account && device) {
          onPay({
            payer_account_id: account.id,
            payee_account_id: payee,
            amount_paise: amount,
            channel,
            device_id: device.id,
          });
        }
      }}
    >
      {account && (
        <div className="rounded-2xl bg-white/[.06] px-4 py-3">
          <p className="text-[12px] text-white/60">{account.label}</p>
          <p className="figure mt-1 text-[22px]">{formatPaise(account.balance_paise)}</p>
        </div>
      )}
      <fieldset className="mt-6">
        <legend className="text-[13px] text-white/70">Pay to</legend>
        <div className="mt-2 max-h-[208px] space-y-1 overflow-y-auto">
          {persona.payees.map((p) => (
            <label
              key={p.account_id}
              className={`flex cursor-pointer items-center gap-3 rounded-xl px-3 py-2.5 transition-colors ${
                payee === p.account_id ? 'bg-white/[.12]' : 'hover:bg-white/[.06]'
              }`}
            >
              <input
                type="radio"
                name="payee"
                value={p.account_id}
                checked={payee === p.account_id}
                onChange={() => {
                  setPayee(p.account_id);
                }}
                className="sr-only"
              />
              <span
                aria-hidden="true"
                className="flex size-9 shrink-0 items-center justify-center rounded-full bg-white/10 text-[14px] font-semibold"
              >
                {p.label.slice(0, 1).toUpperCase()}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-[15px]">{p.label}</span>
                {p.note && <span className="block text-[12px] text-white/55">{p.note}</span>}
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      <label className="mt-5 block text-[13px] text-white/70" htmlFor="amount">
        Amount
      </label>
      <div className="mt-2 flex items-baseline gap-1 border-b border-white/25 pb-1 focus-within:border-white">
        <span className="text-[28px] text-white/60">₹</span>
        <input
          id="amount"
          inputMode="decimal"
          autoComplete="off"
          placeholder="0"
          value={rupees}
          onChange={(e) => {
            setRupees(e.target.value.replace(/[^\d.,]/g, ''));
          }}
          className="figure w-full bg-transparent text-[28px] outline-none placeholder:text-white/30"
        />
      </div>
      <div role="radiogroup" aria-label="Channel" className="mt-4 flex gap-1.5">
        {CHANNELS.map((c) => (
          <button
            key={c}
            type="button"
            role="radio"
            aria-checked={channel === c}
            onClick={() => {
              setChannel(c);
            }}
            className={`h-8 rounded-full px-3 text-[12px] transition-colors ${
              channel === c ? 'bg-white text-[#0e0e10]' : 'text-white/70 hover:text-white'
            }`}
          >
            {CHANNEL_LABEL[c] ?? humanise(c)}
          </button>
        ))}
      </div>
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-[#f08a9b]">
          {error.message}
        </p>
      )}
      <button
        type="submit"
        disabled={!valid || busy}
        className="mt-auto h-[52px] w-full rounded-full bg-white text-[16px] font-semibold text-[#0e0e10] transition-transform duration-100 active:scale-[0.98] disabled:opacity-40"
      >
        {busy ? 'Paying…' : valid ? `Pay ${formatPaise(amount)}` : 'Pay'}
      </button>
    </form>
  );
}

/** What DELPHI makes of the payment, live (docs/08 §6): the posterior leads. */
export function DelphiPanel({
  sessionId,
  persona,
}: {
  sessionId: string | null;
  persona?: Persona | null;
}) {
  if (sessionId === null) return <IdlePanel persona={persona ?? null} />;
  return <LivePanel sessionId={sessionId} />;
}

const LANGUAGES: Readonly<Record<string, string>> = {
  en: 'English',
  hi: 'Hindi',
  te: 'Telugu',
  ta: 'Tamil',
};

/** Before a payment: who is paying, and what DELPHI will weigh (the active KB's hypotheses). */
function IdlePanel({ persona }: { persona: Persona | null }) {
  const kb = useActiveKbDisplay();
  const hypotheses = Object.entries(kb.data?.styles ?? {});
  const balance = persona?.accounts.reduce((sum, a) => sum + a.balance_paise, 0) ?? 0;
  return (
    <div className="space-y-6">
      {persona && (
        <section aria-labelledby="customer-title" className="rounded-md bg-surface p-8">
          <h2 id="customer-title" className="text-[20px] text-muted">
            The customer
          </h2>
          <p className="mt-4 text-[26px] font-semibold tracking-tight">{persona.name}</p>
          <dl className="mt-6 grid grid-cols-2 gap-x-8 gap-y-5 sm:grid-cols-4">
            <Fact label="Language">{LANGUAGES[persona.language] ?? persona.language}</Fact>
            <Fact label="Vulnerable">{persona.vulnerable ? 'Yes: losses weigh more' : 'No'}</Fact>
            <Fact label="Balance">
              <span className="figure">{formatPaise(balance)}</span>
            </Fact>
            <Fact label="Saved payees">
              <span className="figure">{persona.payees.length}</span>
            </Fact>
          </dl>
        </section>
      )}
      <section aria-labelledby="weighs-title" className="rounded-md bg-surface p-8">
        <h2 id="weighs-title" className="text-[20px] text-muted">
          What DELPHI weighs
        </h2>
        <p className="mt-3 max-w-xl text-[15px] leading-relaxed text-soft">
          Every payment starts as a spread of belief over these explanations. Each answer the
          customer gives moves it, until one decision is cheapest.
        </p>
        {kb.isError && (
          <div className="mt-4">
            <ProblemAlert error={kb.error} title="Could not load the knowledge base" />
          </div>
        )}
        <ul className="mt-6 grid gap-x-8 gap-y-3 sm:grid-cols-2">
          {hypotheses.map(([key, style]) => (
            <li key={key} className="flex items-center gap-3 text-[15px]">
              <span
                aria-hidden="true"
                className="h-5 w-1.5 shrink-0 rounded-[2px]"
                style={{ backgroundColor: forDark(style.color) }}
              />
              {style.name}
            </li>
          ))}
        </ul>
        <p className="mt-8 border-t border-rule pt-5 text-[14px] text-muted">
          Pay from the phone to watch it decide.
        </p>
      </section>
    </div>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="mt-1 text-[15px]">{children}</dd>
    </div>
  );
}

function LivePanel({ sessionId }: { sessionId: string }) {
  const session = useGuardianSession(sessionId);
  const kb = useKbDisplay(session.data?.kb_version_id);
  if (session.isPending)
    return <p className="rounded-md bg-surface p-8 text-muted">Opening the session…</p>;
  if (session.isError)
    return (
      <div className="rounded-md bg-surface p-8">
        <ProblemAlert error={session.error} title="Could not load the session" />
      </div>
    );
  return <Thinking session={session.data} styles={kb.data?.styles ?? {}} />;
}

function Thinking({ session, styles }: { session: GuardianSessionView; styles: HypothesisStyles }) {
  const posterior = session.posterior;
  const leading = session.leading_hypothesis;
  const leadShare =
    leading && posterior
      ? (posterior[leading] ?? 0) / Object.values(posterior).reduce((a, b) => a + b, 0)
      : null;
  const planned = session.probe_runs.filter((r) => r.candidates.length > 0);
  const current = planned.at(-1);
  const answered = session.probe_runs.filter((r) => r.outcome);
  return (
    <div className="space-y-6">
      <section aria-labelledby="belief-title" className="rounded-md bg-surface p-8">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="belief-title" className="text-[20px] text-muted">
            What DELPHI believes
          </h2>
          <p className="text-[13px] text-muted">
            <MoneyText paise={session.payment.amount_paise} /> ·{' '}
            {CHANNEL_LABEL[session.payment.channel] ?? humanise(session.payment.channel)}
          </p>
        </div>
        {leading && leadShare !== null && (
          <p className="mt-5 flex flex-wrap items-baseline gap-x-4">
            <span className="figure text-[40px] font-medium leading-none">
              {percent(leadShare)}
            </span>
            <span className="text-[18px]">{styles[leading]?.name ?? humanise(leading)}</span>
          </p>
        )}
        {posterior && (
          <div className="mt-5 space-y-4">
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
        )}
        <p aria-live="polite" className="mt-6 text-[15px]">
          {session.decision ? (
            <>
              Decided: <span className="font-semibold">{decisionText(session.decision)}</span>
              {session.terminal_reason && (
                <span className="text-muted"> · {humanise(session.terminal_reason)}</span>
              )}
            </>
          ) : current ? (
            <>
              Asking: <span className="font-semibold">{humanise(current.probe_key)}</span>
              <span className="text-muted"> · the question worth most right now</span>
            </>
          ) : (
            <span className="text-muted">Reading the payment’s passive signals…</span>
          )}
        </p>
      </section>

      {answered.length > 0 && (
        <section aria-labelledby="answers-title" className="rounded-md bg-surface p-8">
          <h2 id="answers-title" className="text-[20px] text-muted">
            What the customer said
          </h2>
          <ol className="mt-4">
            {answered.map((run) => (
              <li
                key={run.id}
                className="flex items-baseline justify-between gap-4 border-b border-rule py-2.5 last:border-0"
              >
                <span className="min-w-0 truncate">{humanise(run.probe_key)}</span>
                <span className="text-soft">{humanise(run.outcome ?? '')}</span>
                <span className="figure w-24 shrink-0 text-right text-muted">
                  {run.info_yield_bits === null ? '—' : `${run.info_yield_bits.toFixed(2)} bits`}
                </span>
              </li>
            ))}
          </ol>
        </section>
      )}

      {current && !session.decision && (
        <section aria-labelledby="why-title" className="rounded-md bg-surface p-8">
          <h2 id="why-title" className="text-[20px] text-muted">
            Why this question
          </h2>
          <div className="mt-4 overflow-x-auto">
            <CandidatesTable
              caption={`Candidates for step ${String(current.seq)}`}
              candidates={parseCandidates(current.candidates)}
              selectedBy={current.selected_by}
              propensity={current.propensity}
            />
          </div>
        </section>
      )}

      {session.decision && session.risk_by_decision && (
        <section aria-labelledby="risk-title" className="rounded-md bg-surface p-8">
          <h2 id="risk-title" className="text-[20px] text-muted">
            Expected loss by decision
          </h2>
          <div className="mt-4">
            <RiskTable risks={session.risk_by_decision} taken={session.decision} />
          </div>
        </section>
      )}
    </div>
  );
}
