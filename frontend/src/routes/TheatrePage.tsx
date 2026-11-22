import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router';

import { formatPaise } from '../components/MoneyText';
import { forDark } from '../components/HypothesisBar';
import { decisionText, humanise } from '../lib/format';
import { DecisionScreen } from '../phone/DecisionScreen';
import { PhoneFrame } from '../phone/PhoneFrame';
import { caption, holdMs, leading, type ReplayStep, S1 } from '../theatre/replay';

const Oracle = lazy(() => import('../theatre/Oracle').then((m) => ({ default: m.Oracle })));

/**
 * The Theatre (`/theatre`): the judges' view of story S-1 (docs/25 M9 pitch, #132).
 *
 * The customer's phone on the left, DELPHI's belief in the middle as light (the Oracle), and
 * what DELPHI is weighing on the right. It plays the recorded engine run step by step, so it
 * also serves as the backup when a live demo fails. Space pauses, the arrows step, R restarts.
 */
export function TheatrePage() {
  const replay = S1;
  // `?step=N` opens at a step (the presenter's jump), `?paused` holds it there.
  const [search] = useSearchParams();
  const [index, setIndex] = useState(() =>
    Math.min(Math.max(Number(search.get('step') ?? 0) || 0, 0), replay.steps.length - 1),
  );
  const [playing, setPlaying] = useState(() => !search.has('paused'));
  const step = replay.steps[index] ?? replay.steps[0];

  useEffect(() => {
    if (!playing || !step) return undefined;
    if (index >= replay.steps.length - 1) return undefined;
    const timer = setTimeout(() => {
      setIndex((i) => Math.min(i + 1, replay.steps.length - 1));
    }, holdMs(step));
    return () => {
      clearTimeout(timer);
    };
  }, [index, playing, step, replay.steps.length]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === ' ') {
        e.preventDefault();
        setPlaying((p) => !p);
      } else if (e.key === 'ArrowRight') {
        setIndex((i) => Math.min(i + 1, replay.steps.length - 1));
      } else if (e.key === 'ArrowLeft') {
        setIndex((i) => Math.max(i - 1, 0));
      } else if (e.key.toLowerCase() === 'r') {
        setIndex(0);
        setPlaying(true);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
    };
  }, [replay.steps.length]);

  if (!step) return null;
  const decided = step.kind === 'decision' ? (step.decision ?? null) : null;
  const [leadKey, leadShare] = leading(step.posterior);
  const lead = replay.hypotheses.find((h) => h.key === leadKey);

  return (
    <main className="fixed inset-0 overflow-hidden bg-[#050507] text-ink">
      <div className="absolute inset-0">
        <Suspense fallback={null}>
          <Oracle hypotheses={replay.hypotheses} posterior={step.posterior} decision={decided} />
        </Suspense>
      </div>

      {/* Top: the story */}
      <header className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between px-10 pt-8">
        <div>
          <p className="text-[13px] text-white/50">DELPHI · story {replay.story}</p>
          <h1 className="mt-1 text-[28px] font-semibold tracking-tight">{replay.title}</h1>
        </div>
        <Link
          to="/"
          className="pointer-events-auto rounded-full px-4 py-2 text-[13px] text-white/50 hover:text-white"
        >
          Exit
        </Link>
      </header>

      {/* Left: the customer's phone, scaled for the stage */}
      <div className="absolute left-10 top-1/2 hidden w-[372px] origin-left -translate-y-1/2 scale-[0.82] lg:block xl:scale-90">
        <PhoneFrame clock={replay.payment.time} sms={null}>
          <PhoneStep step={step} replay={replay} />
        </PhoneFrame>
      </div>

      {/* Right: what DELPHI is weighing */}
      <aside className="absolute right-10 top-1/2 hidden w-[340px] -translate-y-1/2 lg:block">
        <Weighing step={step} />
      </aside>

      {/* Bottom: the caption and the presenter's controls */}
      <footer className="absolute inset-x-0 bottom-0 px-10 pb-8">
        <p
          key={index}
          aria-live="polite"
          className="mx-auto max-w-[900px] animate-[caption-in_.6s_ease-out] text-center text-[22px] leading-snug text-white"
        >
          {caption(step)}
        </p>
        <p className="mt-3 text-center text-[14px] text-white/55">
          {decided ? (
            <>
              Decided <span className="font-semibold text-white">{decisionText(decided)}</span>,
              with <span className="figure text-white">{(leadShare * 100).toFixed(0)}%</span> on{' '}
              <span style={{ color: lead ? forDark(lead.color) : undefined }}>{lead?.name}</span>
            </>
          ) : (
            <>
              Leading:{' '}
              <span style={{ color: lead ? forDark(lead.color) : undefined }}>{lead?.name}</span>{' '}
              <span className="figure text-white">{(leadShare * 100).toFixed(0)}%</span> ·
              uncertainty{' '}
              <span className="figure text-white">{step.entropy_bits.toFixed(2)} bits</span>
            </>
          )}
        </p>
        <nav aria-label="Story steps" className="mt-5 flex items-center justify-center gap-2">
          <button
            type="button"
            onClick={() => {
              setPlaying((p) => !p);
            }}
            className="mr-3 h-8 rounded-full px-3 text-[12px] text-white/60 hover:text-white"
          >
            {playing ? 'Pause' : 'Play'}
          </button>
          {replay.steps.map((s, i) => (
            <button
              key={`${s.kind}-${s.probe_key ?? ''}`}
              type="button"
              aria-label={`Step ${String(i + 1)}: ${s.probe_key ? humanise(s.probe_key) : humanise(s.kind)}`}
              aria-current={i === index ? 'step' : undefined}
              onClick={() => {
                setIndex(i);
              }}
              className={`h-1.5 rounded-full transition-all duration-300 ${
                i === index ? 'w-8 bg-white' : i < index ? 'w-3 bg-white/50' : 'w-3 bg-white/15'
              }`}
            />
          ))}
        </nav>
      </footer>
    </main>
  );
}

/** The phone shows what Kamala sees at this step. */
function PhoneStep({ step, replay }: { step: ReplayStep; replay: typeof S1 }) {
  if (step.kind === 'decision') {
    return <DecisionScreen decision={step.decision ?? null} onDone={() => undefined} />;
  }
  if (step.kind === 'active' && step.text) {
    return <AskedQuestion step={step} />;
  }
  return (
    <div className="flex h-full flex-col">
      <div className="rounded-2xl bg-white/[.06] px-4 py-3">
        <p className="text-[12px] text-white/60">Paying {replay.payment.payee}</p>
        <p className="figure mt-1 text-[26px]">{formatPaise(replay.payment.amount_paise)}</p>
        <p className="mt-1 text-[12px] text-white/50">{replay.payment.payee_note}</p>
      </div>
      <div className="flex flex-1 flex-col items-center justify-center text-center">
        <span
          aria-hidden="true"
          className="size-10 animate-spin rounded-full border-2 border-white/15 border-t-white"
        />
        <p className="mt-5 text-[16px]">Checking this payment</p>
        <p className="mt-1 text-[13px] text-white/60">
          {step.kind === 'passive' ? `Reading: ${humanise(step.probe_key ?? '')}` : 'One moment.'}
        </p>
      </div>
    </div>
  );
}

/** A question on the phone; after a beat, the option Kamala chose lights up. */
function AskedQuestion({ step }: { step: ReplayStep }) {
  const [tapped, setTapped] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => {
      setTapped(true);
    }, 2400);
    return () => {
      clearTimeout(t);
    };
  }, []);
  const options = step.options ?? [];
  const shown = options.length > 5 ? options.slice(0, 5) : options;
  return (
    <div key={step.probe_key} className="flex h-full flex-col">
      <h2 className="text-[20px] font-semibold leading-[1.25] tracking-tight">{step.text}</h2>
      <div className="mt-6 space-y-2.5">
        {shown.map((o) => {
          const chosen = tapped && o.key === step.tap;
          return (
            <div
              key={o.key}
              className={`flex h-12 items-center justify-center rounded-full border text-[15px] transition-all duration-300 ${
                chosen
                  ? 'scale-[1.02] border-white bg-white font-semibold text-[#0e0e10]'
                  : 'border-white/25 text-white'
              }`}
            >
              {o.text}
            </div>
          );
        })}
        {options.length > shown.length && (
          <p className="pt-1 text-center text-[12px] text-white/45">
            and {options.length - shown.length} more
          </p>
        )}
      </div>
    </div>
  );
}

/** What DELPHI is weighing: the questions it could ask, valued in rupees of avoided loss. */
function Weighing({ step }: { step: ReplayStep }) {
  const candidates = useMemo(() => step.candidates ?? [], [step]);
  const max = Math.max(...candidates.map((c) => c.net_value_paise), 1);
  if (step.kind === 'decision' && step.risk_by_decision) {
    const risks = Object.entries(step.risk_by_decision).sort((a, b) => a[1] - b[1]);
    const worst = Math.max(...risks.map(([, r]) => r), 1);
    return (
      <section aria-labelledby="loss-title" className="animate-[caption-in_.6s_ease-out]">
        <h2 id="loss-title" className="text-[14px] text-white/55">
          Expected loss of each decision
        </h2>
        <ul className="mt-4 space-y-4">
          {risks.map(([d, r], i) => (
            <li key={d}>
              <div className="flex items-baseline justify-between text-[15px]">
                <span className={i === 0 ? 'font-semibold text-white' : 'text-white/70'}>
                  {decisionText(d)}
                </span>
                <span className="figure text-white/80">{formatPaise(Math.round(r))}</span>
              </div>
              <div className="mt-1.5 h-1 rounded-full bg-white/10">
                <div
                  className={`h-full rounded-full ${i === 0 ? 'bg-white' : 'bg-white/35'}`}
                  style={{ width: `${String(Math.max(1.5, (r / worst) * 100))}%` }}
                />
              </div>
            </li>
          ))}
        </ul>
        <p className="mt-5 text-[13px] leading-relaxed text-white/55">
          DELPHI takes the decision with the least expected loss. The LLM never decides.
        </p>
      </section>
    );
  }
  if (candidates.length === 0) {
    return (
      <section aria-labelledby="signals-title">
        <h2 id="signals-title" className="text-[14px] text-white/55">
          {step.kind === 'prior' ? 'First read' : 'Passive signal'}
        </h2>
        <p className="mt-3 text-[22px] leading-tight">
          {step.kind === 'prior' ? 'Sensing model' : humanise(step.probe_key ?? '')}
        </p>
        {step.outcome && (
          <p className="figure mt-2 text-[14px] text-white/70">{humanise(step.outcome)}</p>
        )}
        {step.info_bits !== undefined && (
          <p className="mt-4 text-[13px] text-white/55">
            Uncertainty {step.info_bits >= 0 ? 'down' : 'up'}{' '}
            <span className="figure text-white">{Math.abs(step.info_bits).toFixed(2)} bits</span>
          </p>
        )}
        <p className="mt-6 text-[13px] leading-relaxed text-white/45">
          Passive signals cost the customer nothing. DELPHI reads them first.
        </p>
      </section>
    );
  }
  return (
    <section aria-labelledby="weigh-title" key={step.probe_key}>
      <h2 id="weigh-title" className="text-[14px] text-white/55">
        Which question is worth asking?
      </h2>
      <ul className="mt-4 space-y-3.5">
        {candidates.map((c, i) => (
          <li key={c.source_key} className={i === 0 ? '' : 'opacity-60'}>
            <div className="flex items-baseline justify-between gap-3 text-[14px]">
              <span className={i === 0 ? 'font-semibold text-white' : 'text-white/80'}>
                {humanise(c.source_key)}
              </span>
              <span className="figure shrink-0 text-white/80">
                {formatPaise(Math.round(c.net_value_paise))}
              </span>
            </div>
            <div className="mt-1.5 h-1 rounded-full bg-white/10">
              <div
                className="h-full rounded-full bg-accent transition-[width] duration-700"
                style={{ width: `${String((c.net_value_paise / max) * 100)}%` }}
              />
            </div>
          </li>
        ))}
      </ul>
      <p className="mt-5 text-[13px] leading-relaxed text-white/55">
        Net value: the expected loss a question avoids, minus the friction it costs the customer.
        DELPHI asks the top one.
      </p>
    </section>
  );
}
