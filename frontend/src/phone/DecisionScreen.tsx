/**
 * What the customer sees once the Guardian has decided (docs/08 §7), word for word. The
 * protective-hold screen is scam education: it is the one screen that speaks against the
 * caller. Its translations come with the reviewed question variants (#4); until then it is
 * shown in English.
 */
interface Copy {
  title: string;
  body: string;
  tone: 'done' | 'wait' | 'protect' | 'stop';
}

const COPY: Record<string, Copy> = {
  RELEASE: { title: 'Payment sent', body: '', tone: 'done' },
  HOLD_FOR_REVIEW: {
    title: 'We’re checking this payment',
    body: 'Most checks finish within 4 hours. You’ll get a call from our verified number 1800-XXX.',
    tone: 'wait',
  },
  PROTECTIVE_HOLD: {
    title: 'Hang up.',
    body: 'No police, CBI, court or RBI officer will ever ask you to pay or stay on a video call. We are holding this payment to protect you and will call you from 1800-XXX.',
    tone: 'protect',
  },
  DECLINE: {
    title: 'This payment could not be completed',
    body: 'Please call 1800-XXX.',
    tone: 'stop',
  },
};

const CANCELLED: Copy = {
  title: 'Payment cancelled',
  body: 'No money has left your account.',
  tone: 'done',
};

function Glyph({ tone }: { tone: Copy['tone'] }) {
  const common = 'size-14';
  if (tone === 'done')
    return (
      <svg viewBox="0 0 56 56" className={common} aria-hidden="true">
        <circle cx="28" cy="28" r="26" fill="none" stroke="currentColor" strokeWidth="2" />
        <path d="M17 29l7 7 15-16" fill="none" stroke="currentColor" strokeWidth="2.5" />
      </svg>
    );
  if (tone === 'wait')
    return (
      <svg viewBox="0 0 56 56" className={common} aria-hidden="true">
        <circle cx="28" cy="28" r="26" fill="none" stroke="currentColor" strokeWidth="2" />
        <path d="M28 14v15l9 6" fill="none" stroke="currentColor" strokeWidth="2.5" />
      </svg>
    );
  if (tone === 'protect')
    return (
      <svg viewBox="0 0 56 56" className={common} aria-hidden="true">
        <path
          d="M28 4l20 8v14c0 13-9 22-20 26C17 48 8 39 8 26V12z"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
        />
        <path d="M28 18v13M28 37v2" stroke="currentColor" strokeWidth="3" />
      </svg>
    );
  return (
    <svg viewBox="0 0 56 56" className={common} aria-hidden="true">
      <circle cx="28" cy="28" r="26" fill="none" stroke="currentColor" strokeWidth="2" />
      <path d="M19 19l18 18M37 19L19 37" stroke="currentColor" strokeWidth="2.5" />
    </svg>
  );
}

export function DecisionScreen({
  decision,
  onDone,
}: {
  /** The Guardian's decision, or `null` when the customer cancelled. */
  decision: string | null;
  onDone: () => void;
}) {
  const copy = decision === null ? CANCELLED : (COPY[decision] ?? COPY.HOLD_FOR_REVIEW);
  const protect = copy?.tone === 'protect';
  if (!copy) return null;
  return (
    <div
      className={`-mx-6 -mb-6 flex h-[calc(100%+24px)] flex-col px-6 pb-6 ${
        protect ? 'bg-[#f2f2f3] text-[#0e0e10]' : ''
      }`}
    >
      <div className={`pt-10 ${protect ? 'text-[#b4233c]' : 'text-white'}`}>
        <Glyph tone={copy.tone} />
      </div>
      <h2
        aria-live="polite"
        className={`mt-6 font-semibold tracking-tight ${protect ? 'text-[34px] leading-[1.05]' : 'text-[24px] leading-tight'}`}
      >
        {copy.title}
      </h2>
      {copy.body && (
        <p
          className={`mt-4 text-[16px] leading-[1.5] ${protect ? 'text-[#26262a]' : 'text-white/75'}`}
        >
          {copy.body}
        </p>
      )}
      <button
        type="button"
        onClick={onDone}
        className={`mt-auto h-[52px] w-full rounded-full text-[16px] font-semibold transition-transform duration-100 active:scale-[0.98] ${
          protect ? 'bg-[#0e0e10] text-white' : 'bg-white text-[#0e0e10]'
        }`}
      >
        Done
      </button>
    </div>
  );
}
