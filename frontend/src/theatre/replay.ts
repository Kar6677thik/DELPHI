import s1 from './replays/s1.json';

/**
 * A recorded Guardian session, step by step, for the Theatre (#99 follow-up, the judges' view).
 *
 * `replays/s1.json` was produced by running the real engine (KB v1, the real planner) on the
 * ENG-S1 golden fixtures (tests/golden/test_story_behaviour.py): Kamala Devi, coached. Every
 * posterior, candidate value and the decision are the engine's own numbers; only the prior and
 * Kamala's answers are the fixture's inputs.
 */
export interface ReplayHypothesis {
  key: string;
  name: string;
  color: string;
}

export interface ReplayCandidate {
  source_key: string;
  net_value_paise: number;
  voi_paise: number | null;
  cost_paise: number;
  settle_pct: number | null;
}

export interface ReplayStep {
  kind: 'prior' | 'passive' | 'active' | 'decision';
  probe_key?: string;
  outcome?: string;
  decision?: string;
  posterior: Record<string, number>;
  entropy_bits: number;
  info_bits?: number;
  text?: string | null;
  options?: { key: string; text: string }[];
  tap?: string | null;
  candidates?: ReplayCandidate[];
  risk_by_decision?: Record<string, number>;
}

export interface Replay {
  story: string;
  title: string;
  persona: { name: string; age: number; language: string; vulnerable: boolean };
  payment: {
    amount_paise: number;
    channel: string;
    payee: string;
    payee_note: string;
    time: string;
  };
  hypotheses: ReplayHypothesis[];
  steps: ReplayStep[];
}

export const S1 = s1 as Replay;

/** What the presenter's caption says at each step: the story, in one line. */
const CAPTIONS: Record<string, string> = {
  prior:
    '₹4,80,000 at 23:40, to a payee added 6 minutes ago. The sensing model takes a first read.',
  BENEFICIARY_INTEL: 'The payee’s account has no fraud record. Yet.',
  COUNTERPARTY_PHONE_RISK: 'The payee’s phone carries no risk signal either.',
  DEVICE_POSTURE: 'It’s her own phone. Nothing looks unusual.',
  SESSION_BEHAVIOUR: 'She types normally. DELPHI leans towards genuine.',
  CALL_STATUS: 'But she is on a call, with a number she has never called before.',
  PURPOSE_QUESTION:
    'DELPHI asks what the money is for. She answers as the caller coached her: family.',
  SPELL_BREAKER_DIGITAL_ARREST:
    'One question breaks the spell. She hesitates, and switches to the caller’s app.',
  decision: 'Protective hold. The money stays with Kamala, and the bank calls her back.',
};

export function caption(step: ReplayStep): string {
  if (step.kind === 'prior' || step.kind === 'decision') return CAPTIONS[step.kind] ?? '';
  return CAPTIONS[step.probe_key ?? ''] ?? '';
}

/** How long each step holds before the next, in ms: passive signals land fast, questions wait. */
export function holdMs(step: ReplayStep): number {
  switch (step.kind) {
    case 'prior':
      return 4200;
    case 'passive':
      return step.probe_key === 'CALL_STATUS' ? 4600 : 2600;
    case 'active':
      return 7000;
    case 'decision':
      return 12000;
  }
}

/** The leading hypothesis and its share. */
export function leading(posterior: Record<string, number>): [string, number] {
  const total = Object.values(posterior).reduce((a, b) => a + b, 0) || 1;
  const [key, p] = Object.entries(posterior).sort((a, b) => b[1] - a[1])[0] ?? ['', 0];
  return [key, p / total];
}
