# 14 — Simulation: synthetic bank, scenarios, responses, labels

Research §12 ("Data"). Module `delphi.sim`. The simulator is a first-class deliverable. Research
§17.5 notes that an APP scenario generator with a probe-response model is itself a contribution.

## 1. Principles

1. **Deterministic.** Everything is driven by `numpy.random.Generator(PCG64(seed))`, with
   independent sub-streams per component (`SeedSequence(seed).spawn`). Streams whose draws must
   not shift when other streams change are keyed by name instead (`delphi.sim.rng.keyed_rng`):
   the response-model freeze (§5.2) and the label draws per payment (§7). The same profile and
   seed produce byte-identical datasets. A test checks the SHA-256 of a canonical export of the
   `test` profile.
2. **Ground truth is quarantined** in the `sim_truth` schema (doc 17 §2). Product code never
   reads it.
3. **The simulator's truth is not the KB.** The response models (§5) are deliberately different
   from KB v1's likelihood tables, so the learning loop has something real to learn and the
   evaluation is not circular.
4. **Every number below lives in `backend/src/delphi/sim/config/*.yaml`**, versioned as
   `generator_version`.

## 2. Dataset profiles

| Profile | Customers | History days | `no_complaint_maturity_days` | Live/eval window | Seed | Use |
|---|---|---|---|---|---|---|
| `test` | 200 | 21 | 7 | 3 days | 1 | CI integration and E2E tests (< 30 s to generate) |
| `demo` | 2,000 | 150 | 60 | open-ended live window | 20261031 | Deployed demo (personas included) |
| `eval` | 20,000 | 150 | 60 | 30 days | 7 | Evaluation harness (doc 22) |

**Deterministic ids.** Every entity the generator creates (customers, accounts, payments, …)
gets a UUID derived from the profile's RNG stream (`uuid.UUID(bytes=rng.bytes(16), version=4)`),
so ids are identical across runs. Each entity kind has its own stream,
`keyed_rng(profile seed, "id", kind)` (`delphi.sim.ids.IdStream`), so adding a kind of entity
never shifts the ids of the others. Only entities created live through the API (phone payments,
cases, sessions) get random UUIDv7s.

The history period ends at `history_end` = the dataset's creation time (demo) or a fixed virtual
epoch `2026-07-01T00:00:00+05:30` (eval/test).

## 3. Population

| Attribute | Distribution |
|---|---|
| Age | 18–85; P(≥ 70) = 0.12; P(60–69) = 0.15; the rest uniform over 18–59 |
| Language | hi 0.40, en 0.25, te 0.12, ta 0.12, other → en 0.11 |
| Disability flag | 0.02 |
| Income band | LOW 0.45, MID 0.40, HIGH 0.15, scaling amounts ×0.5 / ×1 / ×3 |
| Devices | 1 (0.8) or 2 (0.2); a genuine new-device event happens once per 400 days on average |
| Payees | 3–15 established payees (family, utilities, merchants), Zipf-weighted |
| Consent to call status | 0.70 |
| Home banks | 70% of counterparties are at `DLPH`, the rest spread over `XBK1`–`XBK5` |

Merchant accounts: 300 (`demo`) / 3,000 (`eval`), including 20 hospitals (`demo`), all marked as
registered merchants.

`delphi.sim.population` generates this from `sim/config/generator.yaml`. Each customer draws
from its own stream, `keyed_rng(seed, "customer", i)`, and the merchants from
`keyed_rng(seed, "merchants")`. Where the table above is silent, the generator uses these values
(owner review pending):

- **Within the age bands**, ages are uniform (60–69 and 70–85 as well as 18–59).
- **Every customer** is a `RETAIL` customer with one `ACTIVE` account at `DLPH`,
  opened 1–10 years (uniform) before the history starts. The opening balance is log-normal,
  median ₹40,000 × the income band's amount scale, σ = 1.0.
- **KYC.** Genuine non-merchant accounts (customers' and external counterparties') have
  `MINIMAL` KYC with probability 0.05, from `keyed_rng(seed, "kyc", account_id)`, else `FULL`;
  merchants are `FULL`. Minimal KYC is then a strong mule signal but not a tell (owner
  decision on #49).
- **Home city** follows language: te → Hyderabad; ta → Chennai; hi → Delhi 0.4, Lucknow 0.3,
  Jaipur 0.3; en → Bengaluru 0.4, Mumbai 0.4, Kolkata 0.2.
- **Devices** are ANDROID 0.85 / IOS 0.15. Every device is first seen at least 30 days before the
  history starts, so none looks new; new-device events come with the payments.
- **Payees:** 3–15 (uniform count) of type family 0.4, utility 0.2, merchant 0.4, all saved
  before the history starts. A beneficiary drawn twice is drawn again, so every customer has
  exactly its drawn count. A family payee is another customer's `DLPH` account with
  probability 0.7, else a new personal account at an external bank. Utility and merchant payees
  are merchant accounts; hospitals are never established payees (paying one is
  `GENUINE_UNUSUAL`). Zipf weights apply when payments pick a payee.
- **Merchants** are 0.15 per customer on every profile (300 on `demo`, 3,000 on `eval`, 30 on
  `test`), 1 in 15 a hospital. The rest are GROCERY 0.25, UTILITY 0.20, RETAIL 0.20, FUEL 0.10,
  TELECOM 0.10, EDUCATION 0.07, WEDDING_VENDOR 0.04, PROPERTY 0.04. Like every counterparty,
  70% bank at `DLPH`; merchants have no customer row.
- **Names, phones and VPAs** are synthetic and unique: `Customer 00042`, `+919000000042`,
  `customer000042@dlph`. No real-looking personal data.

The `test` profile's canonical export (`delphi.sim.export`: rows sorted by id, canonical JSON,
no database-assigned columns, floats to 6 decimals) has its SHA-256 recorded per
`generator_version` in `sim/config/dataset_hashes.yaml`. Generated floats are rounded to 6
decimals at the source as well, because `exp` and `log` can differ in the last bit between
C libraries. A unit test regenerates it on every push.

## 4. Scenarios

Each scenario generates payments, ledger movements, session telemetry, logins, CRM events and
complaints. Rates are per 10,000 payments in the **live/eval window**. Fraud is oversampled
relative to reality so the metrics have power (stated in the report).

| Scenario (`scenario_type`) | Hypothesis | Rate / 10k | Key generative features |
|---|---|---|---|
| `GENUINE_ROUTINE` | GENUINE | 9,700 | Poisson payment counts (λ ~ Gamma(2, 0.3)/day); log-normal amounts per payee type; habitual hours |
| `GENUINE_UNUSUAL` | GENUINE | 150 | Large one-off to a new merchant (hospital, wedding vendor, property token), or a new phone. 30% happen at night |
| `ATO` | ATO | 40 | SIM swap or phishing → login from a new device and new city → add payee → 1–3 transfers draining 60–95% of the balance to layer-1 mules within 30 min |
| `APP_DIGITAL_ARREST` | APP_DIGITAL_ARREST | 30 | Victim age skewed older (P(≥ 60) = 0.6); on a call; 1–4 large transfers over 1–3 days to mule accounts |
| `APP_INVESTMENT` | APP_INVESTMENT | 40 | 3–8 escalating payments over 2–6 weeks to "platform" accounts, then "withdrawal fee" payments |
| `MULE_FORWARDING` | MULE (outbound) | 30 | A home-bank mule account forwards credits onward within minutes |
| `UPI_COLLECT_REFUND` (hidden) | `APP_COLLECT_REFUND`, the key the S-4 script promotes (doc 11 §7); not in KB v1 | 10 (live/eval window only) | Channel `UPI_COLLECT_APPROVAL`; victim believes they are receiving a refund; amounts ₹2k–₹50k; on a call with an unknown number in 60% of cases |

**Mule networks:** rings of 5–30 accounts across banks, with 1–4 layers. Dwell time per layer is
log-normal (median 12 min at layer 1, 25 min at deeper layers, σ = 0.8). Each onward transfer
splits into 1–3 payments. 40% of final-layer flows end in `CASH_OUT`. Ring accounts share
devices: P(a device shared by ≥ 3 accounts) = 0.5. 40% of mule accounts are listed in the sim
suspect registry after a lag (§7). False listings: 0.1% of genuine accounts.

**History period:** the same scenarios at the same rates, decided by a simulated **legacy
policy** (= baseline B1, doc 22 §2). This produces selective labels in the training data.

`delphi.sim.history` generates the history's genuine payments; numbers are in
`sim/config/generator.yaml`. Where the table above is silent, it uses these values (owner
review pending):

- **Planning.** Each customer plans from its own stream, `keyed_rng(seed, "routine", i)`: a daily
  rate λ ~ Gamma(shape 2, scale 0.3), so 0.6 a day on average, and Poisson counts per local
  (IST) day.
- **Payee choice:** Zipf weights 1 / rank over the customer's payees.
- **Amounts** are log-normal per payee type × the income band's scale: family median ₹2,000
  (σ 0.9), utility ₹800 (σ 0.6), merchant ₹600 (σ 1.0).
- **Timing and channel.** Each customer has a habitual hour, uniform over 08–21 IST; each
  payment falls at that hour + N(0, 1.5 h). Channels by payee type: family UPI_PUSH 0.80 /
  IMPS 0.15 / NEFT 0.05; utility UPI_PUSH 0.60 / NEFT 0.40; merchant UPI_PUSH.
- **Salary.** Every customer gets a monthly salary on the 1st, 10:00–12:00 IST, by NEFT, from a
  payroll account at one of the external banks. The amount is log-normal, median ₹30,000 ×
  the income scale, σ 0.4. Salaries are not customer sessions: no telemetry, no B1, no labels.
- **Session telemetry** comes from the scenario's §5.1 signals, placed on the intended side of
  the KB's classifier thresholds:
  - half the paste/long-dwell sessions paste; the rest dwell 20 s plus a log-normal extra
    (median 15 s)
  - normal dwell is uniform over 1–12 s
  - cadence-mismatch z is 3.0 plus an exponential excess (mean 1); otherwise N(0, 1), kept below
    3.0
  - 30% of remote-access sessions are screen shares
  - session duration is log-normal, median 45 s, σ 0.5
- **Decisions.** Every customer payment goes through B1:
  - **RELEASE:** executed at once (docs/05 §3), with a double-entry ledger. A payment the payer
    cannot fund is not made.
  - **HOLD:** stays `HELD`; nothing investigates it, so it never gets a label.
  - **DECLINE:** ends `DECLINED`.

  Labels are scheduled by `schedule_payment_labels` (§7).
- **GENUINE_UNUSUAL** runs at each customer's routine rate × 150 / 9,700 (the table's rates).
  Each event is one of two kinds:
  - **New merchant** (0.6): a hospital, wedding vendor or property merchant, saved 2–30 minutes
    before paying it.
  - **New phone** (0.4): a phone first seen 2–30 minutes before, paying an established family
    payee.

  The amount is log-normal, median ₹80,000 × the income scale, σ 0.7, by IMPS. 30% fall at
  night (22:00–05:00 IST), the rest at the habitual hour. A customer saves each merchant once:
  a new merchant is one not saved before or during the history (if none is left, the event is
  a new phone). An event that falls outside the history saves no payee and adds no phone. One
  the customer cannot fund keeps its payee or phone: the payment failed, the set-up did not.
- **New phones** (§3, once per 400 days) arrive as a Poisson process; later payments use the
  newest device.
- **Logins.** Every customer session starts with a login 30–300 s earlier (never before the
  history starts), from the session's device (a stable synthetic `10.x.y.0/24` per device) in
  the home city. Saved times and new phones are likewise never before the history starts.
- **B1 inputs.** B1 sees a payee as new by doc 13 §3's `is_new_payee`: saved < 24 h ago, or
  never paid and saved within the 30-day baseline window (`legacy_policy.new_payee_hours` and
  `payee_baseline_days`, the one place the simulator reads them from). It sees a device as new if
  first seen within the KB's `new_device_days`. So it holds most unusual payments, and those
  never get a label. It declines a payment to a payee the sim registry lists by then.

`delphi.sim.fraud` adds the fraud victims to the history, also from `generator.yaml`:

- **Rates.** Each scenario gets `rate × made / 9,850` payments, in whole instances, where
  `made` counts the genuine sessions a dry run of the history actually makes (some are never
  made, for want of funds). Each scenario draws from `keyed_rng(seed, "fraud", scenario)`, and
  the sessions of an instance from `keyed_rng(seed, "fraud_session", instance_id)`. A victim
  pays layer-1 mules of a uniformly chosen ring, saved as a payee 2–30 minutes before the
  first payment to it and labelled like any family payee ("Family N"), so the label gives
  nothing away.
- **ATO.**
  - Half are SIM swaps, which the CRM records (`SIM_SWAP_REPORTED`) 1–48 h before the login;
    half are phishing.
  - The attacker logs in from a new device in another city and saves the mule 1–5 minutes later.
  - The transfers (IMPS 0.6 / UPI 0.4) fall uniformly in the rest of the 30 minutes. Transfer
    j of k takes `d / k` of the first balance, d uniform over 60–95%, as a share of what is left.
- **APP_DIGITAL_ARREST.** The victim is aged ≥ 60 with probability 0.6. Each transfer sends
  30–80% of the balance at the time, to a layer-1 mule of the ring (IMPS 0.6 / NEFT 0.3 /
  UPI 0.1).
- **APP_INVESTMENT.**
  - Deposits go to one "platform" mule, the first log-normal (median ₹5,000 × the income scale,
    σ 0.5), each 1.5 × the last.
  - 1–2 withdrawal fees of 10% of the deposits actually made follow, in the last 20% of the
    span (UPI 0.7 / IMPS 0.3).
  - A victim never sends more than 90% of the balance at the time.
- **Coached victims:** 30% of DA and INV instances (the §5.2 share), in
  `payment_truth.coached`.
- **Complaints.** A reported payment (§7) becomes a `bank.complaints` row at its complaint
  time if that falls before the history ends: NCRP_1930 0.7 / BRANCH 0.3, from
  `keyed_rng(seed, "complaint", payment_id)`. The categories are UNAUTHORISED_TRANSACTION (ATO),
  DIGITAL_ARREST_SCAM and INVESTMENT_SCAM. Later complaints exist only in
  `sim_truth.label_schedule`.
- **Background CRM events.** Genuine customers report a SIM swap or a lost phone (0.5 / 0.5)
  once per 500 days, from `keyed_rng(seed, "crm", i)`, so about 3% of genuine payments have
  one in the 14-day CRM lookback.

`delphi.sim.mules` builds the mule networks, also from `generator.yaml`:

- **Rings.** There is one ring per 400 customers, at least 2 (2 on `test`, 5 on `demo`, 50 on
  `eval`), each from its own stream, `keyed_rng(seed, "mule_ring", r)`. A ring's size (5–30)
  and layer count (1–4) are uniform. Its accounts are split over the layers as evenly as
  possible, with the extra accounts in layer 1. `sim_truth.account_truth` has a row for every
  account; only mules have `is_mule`, a `ring_id` and a `layer`.
- **Banks.** A mule banks at `DLPH` with probability 0.21, else at an external bank (evenly). A
  ring always spans at least two banks. The 0.21 is calibrated: home-bank mules' forwarding
  sessions then come out at MULE_FORWARDING's 30 per 10,000 payments (measured over 50 rings).
- **Home-bank mules** are customers, numbered like the rest, so names, phones and VPAs give
  nothing away:
  - aged 19–35 (uniform), `LOW` income, no disability
  - language, city and consent drawn as in §3
  - one device each
- **External mules** are accounts only, like the other external counterparties.
- **Every mule account:**
  - has `MINIMAL` KYC with probability 0.5, else `FULL`
  - was opened 5–400 days before the history (docs/13 §6 uses both as features)
  - has a log-normal opening balance in paise, median ₹2,000, σ 0.8
- **Shared devices.** With probability 0.5 a ring shares one device fingerprint across 3 to
  all of its home-bank mules. Such a ring gets at least 3 home-bank mules, turning randomly
  chosen accounts to `DLPH` if needed.
- **False listings.** Each genuine non-merchant account is listed with probability 0.001, from
  its own stream `keyed_rng(seed, "false_listing", account_id)`, at a uniform instant in the
  history. It gets a MULE `REGISTRY_LISTING` label at that instant. `bank.sim_registry_listings`
  holds only listings made before the history ends; later ones are known only to
  `sim_truth.label_schedule` (§8).

`delphi.sim.forwarding` moves the money down the rings during the history's pass:

- **Flows.** A victim's executed payment to a mule starts a flow there. After the §4 dwell,
  the mule forwards it as one onward transfer: 1–3 payments (uniform count, uniform whole-rupee
  shares), 1–5 minutes apart, IMPS 0.5 / UPI 0.5, all to one uniformly chosen account of the
  next layer.
- **Next layer.** Once every payment of a transfer is resolved, what was executed starts a flow
  at that account. A held or declined payment stops its share there.
- **Final layer.** A final-layer account cashes the credit out with probability 0.4: one
  `CASH_OUT` payment to its bank's cash-out account (one per bank, no customer). Otherwise the
  money stays.
- **Streams.** Each flow draws from `keyed_rng(seed, "flow", n)`, numbered in the order the
  pass starts them. A home-bank mule's payments are app sessions from its device, with the
  MULE_FORWARDING §5.1 telemetry, decided by B1. External mules' payments and cash-outs are not
  sessions. All of them belong to one MULE_FORWARDING scenario instance per ring.
- **Payees.** A home-bank mule saves its next-layer account as a payee ("Family N") 2–30
  minutes before it first pays it (not before the history), from
  `keyed_rng(seed, "mule_payee", payer/payee)`, so B1 sees the payee as new only while doc 13
  §3 says it is. Dwell and split times are whole seconds.
- **Registry.** 40% of *credited* mule accounts are listed: when a mule first receives fraud
  money it is listed with probability 0.4, from
  `keyed_rng(seed, "mule_listing", account_id)`, at its first fraud credit plus the §7 registry
  delay. B1 declines payments to it from then on.
- **After the history.** A payment of a flow that falls due after the history ends is not
  made here. It becomes a `bank.sim_schedule` TRANSFER row (payload: payer, payee, amount,
  channel, `reason: MULE_FORWARDING`, the flow and whether it is a session), which `SIM_TICK`
  (§8) or the evaluation replay makes when it falls due.

**The live/eval window** (`window_days`: 3 on `test`, 30 on `eval`; `demo` has an open-ended
live window and no generated one) is generated with the history, as one stream every policy
replays (doc 22 §1). This design is proposed in the last #17 PR, for the owner's review.

- **Planning.** Customers plan straight through the window, with the same rates, habits and
  payees.
- **Fraud scenarios.** The window has its own fraud scenarios at their rates against the
  window's genuine sessions, from `keyed_rng(seed, "window", "fraud", scenario)` and their own
  id streams. **UPI_COLLECT_REFUND** runs here only (10 per 10,000):
  - one `UPI_COLLECT_APPROVAL` payment per victim, ₹2k–₹50k log-uniform
  - requested by a layer-1 mule
  - an unknown-number call in 60% of cases (its §5.1 target)
- **Undecided.** Every window session is a `PENDING_GUARDIAN` payment with origin `SIM_LIVE`,
  with telemetry, a login and ground truth. No policy has decided it, so there is no ledger
  entry and no label. An amount that depends on a balance uses the balance at the end of the
  history. Investment fees use the window's planned deposits.
- **Not in the stream.** The window holds no MULE_FORWARDING sessions: they follow from what
  each policy releases, so the replay generates them with `delphi.sim.forwarding`. The
  window's salaries are TRANSFER rows (`reason: SALARY`).
- **Stats.** `delphi.sim.stats` (`delphi sim stats --profile`) reports sessions per 10,000 per
  scenario for the history and the window, against these rates. The #17 acceptance check (±10%
  on `eval`, a `slow` test) uses:
  - the history for every scenario except UPI_COLLECT_REFUND
  - the window for every scenario except MULE_FORWARDING
  - the history's share of credited mules listed (0.4)

## 5. Response models (the simulator's truth)

### 5.1 Telemetry-driven passive probes

Telemetry is generated per scenario. The deterministic classifiers (doc 08 §5) then produce the
outcomes. Generation targets (approximate outcome rates):

| Signal | GENUINE_ROUTINE | GENUINE_UNUSUAL | ATO | DA | INV | MULE_FWD | COLLECT_REFUND |
|---|---|---|---|---|---|---|---|
| On a call with an unknown number | 0.05 | 0.10 | 0.08 | 0.80 | 0.40 | 0.12 | 0.60 |
| Remote access active | 0.005 | 0.01 | 0.20 | 0.25 | 0.10 | 0.04 | 0.05 |
| Paste / long dwell | 0.06 | 0.20 | 0.12 | 0.50 | 0.35 | 0.50 | 0.30 |
| Cadence mismatch | 0.02 | 0.05 | 0.65 | 0.04 | 0.04 | 0.10 | 0.03 |

### 5.2 Active-probe answers

For each scenario s and active probe a, the true outcome distribution is

```
P_true(· | s, a) ~ Dirichlet(50 · P_kb(· | h(s), a))     sampled ONCE with seed 99 and frozen into
                                                         sim/config/response_models.generated.yaml
```

with these overrides:

- **Well-coached victims**: 30% of DA and INV instances answer PURPOSE_QUESTION and
  SPELL_BREAKER_* from 0.5·P_true(GENUINE) + 0.5·P_true(own).
- **GENUINE_UNUSUAL**: SESSION_BEHAVIOUR hesitant 0.20, PURPOSE timing-anomalous 0.12.
- **UPI_COLLECT_REFUND** (no KB row exists, so it is explicit): PURPOSE_QUESTION =
  (0.15, 0.03, 0.12, 0.70); SPELL_BREAKER_* = (0.10, 0.02, 0.88); OTP/BIOMETRIC pass 0.97;
  COOLING_OFF = (0.15, 0.25, 0.60); COLLECT_INTENT_CHECK = (0.88, 0.12);
  CUSTOMER_OUTREACH_CALL = (0.50, 0.25, 0.05, 0.20), the same as APP_INVESTMENT (the victim
  realises once a verified bank caller explains).

How the freeze is done (`delphi sim freeze-response-models`; the inputs are in
`sim/config/response_models.yaml`):

- **Streams.** Each row (scenario s, source a) is drawn from its own PCG64 stream,
  `SeedSequence(99, spawn_key=sha256("s/a")[:16] as four 32-bit words)`, so adding a scenario or
  a source later never changes a row already frozen. The rows cover every ACTIVE probe plus
  `CUSTOMER_OUTREACH_CALL` (§9). The KB v1 content hash is recorded in the file.
- **P_true(GENUINE)** in the coached mix is the `GENUINE_ROUTINE` row. The mix is stored as
  separate `coached` rows; which instances are coached (30%) is decided per payment by the
  scenario generator and recorded in `sim_truth.payment_truth.coached`.
- **Pinned outcomes.** GENUINE_UNUSUAL's PURPOSE_QUESTION row is drawn like the others, then
  TIMING_ANOMALOUS is set to 0.12 and the other outcomes keep their drawn proportions of the
  remaining 0.88. "SESSION_BEHAVIOUR hesitant 0.20" is its §5.1 paste/long-dwell target.
- **UPI_COLLECT_REFUND** takes the vectors above exactly, without perturbation. A scenario with
  explicit answers must list every answered source, and a scenario without them must have a KB
  v1 hypothesis: a missing row fails the freeze, never falls back.
- The snapshot test fails if the committed file differs from a fresh freeze. A deliberate change
  bumps `generator_version` and is recorded in `sim/config/CHANGELOG.md` (§5.4).

### 5.3 Coaching adaptation (optional switch)

`coaching_adaptation: [{scenario, probe, start_day, ramp_days, shift}]` gradually moves a
scenario's answers toward the GENUINE row (e.g. the DA spell-breaker loses half its signal over
14 days: `start_day` d, `shift: 0.5`; `ramp_days` defaults to 14). The answers become
`(1 − w)·P(own) + w·P_true(GENUINE)` with `w = shift · clip((day − start_day) / ramp_days, 0, 1)`,
where `day` counts sim days from the start of the profile's history. It is off by default and is
used to demo the yield-drift detector (doc 12 §5).

### 5.4 Calibration requirement for demo story S-4

On the `demo` profile under KB v1, at least 70% of `UPI_COLLECT_REFUND` sessions MUST end with
P(UNEXPLAINED) ≥ 0.35. If they do not, tune **only** the collect-refund telemetry and answer
generation (not the KB) until they do, and record the change in the generator changelog.

## 6. Personas (demo profile)

| Persona | Facts | Scripted behaviour |
|---|---|---|
| Kamala Devi (S-1) | 68, te, 6 years as a customer, 1 device, consent to call status on | Story payment ₹4,80,000 at 23:40 to a new payee: a freshly opened layer-1 mule account at XBK2, not yet listed, scripted so p_mule < 0.5, with a phone number whose FRI category is LOW. Kamala is on a call with an unknown number, typing normally, on her known device. The five passive outcomes are therefore CLEAN_UNKNOWN, LOW_OR_UNKNOWN, KNOWN_DEVICE, NORMAL and ON_CALL_UNKNOWN_NUMBER. The presenter answers live, or autoplay uses the script: coached purpose answer FAMILY_OR_FRIEND after 6 s; spell-breaker NO after 11 s with 1 app switch; answers any other probe from the DA response model |
| Arjun Mehta (S-2) | 34, en, new phone today (genuine) | ₹1,80,000 to "Sunrise Multispeciality Hospital" (a registered merchant); biometric MATCH; purpose MEDICAL after 5 s |
| Ravi Kumar (S-3 victim) | 45, hi | ₹3,00,000 released earlier, at T−10 min, to ring "Ring-7" (12 accounts, 3 layers); complaint at T0 |
| Ring-7 | 12 mule accounts across DLPH, XBK1, XBK3 | Onward transfers scheduled per §4 dwell times; continues during the demo |
| Collect-refund victims (S-4) | 15 customers | Generated by the hidden scenario in the live window |

## 7. Label arrival model (what the bank learns, and when)

| Situation | Label | Source | Delay |
|---|---|---|---|
| Released fraud payment, victim reports | true hypothesis if it is in the ACTIVE KB, else `FRAUD_OTHER` (collect-refund: `APP_COLLECT_REFUND` once promoted) | `CUSTOMER_COMPLAINT` | report probability: APP 0.70, ATO 0.90, COLLECT_REFUND 0.60; delay log-normal (median 2 days; ATO 4 h) |
| Released genuine payment | GENUINE | `NO_COMPLAINT_60D` | `no_complaint_maturity_days` (60; 7 on `test`) |
| Released fraud, never reported | none until maturity, then GENUINE (a **wrong** label, by design: real-world label noise) | `NO_COMPLAINT_60D` | `no_complaint_maturity_days` |
| Held/declined, DELPHI outreach confirms | leading fraud hypothesis | `OUTREACH_CONFIRMED` | at the outcome |
| Held, investigator disposition | disposition | `INVESTIGATOR_DISPOSITION` | at disposition |
| Held/declined in the legacy policy with no investigation | none | — | never (selective labels) |
| Mule account listed in the registry | MULE | `REGISTRY_LISTING` | log-normal, median 5 days after first fraud credit |

How the model applies the table (`delphi.sim.labels`, numbers in `sim/config/labels.yaml`):

- **Delays** are log-normal, `median · exp(σ·z)`, with `label_delay_sigma` σ = 0.8 (its own
  key, equal to the mule-dwell spread in §4).
- **Who reports.** `labels.yaml` lists a report probability for every fraud scenario; loading
  fails if one is missing. `MULE_FORWARDING` is listed at 0.0: its payments leave a mule's own
  account, so they mature as GENUINE (noise) and the account is labelled only through the
  registry.
- **Random draws.** Each payment draws its report and delay numbers from its own stream,
  `keyed_rng(profile seed, "label", payment_id)`, and draws both whatever happens to it, so a
  payment's labels never depend on other payments or on the policy (doc 22 §1). A
  registry-listed account uses `keyed_rng(profile seed, "registry", account_id)`.
- **Resolutions.** DELPHI outreach resolves a held or declined payment; an investigator
  disposition resolves a held one only, as in the table. Any other resolution is an error.
- **Late complaints.** A complaint that falls after `no_complaint_maturity_days` arrives after
  the GENUINE label; label precedence (doc 05 §6) then lets the complaint win.
- **ACTIVE KB.** The schedule stores the ground-truth key; `LABEL_DELIVERY` maps a complaint to
  `FRAUD_OTHER` if that key is not in the ACTIVE KB at delivery time.

## 8. Live simulation in the deployed demo

The worker runs a `SIM_TICK` job every 10 s (wall clock). It:

1. Advances scheduled mule-network transfers whose due time has passed (time-scaled): the
   `bank.sim_schedule` TRANSFER rows the generator leaves for flows still in flight when the
   history ends (§4).
2. Emits background live payments at `demo_background_rate` (default 1 every 20 s), through
   Guardian with `SimCustomerChannel` (runner `WORKER`), so the monitor always shows traffic and
   shadow mode accumulates sessions.
3. Acknowledges CFCFRMS tickets whose latency has elapsed, and expires overdue ones.

Label arrivals are delivered by the separate `LABEL_DELIVERY` job from
`sim_truth.label_schedule` (doc 04 §5.4). `bank.sim_schedule` never holds labels, so the app
role cannot see future labels.

**Story S-4 data.** The hidden scenario's natural rate (10 per 10k) is far too slow for a live
demo. The `DEMO_STORY` S4 runner therefore **injects** 15 `UPI_COLLECT_REFUND` payments for
15 pre-generated collect-refund victim customers (created at seed time, with no history of the
scam). They go through Guardian with runner `WORKER`, one every 2 s. After the typology enters
shadow, the runner injects 12 more mixed background payments (including 3 collect-refund), so
the shadow minimum (10) is reached. Then, after promotion, it injects one fresh collect-refund
payment.

**Time scale**: in `DEMO` mode every *simulated duration* (dwell, latencies, cooling-off,
golden hour, SLA timers) is multiplied by `demo_time_scale` = 1/30. The UI shows durations in
"sim minutes" = real seconds × 30 / 60.

## 9. Simulated external systems

| System | Behaviour |
|---|---|
| Suspect registry (I4C) | Table `sim_registry_listings(account_id, listed_at)`, populated by §7 |
| FRI (DoT) | Phone risk per number: numbers used by fraud rings → VERY_HIGH 0.35 / HIGH-MEDIUM 0.35 / LOW 0.30; genuine numbers → VERY_HIGH 0.005 / MEDIUM-HIGH 0.03 |
| Telco call status | From session telemetry |
| CFCFRMS | Ticket ack latency log-normal, median 20 min (× time scale), 10% never acknowledged within 2 h |
| External banks | Hold ledgers in the same database. Lien placement is honoured if the funds are present at acknowledgement |
| Customer outreach call | Samples `CUSTOMER_OUTREACH_CALL` from the response model (a perturbation of the KB row, as in §5.2) |

## 10. Demo control

- `POST /admin/demo/scenarios/{story}/run {mode: "interactive"|"autoplay"}` for stories S-1 to
  S-4.
- `POST /admin/demo/reset`: deletes every row created after the `seed_completed_at` marker
  (in dependency order, in one transaction), re-activates KB v1, and resets the persona state.
  Must finish in ≤ 60 s.
