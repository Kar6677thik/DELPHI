import { useEffect, useRef, useState } from 'react';

import type { ClientSignals, CoolingOffAnswer, ProbeAnswer, ProbeRequest } from '../api/phone';
import { useClientSignals } from './useClientSignals';

/**
 * The customer's screens for each probe `ui` type (docs/08 §4). Each one measures its client
 * signals and hands back one answer; none decides anything (ADR-0004). The probe's own text
 * comes from the server in the customer's language; the chrome around it is English
 * (docs/18 §4).
 */
export interface ProbeScreenProps {
  probe: ProbeRequest;
  onAnswer: (answer: ProbeAnswer, signals: ClientSignals) => void;
  busy?: boolean;
}

const OTP_LENGTH = 6;

/** Seconds left on the probe's timer, counting down from `timeout_s`. */
function useSecondsLeft(total: number): number {
  const [left, setLeft] = useState(total);
  useEffect(() => {
    const started = Date.now();
    const tick = setInterval(() => {
      setLeft(Math.max(0, total - Math.floor((Date.now() - started) / 1000)));
    }, 250);
    return () => {
      clearInterval(tick);
    };
  }, [total]);
  return left;
}

function TimeLeft({ total }: { total: number }) {
  const left = useSecondsLeft(total);
  return (
    <div className="mt-auto pt-6">
      <div className="h-0.5 w-full bg-white/10">
        <div
          className="h-full bg-white/60 transition-[width] duration-300 ease-linear"
          style={{ width: `${String((left / Math.max(total, 1)) * 100)}%` }}
        />
      </div>
      <p className="figure mt-2 text-[12px] text-white/60">{left} s to answer</p>
    </div>
  );
}

function Prompt({ children }: { children: React.ReactNode }) {
  return <h2 className="text-[22px] font-semibold leading-[1.25] tracking-tight">{children}</h2>;
}

const PRIMARY =
  'h-[52px] w-full rounded-full bg-white text-[16px] font-semibold text-[#0e0e10] transition-transform duration-100 active:scale-[0.98] disabled:opacity-40';
const SECONDARY =
  'h-[52px] w-full rounded-full border border-white/25 text-[16px] font-medium text-white transition-[transform,border-color] duration-100 hover:border-white/60 active:scale-[0.98] disabled:opacity-40';

export function QuestionScreen({ probe, onAnswer, busy }: ProbeScreenProps) {
  const { stamp } = useClientSignals();
  return (
    <div className="flex h-full flex-col">
      <Prompt>{probe.text ?? 'One question before this payment goes'}</Prompt>
      <div role="group" aria-label="Answers" className="mt-8 space-y-3">
        {(probe.options ?? []).map((option) => (
          <button
            key={option.key}
            type="button"
            disabled={busy}
            className={SECONDARY}
            onClick={() => {
              onAnswer({ option_key: option.key }, stamp());
            }}
          >
            {option.text}
          </button>
        ))}
      </div>
      <TimeLeft total={probe.timeout_s} />
    </div>
  );
}

export function OtpScreen({
  probe,
  onAnswer,
  busy,
  attemptsRemaining,
}: ProbeScreenProps & { attemptsRemaining?: number | null }) {
  const { stamp, onPaste } = useClientSignals();
  const [code, setCode] = useState('');
  const complete = code.length === OTP_LENGTH;
  return (
    <form
      className="flex h-full flex-col"
      onSubmit={(event) => {
        event.preventDefault();
        if (complete) onAnswer({ otp: code }, stamp());
      }}
    >
      <Prompt>{probe.text ?? 'Enter the code we sent you by SMS'}</Prompt>
      <label htmlFor="otp" className="mt-8 block text-[13px] text-white/70">
        One-time code
      </label>
      <div className="relative mt-2">
        <input
          id="otp"
          inputMode="numeric"
          autoComplete="one-time-code"
          maxLength={OTP_LENGTH}
          value={code}
          aria-describedby={attemptsRemaining != null ? 'otp-attempts' : undefined}
          onPaste={onPaste}
          onChange={(event) => {
            setCode(event.target.value.replace(/\D/g, '').slice(0, OTP_LENGTH));
          }}
          className="figure absolute inset-0 h-full w-full cursor-text text-transparent caret-transparent opacity-0"
        />
        {/* The six cells draw the field; the real input sits over them. */}
        <div aria-hidden="true" className="grid grid-cols-6 gap-2">
          {Array.from({ length: OTP_LENGTH }, (_, i) => (
            <span
              key={i}
              className={`figure flex h-14 items-center justify-center rounded-lg text-[24px] ${
                i === code.length ? 'border border-white' : 'border border-white/20'
              }`}
            >
              {code[i] ?? ''}
            </span>
          ))}
        </div>
      </div>
      {attemptsRemaining != null && (
        <p id="otp-attempts" role="alert" className="mt-3 text-[13px] text-[#f08a9b]">
          That code is wrong. {attemptsRemaining} {attemptsRemaining === 1 ? 'try' : 'tries'} left.
        </p>
      )}
      <button type="submit" disabled={!complete || busy} className={`${PRIMARY} mt-8`}>
        Confirm
      </button>
      <TimeLeft total={probe.timeout_s} />
    </form>
  );
}

function Fingerprint() {
  return (
    <svg viewBox="0 0 64 64" className="size-16" aria-hidden="true">
      <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
        <path d="M20 46c-3-5-4-10-4-15a16 16 0 0 1 32 0c0 3 0 6-1 9" />
        <path d="M25 50c-2-5-3-11-3-18a10 10 0 0 1 20 0c0 6-1 11-3 16" />
        <path d="M32 52c-1-6-2-13-2-20a2 2 0 0 1 4 0c0 7-1 13-2 20" />
        <path d="M14 22a20 20 0 0 1 36 0" />
      </g>
    </svg>
  );
}

export function BiometricScreen({ probe, onAnswer, busy }: ProbeScreenProps) {
  const { stamp } = useClientSignals();
  return (
    <div className="flex h-full flex-col">
      <Prompt>{probe.text ?? 'Confirm it is you'}</Prompt>
      <div className="flex flex-1 flex-col items-center justify-center gap-4">
        <button
          type="button"
          disabled={busy}
          className="flex size-40 items-center justify-center rounded-full border border-white/25 text-white transition-[transform,border-color] duration-150 hover:border-white active:scale-95 disabled:opacity-40"
          onClick={() => {
            onAnswer({ biometric: 'MATCH' }, stamp());
          }}
        >
          <Fingerprint />
          <span className="sr-only">Touch the sensor</span>
        </button>
        <p className="text-[14px] text-white/70">Touch the sensor</p>
      </div>
      <button
        type="button"
        disabled={busy}
        className={SECONDARY}
        onClick={() => {
          onAnswer({ biometric: 'CANCEL' }, stamp());
        }}
      >
        Cancel
      </button>
      <TimeLeft total={probe.timeout_s} />
    </div>
  );
}

const RING = 2 * Math.PI * 70;

export function CoolingOffScreen({ probe, onAnswer, busy }: ProbeScreenProps) {
  const { stamp } = useClientSignals();
  const left = useSecondsLeft(probe.timeout_s);
  const counts = useRef<Omit<CoolingOffAnswer, 'cancelled'>>({
    other_channel_attempts: 0,
    limit_increase_requests: 0,
    send_now_presses: 0,
  });
  const [note, setNote] = useState<string | null>(null);
  const sent = useRef(false);
  const finish = (cancelled: boolean) => {
    if (sent.current) return;
    sent.current = true;
    onAnswer({ cooling_off: { cancelled, ...counts.current } }, stamp());
  };
  const finishRef = useRef(finish);
  useEffect(() => {
    finishRef.current = finish;
  });
  useEffect(() => {
    if (left === 0) finishRef.current(false); // the pause is over: the payment may go
  }, [left]);

  const press = (key: keyof typeof counts.current, message: string) => {
    counts.current[key] += 1;
    setNote(message);
  };
  const share = left / Math.max(probe.timeout_s, 1);
  return (
    <div className="flex h-full flex-col">
      <Prompt>{probe.text ?? 'Take a minute before this payment goes'}</Prompt>
      <div className="relative mx-auto mt-6 size-[168px]">
        <svg viewBox="0 0 160 160" className="size-full -rotate-90" aria-hidden="true">
          <circle
            cx="80"
            cy="80"
            r="70"
            fill="none"
            stroke="rgb(255 255 255 / .1)"
            strokeWidth="4"
          />
          <circle
            cx="80"
            cy="80"
            r="70"
            fill="none"
            stroke="white"
            strokeWidth="4"
            strokeDasharray={RING}
            strokeDashoffset={RING * (1 - share)}
            className="transition-[stroke-dashoffset] duration-300 ease-linear"
          />
        </svg>
        <p className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="figure text-[44px] font-medium leading-none">{left}</span>
          <span className="mt-1 text-[12px] text-white/60">seconds</span>
        </p>
      </div>
      <p aria-live="polite" className="mt-4 min-h-5 text-center text-[13px] text-white/70">
        {note}
      </p>
      <div className="mt-auto space-y-2.5 pt-4">
        <button
          type="button"
          disabled={busy}
          className={SECONDARY}
          onClick={() => {
            press('send_now_presses', 'The payment goes when the timer ends.');
          }}
        >
          Send now
        </button>
        <div className="grid grid-cols-2 gap-2.5">
          <button
            type="button"
            disabled={busy}
            className={`${SECONDARY} text-[14px]`}
            onClick={() => {
              press('other_channel_attempts', 'Other payment methods are paused too.');
            }}
          >
            Pay another way
          </button>
          <button
            type="button"
            disabled={busy}
            className={`${SECONDARY} text-[14px]`}
            onClick={() => {
              press('limit_increase_requests', 'Limits can’t change during the pause.');
            }}
          >
            Increase my limit
          </button>
        </div>
        <button
          type="button"
          disabled={busy}
          className={PRIMARY}
          onClick={() => {
            finish(true);
          }}
        >
          Cancel payment
        </button>
      </div>
    </div>
  );
}

/** The screen for a probe, by its `ui` type. */
export function ProbeScreen(props: ProbeScreenProps & { attemptsRemaining?: number | null }) {
  switch (props.probe.ui) {
    case 'QUESTION':
      return <QuestionScreen {...props} />;
    case 'OTP':
      return <OtpScreen {...props} />;
    case 'BIOMETRIC':
      return <BiometricScreen {...props} />;
    case 'COOLING_OFF':
      return <CoolingOffScreen {...props} />;
  }
}
