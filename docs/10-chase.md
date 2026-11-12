# 10 — Jatayu (Chase) loop — golden-hour recovery

Research §9.6. Module `delphi.chase`. Maths decision: ADR-0012.

## 1. Triggers

- **Complaint**: a `complaints` row naming one or more *executed* payments (the demo story S-3
  inserts it via the scenario runner; in simulation, label arrival creates it).
- **Manual**: an INVESTIGATOR starts a chase from a disposed `CONFIRM_FRAUD` case whose payment
  had already been executed (e.g. a released payment that later turned out to be fraud).

`POST /api/v1/chase/operations {complaint_id | payment_ids[]}` creates the operation with
`reported_at` = the complaint time (or now), and returns the traced view.

## 2. Network visibility

Per A-06, Chase reads the full simulated payment graph (all banks) up to `now − visibility_lag`.
`visibility_lag` defaults to 0 in demo and 5 minutes in evaluation. This models DPIP/CFCFRMS data
sharing. Its absence is a config change, not a code change.

## 3. Taint tracing — proportional (haircut) rule

Inputs: the source payments (victim → first-hop accounts) and the ledger since the earliest
source payment time t0.

```
state per account: balance B, taint T (0 ≤ T ≤ B)
initialise: each source payment's payee gets T += amount (and B as per the ledger)
process ledger events in time order (ties: ledger entry id):
  credit x carrying taint τ:   B += x; T += τ
  debit  x (outgoing payment): τ_out = x · T / B   (computed with B before the debit; 0 if B = 0)
                               B −= x; T −= τ_out
                               the payment carries τ_out to its payee (if traced)
  CASH_OUT channel debit:      T −= τ_out; the taint is LOST (recorded as cash-out leakage)
prune: stop following an account once T < min_trace_paise (₹500)
```

Outputs: per-account current taint `T_now`; per-edge taint flows (for the graph); total cash-out
leakage; taint in flight (debited, credit not yet visible); hop depth per account (first-hop =
1). Integer arithmetic: compute in float64 and round each τ_out to paise with banker's rounding.
The conservation check (Σ T_now + leakage + in flight + pruned = initial taint, ± hops × 1 paise)
is a property test.

**Implementation** (`delphi.chase.tracer`, a pure function; `delphi.chase.ledger` loads its inputs
through the `bank` tables `delphi.sim.api` re-exports, up to `now − visibility_lag`, never
`sim_truth`). Gaps the rule above leaves open, filled as follows:

- **Reading the ledger outwards from the frontier**, not scanning it. Each source's payee is
  reached when the source executes. Each round reads the reached accounts' entries from the
  time they were reached (index `(account_id, at)`), then the credits of the payments they made
  (index `payment_id`). Each payee becomes reached at its credit's time, and the next round
  reads it. The rounds stop when nothing new is reached. A cash-out payment's payee is not
  followed. This gives exactly the result of scanning every entry since t0 (an integration test
  compares both). On `demo`, tracing all 2,140 executed frauds at once reads 3,466 entries and
  takes about 0.2 s (NFR-04: 5 s).

- **Balances come from the ledger:** B before a debit is `balance_after_paise + x`, so an account
  first reached mid-way is traced with its real balance; nothing is replayed from t0.
- **A payment's debit is applied before its credit**, whatever their entry ids (a payment's
  entries are grouped at its first entry's time), so taint never arrives before it has left.
- **Taint moves in whole paise:** τ_out is rounded once, on the debit, and the payee is credited
  exactly that amount, so conservation holds **exactly** (gap 0, not ± hops paise); the property
  test asserts 0. τ_out is capped at T.
- **Pruning** happens whenever an account's T changes and lands in (0, min_trace_paise): what it
  held is added to `pruned` and the account is dropped. Its later debits carry no taint.
- **Cash-out:** the cash-out payee is not credited with taint, and the flow is recorded with no
  payee.
- **In flight:** with the visibility lag, a payment's debit can be visible before its credit.
  Its taint is counted as `in_flight` (still recoverable), not as leakage, and the flow is
  marked `in_flight`. The next trace, with the credit visible, carries it on to the payee.
- **Sources:** only EXECUTED source payments carry money. A source whose credit falls outside the
  visible ledger still taints its payee with the full amount (depth 1).

## 4. Candidate scoring

For every account with T_now ≥ ₹500 (the **freeze candidates**):

```
Δ        = freeze_latency (internal: 60 s; external: 30 min) × time_scale
τ_acc    = mean dwell time of the account:
             from the account's last-30-day credits: median FIFO time-to-80%-out ÷ ln 2
             fallback when < 3 credits: 20 min if p_mule ≥ 0.5 else 24 h          (all × time_scale)
p_present = exp(−Δ / τ_acc)
p_mule    = mule scorer (doc 13 §6) for the account
κ         = 0.30   # chance a lien on a non-mule holder's tainted balance is upheld (config)
lien_amount = min(T_now, current balance)
ERV       = lien_amount · p_present · (p_mule + (1 − p_mule) · κ)
wrongful  = (1 − p_mule) · (5,000 ₹ + 0.02 · lien_amount)
net       = ERV − wrongful
```

Ranking is by `net` descending. Candidates with net > 0 are **recommended**. The parameters κ,
the latencies, ₹5,000 and 0.02 are in `kb/engine.yaml` under `chase:`, so they are versioned
with the KB.

## 5. Freezing

1. The investigator selects candidates (default selection: all recommended, capped at
   `max_liens_per_round` = 10) and approves them. This is a human-in-the-loop step (FR-CHS-04).
   VIEWERs cannot approve.
2. For each approved candidate, a lien with `amount = lien_amount` **recomputed at approval
   time** and `recorded_reasons` generated by template:
   "Traced ₹{taint} of reported fraud {complaint_id} reached this account via {hops} hop(s)
   ({path}). The mule score is {p_mule:.2f}. A lien is placed on the traced amount only,
   per DELPHI chase operation {id}." `evidence_refs` = the payment ids along the path.
3. Internal account (bank `DLPH`): status `PLACED` immediately, if the balance still covers it;
   otherwise lien what remains, and if nothing remains, `FAILED_FUNDS_MOVED`.
4. External account: create a `cfcfrms_ticket` (`DRAFTED` → `SENT_SIMULATED`) with a
   template payload (complaint id, account, bank, amount, reasons). The worker acknowledges it
   after the external latency. At acknowledgement the lien is placed for
   min(lien_amount, the balance at that moment), or fails if the balance is 0.
5. Re-tracing: while the operation is open, the `CHASE_RETRACE` scheduled job (doc 04 §5.4)
   re-traces it every `retrace_interval_min` (time-scaled, floor 20 s real time), because the
   mule network keeps moving money. New candidates appear and recommendations update. Existing
   liens are not changed automatically.
6. Ticket expiry: a ticket not acknowledged within `ticket_expiry_min` (time-scaled) becomes
   `EXPIRED`, and its lien becomes `FAILED_NO_RESPONSE`.
7. Auto-close: the operation becomes `CLOSED` when the golden hour has elapsed and no ticket
   is still `SENT_SIMULATED`. Manual close is allowed at any time.
8. A complaint never starts a chase automatically in the live app (an investigator decides).
   The evaluation harness starts one for every executed fraud with a complaint (doc 22 §4), and
   story S-3's runner starts its chase explicitly.

## 6. Metrics (per operation and aggregated)

- `golden_hour_recovery_ratio` = Σ lien amounts PLACED within `golden_hour_minutes` (× time
  scale) of `reported_at` ÷ total reported loss.
- `tracing_delay` = time of the first PLACED lien − `reported_at` (shown in sim minutes).
- `liens_on_non_mule_accounts` (from sim truth, evaluation only) = wrongful-freeze count.
- `amount_frozen_beyond_taint` = must always be 0 (an invariant check).

## 7. UI (Chase screen)

- A graph of accounts (nodes sized by T_now, coloured by p_mule, border = internal vs external)
  and taint flows (edges labelled with ₹), laid out left to right by hop depth.
- A golden-hour timer (sim minutes remaining) and a gauge "₹ recovered of ₹ lost".
- A candidates table: account (masked), bank, hop, T_now, p_present, p_mule, ERV, wrongful
  cost, net, status. Plus an "Approve selected" button.
- Ticket list with statuses. Demo mode adds a live feed of onward transfers made by the mule
  network.

## 8. Inbound side (receiving bank)

Inbound mule detection is handled by `ACCOUNT_REVIEW` cases (doc 09 §1). Chase only acts on
already-moved funds.
