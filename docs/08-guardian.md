# 08 — Guardian loop (payment time)

Research §9.3–9.4. Module `delphi.guardian`. Decision profile `GUARDIAN_PAYMENT`.

## 1. Entry points

1. **Mock phone** (demo): `POST /api/v1/guardian/sessions` with a payment initiated by a
   persona. The response streams to the phone and to the Guardian monitor over SSE.
2. **Simulation / autoplay**: the worker submits live-window payments through the same
   application service, with `SimCustomerChannel` answering probes from response models.
3. **Evaluation harness**: calls the same application service with a `VirtualClock` and in-memory
   repositories (doc 22). The harness uses the same code path; there is no second implementation.

## 2. Procedure

```
1  create session (status CREATED); pin the ACTIVE kb_version (and SHADOW, if any, for shadow eval)
2  prior ← sensing.prior(payment)                       # doc 13; persist p_model, q, u, w
3  if fast_path(prior, amount, q): decide RELEASE (terminal_reason FAST_PATH); go to 9
4  status PASSIVE_PROBING: run all eligible passive probes in parallel
     each: adapter.fetch(payment) with timeout 300 ms → classify → outcome
     on timeout or error: ProbeRun ERROR, no update
     apply updates in KB source order; persist each ProbeRun with posterior before/after
5  status PLANNING: result ← engine.plan_next(state, kb, rng)       # doc 06 §6, §10
     persist the candidates table on the next ProbeRun (or on the session if STOP)
6  if STOP: decide d*(posterior) (terminal_reason STOP or BUDGET_EXHAUSTED); go to 9
7  status AWAITING_CUSTOMER: send the probe to the CustomerChannel
     (question variant chosen per doc 07 §9, rendered in the customer's language)
     wait for the response, up to active_probe_response_timeout_s (60 s, not time-scaled);
     for COOLING_OFF the wait is cooling_off_window_s × time scale + cooling_off_extra_timeout_s,
     and that window does not count towards session_max_duration_s
       response → classify → update → go to 5
       timeout  → step-ups: outcome FAILED_OR_ABANDONED; others: ProbeRun TIMED_OUT, no update → go to 5
       customer cancels → status CANCELLED_BY_CUSTOMER; payment CANCELLED; go to 10
8  if session age > session_max_duration_s at any point: decide d*(posterior), terminal_reason SESSION_TIMEOUT
9  apply the decision: payment status; on HOLD_FOR_REVIEW / PROTECTIVE_HOLD → navigator.open_case(...)
     on PROTECTIVE_HOLD → the phone shows the scam-education screen (§7)
     if terminal P(UNEXPLAINED) ≥ 0.35 → enqueue for the Scout (flag session.unexplained = true)
10 shadow evaluation (if a SHADOW KB exists): doc 11 §5
11 write audit entries; emit SSE events; the DD pack is available
```

Steps 4–9 run as one asyncio task per session in the api process. After every step the session
row and its probe runs are committed.

**Implementation** (`delphi.guardian.service.GuardianService`, #94). One method per step, each in
its own transaction:

- `start` (steps 1–3) commits the CREATED session on its own first, so a later failure still has
  a session to hold, then builds the prior from `PriorPort` (the sensing inference, #92):
  `with_payer_mule` → `engine.build_prior`. It persists `p_model`, `q`, `u`, the KB-only shares,
  the top features and the prior, and takes the fast path on `p_model(GENUINE)`.
- `passive` (step 4) runs the eligible passive probes through `delphi.probes.api`. It runs only
  for a CREATED session, so a resumed call is harmless. Each probe run stores VoI_1 and the net
  value against the prior, for analytics (doc 06 §9).
- `plan` (steps 5, 6, 8) either decides, or stores the chosen active probe as a PENDING probe run
  carrying the candidates table, ready to issue. On STOP the last candidates table goes into the
  `guardian.session.decided` audit entry, since the session row has no candidates column.
- **RNG.** Each planning step draws from `default_rng([rng_seed, number of active runs so far])`,
  a stream derived from the session's seed (doc 06 §10). A resumed or replayed session therefore
  draws the same numbers without persisting generator state.
- **The decision** (step 9) sets the payment's status through `sim.api.apply_guardian_decision`,
  opens the case through `navigator.api.open_case` (idempotent for the same session), sets the
  Scout flag, and writes the audit entry and the event in the same transaction.
- **Fail-safe** (FR-GDN-13). Any error in a step rolls that step back, and `fail_safe` then decides
  HOLD_FOR_REVIEW with terminal reason ENGINE_ERROR in a fresh transaction, without running the
  engine: the payment is HELD and a STANDARD case is opened. A finished session is left alone.
  Planning a session that isn't PLANNING is a caller error (409), not an engine error.
- **Active probes** (step 7). `issue` turns a PENDING run into AWAITING_CUSTOMER and returns
  what the channel shows: the question variant (doc 07 §9, rotated over the payer's earlier
  sessions), or a fresh OTP code (the run stores `sha256(probe_run_id:code)` and never the code;
  a wrong attempt is masked too). `respond` checks the client signals (§4), classifies (§5),
  updates and returns to PLANNING; a COOLING_OFF cancel ends the session. `timeout` fails a step-up
  (FAILED_OR_ABANDONED, with an update) and records any other probe as TIMED_OUT with no update.
  `cancel` ends the session CANCELLED_BY_CUSTOMER, with the payment CANCELLED. A bad answer, an
  implausible timing or a wrong state is a caller error (400/409), never an ENGINE_ERROR hold.
  The screen for each non-question active probe (OTP, BIOMETRIC, COOLING_OFF) is fixed in code
  (`guardian.active`), since probes are code (ADR-0006). On api start-up, sessions in `PASSIVE_PROBING` or
`PLANNING` are resumed. Sessions stuck in `AWAITING_CUSTOMER` for longer than the timeout are
resolved as timed out (FR-GDN-14).

## 3. Fast path

`RELEASE` immediately when **all** of these hold: p_model(GENUINE) ≥ 0.995, amount ≤ ₹10,000,
and q < 0.90.

The threshold applies to **p_model**, the sensing model's probability (doc 13), not to the prior
p0. The prior always reserves the open mass u ≥ `open_mass.base` (0.02) for typologies the model
lacks and for UNEXPLAINED (doc 06 §11), so p0(GENUINE) ≤ 0.98 and a p0 threshold of 0.995 could
never be met. Because q < 0.90 also holds, u is exactly its base, so the rule is equivalent to
p0(GENUINE) ≥ 0.995 · (1 − 0.02) ≈ 0.975. The session is still persisted with its prior, so fast-path decisions are auditable
and count in the metrics.

## 4. Customer channel contract (mock phone)

SSE event to the phone (`GET /api/v1/phone/stream?customer_id=…`):

```json
{ "type": "probe.requested", "session_id": "…", "probe_run_id": "…",
  "probe_key": "SPELL_BREAKER_DIGITAL_ARREST", "ui": "QUESTION",
  "language": "te", "variant_id": "SBDA-te-2",
  "text": "…", "options": [{"key": "YES", "text": "…"}, {"key": "NO", "text": "…"}],
  "timeout_s": 60 }
```

`ui` values: `QUESTION` (purpose, spell-breakers, collect intent), `OTP` (the mock SMS shows a
6-digit code), `BIOMETRIC` (a "touch sensor" button plus a cancel button), `COOLING_OFF`
(countdown with "Cancel payment", "Pay another way" and "Increase my limit" buttons).

Response: `POST /api/v1/guardian/sessions/{id}/probe-responses`

```json
{ "probe_run_id": "…",
  "answer": { "option_key": "NO" } | { "otp": "123456" } | { "biometric": "MATCH" | "CANCEL" }
          | { "cooling_off": { "cancelled": false, "other_channel_attempts": 1, "limit_increase_requests": 0, "send_now_presses": 2 } },
  "client_signals": { "shown_at_ms": 1730390400123, "answered_at_ms": 1730390411456,
                      "visibility_hidden_count": 1, "paste_count": 0 } }
```

**OTP protocol.**

1. When `OTP_STEP_UP` is issued, the server generates a 6-digit code (CSPRNG) and stores its
   hash on the probe run.
2. It "sends" the code as an `otp.sms` SSE event to the phone, which shows it as a mock SMS
   notification. Simulated customers receive it internally.
3. Each attempt is one POST with `{ "otp": "123456" }`. A wrong code returns 200 with
   `attempts_remaining`, and the run stays `AWAITING_CUSTOMER`.
4. The run completes on a correct code (`PASSED`), on `otp_max_attempts` (3) wrong codes, or
   on timeout (both `FAILED_OR_ABANDONED`).

The server computes `response_latency_s = (answered_at_ms − shown_at_ms)/1000`, and rejects
negative values or values more than 10 s longer than the server-observed wall time
(`400 INVALID_CLIENT_SIGNALS`). `app_switch_count` = `visibility_hidden_count`.

## 5. Outcome classifiers (deterministic; `delphi.probes.classifiers`)

Thresholds live in `kb/engine.yaml` under `classifiers:` (defaults below).

| Source | Rule (first match wins) |
|---|---|
| `BENEFICIARY_INTEL` | payee account in the sim suspect registry → LISTED_SUSPECT; else p_mule(payee) ≥ 0.50 → HIGH_MULE_SCORE; else payee is a registered merchant or an established payee (≥ 3 earlier payments from ≥ 3 distinct customers over ≥ 90 days) → CLEAN_KNOWN_ENTITY; else CLEAN_UNKNOWN |
| `COUNTERPARTY_PHONE_RISK` | sim FRI category of the phone linked to the payee VPA: `VERY_HIGH` → VERY_HIGH; `HIGH`/`MEDIUM` → MEDIUM_HIGH; else LOW_OR_UNKNOWN |
| `DEVICE_POSTURE` | telemetry remote_access_active or screen_share_active → REMOTE_ACCESS_ACTIVE; else device first_seen_at within 7 days → NEW_DEVICE; else KNOWN_DEVICE |
| `SESSION_BEHAVIOUR` | paste on the account/VPA field, or max field dwell > 20 s → HESITANT_OR_PASTED; else typing-cadence z-score vs the customer's 30-day baseline > 3.0 → CADENCE_MISMATCH; else NORMAL |
| `CALL_STATUS` | on a call and the number is not in the customer's contacts → ON_CALL_UNKNOWN_NUMBER; on a call with a contact → ON_CALL_KNOWN_CONTACT; else NOT_ON_CALL |
| `OTP_STEP_UP` | correct code within 3 attempts and before timeout → PASSED; else FAILED_OR_ABANDONED |
| `BIOMETRIC_STEP_UP` | `MATCH` → PASSED; `CANCEL`/timeout → FAILED_OR_ABANDONED. (Sim: match succeeds with the persona's configured probability) |
| `PURPOSE_QUESTION` | option RECEIVING_REFUND_OR_PRIZE → BELIEVES_RECEIVING_MONEY; option ∈ {INVESTMENT, GOVT_POLICE_OR_COURT_FEE, SAFE_ACCOUNT_TRANSFER, CUSTOMS_OR_PARCEL} → HIGH_RISK_PURPOSE; latency < 1.5 s or > 15 s or app_switch_count ≥ 1 → TIMING_ANOMALOUS; else PLAUSIBLE_NORMAL_TIMING |
| `SPELL_BREAKER_*` | YES → ANSWERED_YES; latency > 8 s or app_switch_count ≥ 1 → HESITATION_OR_APP_SWITCH; else ANSWERED_NO_NORMAL |
| `COOLING_OFF` | cancelled → CANCELLED_BY_CUSTOMER (this also ends the session, §2 step 7); other_channel_attempts + limit_increase_requests ≥ 1, or send_now_presses ≥ 2 → PERSISTED_OR_ESCALATED; else WAITED_NORMALLY. Window = 600 s × time scale |
| `COLLECT_INTENT_CHECK` | YES_RECEIVING → EXPECTS_TO_RECEIVE; else UNDERSTANDS_PAYING |

## 6. What the Guardian monitor shows per session

- The hypothesis bar over time: one row per step, stacked to 100%, labels plus percentages
  (never colour alone).
- The candidates table at each planning step: source, eligible / ineligible reason, cost,
  VoI_1, net value, settle %, and selected (GREEDY/EXPLORE, propensity).
- The decision with `risk_by_decision` ("Release would cost an expected ₹1,761; hold ₹6,323").
- Baseline shadow verdicts for comparison: what B1 and B2 (doc 22 §2) would have done with this
  payment, computed on the fly. This drives the demo line "OTP would pass".

## 7. Customer-facing screens after a decision (mock phone)

| Decision | Phone shows |
|---|---|
| RELEASE | "Payment sent" |
| HOLD_FOR_REVIEW | "We're checking this payment. Most checks finish within 4 hours. You'll get a call from our verified number 1800-XXX." |
| PROTECTIVE_HOLD | The scam-education screen in the customer's language: "No police, CBI, court or RBI officer will ever ask you to pay or stay on a video call. Hang up. We are holding this payment to protect you and will call you from 1800-XXX." |
| DECLINE | "This payment could not be completed. Please call 1800-XXX." |

## 8. Requirements covered

FR-GDN-01 … FR-GDN-15 (doc 03).
