import { useMutation, useQuery } from '@tanstack/react-query';

import { apiFetch, getJson } from './client';
import type { components } from './schema';
import type { GuardianSessionView } from './types';

/*
 * The customer channel of the mock phone (docs/08 §4, docs/16 "Guardian", #98).
 *
 * The requests and the personas use the generated types (`pnpm gen:api`). The phone stream's
 * events are not in the OpenAPI document (an SSE body has no schema there), so their types are
 * written here from docs/08 §4 and docs/16.
 */
type Schemas = components['schemas'];

/** A probe's screen type (docs/08 §4). */
export type ProbeUi = 'QUESTION' | 'OTP' | 'BIOMETRIC' | 'COOLING_OFF';

export interface ProbeOption {
  key: string;
  text: string;
}

/** `probe.requested`: the phone shows this probe's screen until it is answered or times out. */
export interface ProbeRequest {
  session_id: string;
  probe_run_id: string;
  probe_key: string;
  ui: ProbeUi;
  /** The question's language; `null` for the screens fixed in code (OTP, biometric, wait). */
  language: string | null;
  variant_id?: string | null;
  text?: string | null;
  options?: readonly ProbeOption[];
  timeout_s: number;
}

/** `otp.sms`: the mock SMS that carries the one-time code (docs/08 §4 OTP protocol). */
export interface OtpSms {
  session_id: string;
  probe_run_id: string;
  code: string;
}

/** `probe.attempt_result`: a wrong OTP, and how many tries are left. */
export interface AttemptResult {
  session_id: string;
  probe_run_id: string;
  attempts_remaining: number;
}

/**
 * `session.decided`: the session ended. `decision` is `null` when the customer cancelled
 * (`status` CANCELLED_BY_CUSTOMER); the phone turns it into its last screen.
 */
export interface SessionDecided {
  session_id: string;
  status?: string;
  decision: string | null;
  terminal_reason?: string | null;
}

export type PhoneEvent =
  | { type: 'probe.requested'; data: ProbeRequest }
  | { type: 'otp.sms'; data: OtpSms }
  | { type: 'probe.attempt_result'; data: AttemptResult }
  | { type: 'session.decided'; data: SessionDecided };

export const PHONE_EVENT_TYPES = [
  'probe.requested',
  'otp.sms',
  'probe.attempt_result',
  'session.decided',
] as const;

export interface CoolingOffAnswer {
  cancelled: boolean;
  other_channel_attempts: number;
  limit_increase_requests: number;
  send_now_presses: number;
}

/** One answer per probe screen type (docs/08 §4). */
export type ProbeAnswer =
  | { option_key: string }
  | { otp: string }
  | { biometric: 'MATCH' | 'CANCEL' }
  | { cooling_off: CoolingOffAnswer };

/** What the phone measured while the screen was up (docs/08 §4, `client_signals`). */
export type ClientSignals = Schemas['ClientSignalsIn'];

export type PersonaAccount = Schemas['PersonaAccountView'];
export type PersonaPayee = Schemas['PersonaPayeeView'];
export type PersonaDevice = Schemas['PersonaDeviceView'];
/** `GET /phone/personas`: the demo's customers with their accounts, payees and devices. */
export type Persona = Schemas['PersonaView'];

export const CHANNELS = [
  'UPI_PUSH',
  'IMPS',
  'NEFT',
] as const satisfies readonly Schemas['PaymentIn']['channel'][];
export type PayChannel = (typeof CHANNELS)[number];

/** How the customer's app names each channel. */
export const CHANNEL_LABEL: Readonly<Record<string, string>> = {
  UPI_PUSH: 'UPI',
  UPI_COLLECT_APPROVAL: 'UPI collect',
  IMPS: 'IMPS',
  NEFT: 'NEFT',
  RTGS: 'RTGS',
};

export interface PaymentDraft {
  payer_account_id: string;
  payee_account_id: string;
  amount_paise: number;
  channel: PayChannel;
  device_id: string;
  /** A demo story's scripted local time, IST "HH:MM" (docs/14 §10): the story's job sets it. */
  story_local_time?: string;
  /** The DEMO_STORY job that staged it: the server checks it and plays the story (#291). */
  story_job_id?: string;
}

/**
 * What is happening around the customer while they pay, which the bank app's SDK would report
 * (docs/08 §5). A browser cannot see the phone's call or screen, so the presenter sets it. Each
 * preset sits clearly on one side of the KB's classifier thresholds (`kb/engine.yaml`
 * `classifiers`: a field dwell over 20 s or a paste is HESITANT_OR_PASTED, a typing-cadence z
 * over 3.0 is CADENCE_MISMATCH).
 */
export type CallSituation = 'NONE' | 'UNKNOWN_NUMBER' | 'CONTACT';
export type TypingSituation = 'NATURAL' | 'HESITANT' | 'NOT_THEIR_RHYTHM';

export interface Situation {
  call: CallSituation;
  /** Screen sharing or a remote-access app is running. */
  screenShared: boolean;
  typing: TypingSituation;
}

export const CALM: Situation = { call: 'NONE', screenShared: false, typing: 'NATURAL' };

const TYPING: Readonly<
  Record<TypingSituation, { typing_cadence_z: number; paste: boolean; dwell_s: number }>
> = {
  NATURAL: { typing_cadence_z: 0.4, paste: false, dwell_s: 6 },
  // Pasted the payee's details, and lingered on a field (an instruction being read out).
  HESITANT: { typing_cadence_z: 0.8, paste: true, dwell_s: 34 },
  // Someone else's rhythm: dictated, or typed by another person.
  NOT_THEIR_RHYTHM: { typing_cadence_z: 4.1, paste: false, dwell_s: 9 },
};

/**
 * The situation a story's scripted telemetry describes (docs/14 §6), so "Around the customer"
 * starts where the story does. The typing is read against the presets above: a cadence as far
 * off as "not their rhythm", or a paste or a dwell as long as "hesitant".
 */
export function situationOf(telemetry: Schemas['TelemetryIn'] | undefined): Situation {
  if (!telemetry) return CALM;
  let call: CallSituation = 'NONE';
  if (telemetry.on_call) call = telemetry.call_counterparty_known ? 'CONTACT' : 'UNKNOWN_NUMBER';
  let typing: TypingSituation = 'NATURAL';
  if (telemetry.typing_cadence_z >= TYPING.NOT_THEIR_RHYTHM.typing_cadence_z) {
    typing = 'NOT_THEIR_RHYTHM';
  } else if (
    telemetry.paste_on_account_field ||
    telemetry.max_field_dwell_s >= TYPING.HESITANT.dwell_s
  ) {
    typing = 'HESITANT';
  }
  return {
    call,
    screenShared: telemetry.screen_share_active || telemetry.remote_access_active,
    typing,
  };
}

/** The API's bound on an app session's durations (`MAX_SESSION_S`, 24 h; docs/16). */
export const MAX_SESSION_S = 24 * 60 * 60;

/**
 * The telemetry sent with the payment, for a situation and the time spent in the app. The
 * session lasts at least as long as its longest field dwell, and at most the API's bound, so a
 * form left open overnight is not refused.
 */
export function telemetryFor(situation: Situation, sessionSeconds: number): Schemas['TelemetryIn'] {
  const typing = TYPING[situation.typing];
  return {
    typing_cadence_z: typing.typing_cadence_z,
    paste_on_account_field: typing.paste,
    max_field_dwell_s: typing.dwell_s,
    remote_access_active: situation.screenShared,
    screen_share_active: situation.screenShared,
    on_call: situation.call !== 'NONE',
    call_counterparty_known: situation.call === 'NONE' ? null : situation.call === 'CONTACT',
    session_duration_s: Math.min(
      MAX_SESSION_S,
      Math.max(typing.dwell_s, Math.round(sessionSeconds)),
    ),
  };
}

export const PHONE_QUERY_KEY = ['phone'] as const;

export function usePersonas() {
  return useQuery({
    queryKey: [...PHONE_QUERY_KEY, 'personas'],
    queryFn: () => getJson<readonly Persona[]>('/phone/personas'),
    retry: false,
  });
}

async function postForJson<T>(path: string, body: unknown): Promise<T> {
  const response = await apiFetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      // docs/16 §1: required here; a retried request with the same key is answered once.
      'Idempotency-Key': crypto.randomUUID(),
    },
    body: JSON.stringify(body),
  });
  return (await response.json()) as T;
}

/** Pay: `POST /guardian/sessions` starts the Guardian on the payment (docs/16). */
export function useStartPayment() {
  return useMutation({
    mutationFn: (args: { payment: PaymentDraft; telemetry: Schemas['TelemetryIn'] }) => {
      const body: Schemas['StartSessionRequest'] = {
        payment: { ...args.payment, telemetry: args.telemetry },
      };
      return postForJson<GuardianSessionView>('/guardian/sessions', body);
    },
  });
}

/** Answer the probe on screen (docs/08 §4). */
export function useAnswerProbe() {
  return useMutation({
    mutationFn: (args: {
      sessionId: string;
      probeRunId: string;
      answer: ProbeAnswer;
      signals: ClientSignals;
    }) => {
      const body: Schemas['ProbeResponseRequest'] = {
        probe_run_id: args.probeRunId,
        answer: args.answer,
        client_signals: args.signals,
      };
      return postForJson<Schemas['ProbeResponseView']>(
        `/guardian/sessions/${encodeURIComponent(args.sessionId)}/probe-responses`,
        body,
      );
    },
  });
}

/** The customer cancels the payment (docs/16 `POST /guardian/sessions/{id}/cancel`). */
export function useCancelPayment() {
  return useMutation({
    mutationFn: (sessionId: string) =>
      apiFetch(`/guardian/sessions/${encodeURIComponent(sessionId)}/cancel`, { method: 'POST' }),
  });
}

/** `GET /phone/stream?customer_id=` (docs/16 §3). */
export function phoneStreamUrl(customerId: string): string {
  return `/api/v1/phone/stream?customer_id=${encodeURIComponent(customerId)}`;
}
