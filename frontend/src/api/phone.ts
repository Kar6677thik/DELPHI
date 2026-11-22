import { useMutation, useQuery } from '@tanstack/react-query';

import { apiFetch, getJson } from './client';
import type { GuardianSessionView } from './types';

/*
 * The customer channel of the mock phone (docs/08 §4, docs/16 "Guardian").
 *
 * These endpoints arrive with the Guardian API (#98), so they are not in backend/openapi.json
 * yet and their types are written here from docs/08 §4 and docs/16. When #98 lands they move to
 * the generated types (`pnpm gen:api`) and this block goes (#99 part 2).
 */

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
  language: string;
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

/** `session.decided`: the session's decision, which the phone turns into its last screen. */
export interface SessionDecided {
  session_id: string;
  decision: string;
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
export interface ClientSignals {
  shown_at_ms: number;
  answered_at_ms: number;
  visibility_hidden_count: number;
  paste_count: number;
}

export interface PersonaAccount {
  id: string;
  label: string;
  balance_paise: number;
}

export interface PersonaPayee {
  account_id: string;
  label: string;
  /** e.g. "Saved 2 years ago", "New today": what the customer's app shows under the name. */
  note?: string | null;
}

export interface PersonaDevice {
  id: string;
  label: string;
}

/** `GET /phone/personas`: the demo's customers with their accounts, payees and devices. */
export interface Persona {
  customer_id: string;
  name: string;
  language: string;
  vulnerable: boolean;
  accounts: readonly PersonaAccount[];
  payees: readonly PersonaPayee[];
  devices: readonly PersonaDevice[];
}

export const CHANNELS = ['UPI_PUSH', 'IMPS', 'NEFT'] as const;
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
}

export const PHONE_QUERY_KEY = ['phone'] as const;

export function usePersonas() {
  return useQuery({
    queryKey: [...PHONE_QUERY_KEY, 'personas'],
    queryFn: () => getJson<readonly Persona[]>('/phone/personas'),
    retry: false, // a 404 before #98 is final; say so rather than spin
  });
}

async function postForJson<T>(path: string, body: unknown): Promise<T> {
  const response = await apiFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return (await response.json()) as T;
}

/** Pay: `POST /guardian/sessions` starts the Guardian on the payment (docs/16). */
export function useStartPayment() {
  return useMutation({
    mutationFn: (payment: PaymentDraft) =>
      postForJson<GuardianSessionView>('/guardian/sessions', {
        payment: { ...payment, telemetry: {} },
      }),
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
    }) =>
      postForJson<GuardianSessionView>(
        `/guardian/sessions/${encodeURIComponent(args.sessionId)}/probe-responses`,
        { probe_run_id: args.probeRunId, answer: args.answer, client_signals: args.signals },
      ),
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
