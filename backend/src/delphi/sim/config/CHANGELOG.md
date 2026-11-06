# Generator changelog

Every change to `backend/src/delphi/sim/config/*.yaml` or to the generation code that changes
generated data bumps `generator_version` and is recorded here (docs/14 §1 rule 4, §5.4).

The frozen answers also depend on numpy's Dirichlet and PCG64 implementations. If a numpy
upgrade changes them, `test_committed_freeze_matches_kb_v1_and_config` fails even though no
input changed. Then either keep the previous numpy, or regenerate with
`uv run delphi sim freeze-response-models`, bump `generator_version` and record the upgrade here.

## 10

- The simulated external systems (docs/14 §9, #19): every phone number gets an FRI category in
  `bank.sim_phone_risk` (ring numbers VERY_HIGH 0.35 / HIGH-MEDIUM 0.35 / LOW 0.30; genuine
  VERY_HIGH 0.005 / MEDIUM-HIGH 0.03), plus the CFCFRMS acknowledgement sampler (median
  20 min, about 10% never within 2 h) and the customer-outreach-call sampler.

## 9

- The live/eval window (`test` 3 days, `eval` 30): customers plan through it with the same
  habits; its fraud scenarios, plus the window-only UPI_COLLECT_REFUND (10 per 10,000), run at
  their rates against its genuine sessions. Every window session is an undecided
  `PENDING_GUARDIAN` / `SIM_LIVE` payment with telemetry, a login and truth; no ledger, no
  labels (docs/22 §1).
- Mule-ring payments still in flight when the history ends, and the window's salaries, are
  `bank.sim_schedule` TRANSFER rows (docs/14 §8) instead of being dropped.
- The canonical export covers `sim_schedule` and the window's rows.
- The mule home-bank share is re-calibrated on `eval` to 0.31 (MULE_FORWARDING 30.9 per
  10,000); the earlier 0.21 was measured with ten times fewer victims per ring.

## 8

- Mule forwarding (docs/14 §4): after a log-normal dwell (12 min at layer 1, 25 min deeper,
  σ 0.8) each credit moves to one next-layer account in 1–3 payments; 40% of final-layer
  credits are cashed out to a per-bank cash-out account. Home-bank mules forward in app
  sessions (MULE_FORWARDING, decided by B1).
- 40% of credited mule accounts are listed in the sim registry after the §7 delay; B1 then
  declines payments to them.
- The mule home-bank share drops from 0.4 to 0.21, so MULE_FORWARDING sessions come out at 30
  per 10,000 payments.
- Home-bank mules save their next-layer payees before first paying them; dwell and split
  times are whole seconds; the cash-out accounts' age is in `generator.yaml`. Re-measured
  after these changes: MULE_FORWARDING 29.5 per 10,000 over 50 rings at the 0.21 share.

## 7

- The fraud victims' scenarios in the history (docs/14 §4) at their rates per 10,000
  payments made: ATO 40 (new device in a new city, a mule payee, 1–3 transfers draining 60–95%
  within 30 min, half after a SIM swap the CRM records), APP_DIGITAL_ARREST 30 (60% of victims
  aged ≥ 60; 1–4 transfers over 1–3 days) and APP_INVESTMENT 40 (3–8 escalating deposits over
  2–6 weeks, then withdrawal fees), all to layer-1 mules. 30% of DA and INV victims are
  coached.
- Complaints for reported fraud that the bank receives before the history ends; background
  CRM events (SIM swap / lost phone) once per 500 days per customer.
- Mule payees are labelled like any family payee; investment fees are 10% of the deposits
  actually made; CRM events and saved times never fall before the history.

## 6

- Mule rings (docs/14 §4): 5–30 accounts in 1–4 layers across banks, one ring per 400
  customers (at least 2). Home-bank mules are young, low-income customers with a device; half
  the rings share one device across ≥ 3 of them. Half of all mule accounts have minimal KYC.
- `sim_truth.account_truth` for every account.
- False registry listings on 0.1% of genuine non-merchant accounts, with a MULE
  `REGISTRY_LISTING` label when listed; B1 declines payments to a listed payee.
- 5% of genuine non-merchant accounts have MINIMAL KYC (owner decision), so minimal KYC is
  a signal, not a tell; mule opening balances are in paise like everyone else's.

## 5

- GENUINE_UNUSUAL (docs/14 §4): large one-offs to a new hospital, wedding vendor or property
  merchant, or from a new phone, at each customer's routine rate × 150 / 9,700, 30% at night.
- Genuine new phones once per 400 days (docs/14 §3); later payments use the newest device.
- A login before every customer session, from the device's stable /24 in the home city.
- B1 now sees new payees and new devices (the KB's `new_device_days`), so it holds most
  unusual payments; held ones get no label. A payee is new by the amended docs/13 §3 rule:
  saved < 24 h ago, or never paid and saved within the 30-day baseline window.
- A customer saves each merchant once (no duplicate payees); unusual events outside the
  history leave no payee or phone; nothing (logins, saved times, phones) starts before the
  history.

## 4

- Floats are rounded to 6 decimals at the source (session telemetry) and in the canonical
  export, so the `test` hash is the same on every platform: `exp` differed in the last bit
  between MSVC and glibc (FR-SIM-01).
- A customer who draws a beneficiary twice draws again, so every customer has its drawn 3–15
  payees (docs/14 §3).
- B1 compares amounts in paise.
- The known-device age, the external counterparties' income band and the payroll accounts'
  age move into `generator.yaml`; the hospital share is "1 in 15" exactly.

## 3

- The history period's genuine payments (docs/14 §4, #17): GENUINE_ROUTINE payments (λ ~
  Gamma(2, 0.3)/day, Zipf-weighted payees, log-normal amounts per payee type, habitual hours)
  and monthly salary credits from external payroll accounts, decided by the legacy policy B1
  (docs/22 §2), with the double-entry ledger, session telemetry rendered from the §5.1 targets,
  ground truth and scheduled labels. Gap fills are listed in docs/14 §4.
- The canonical export covers every generated table.

## 2

- The population (docs/14 §2–3, #17): customers, their home-bank accounts, devices and
  established payees, the merchants (0.15 per customer, 1 in 15 a hospital) and the external
  counterparties they pay. Numbers in `generator.yaml`; the gaps it fills are listed in docs/14
  §3. Response models unchanged.
- The `test` profile's canonical-export hash is recorded in `dataset_hashes.yaml`.

## 1

- Response models frozen from KB v1 with seed 99 and concentration 50 (docs/14 §5.2), one
  name-derived stream per (scenario, source) row, with the coached-victim, GENUINE_UNUSUAL and
  UPI_COLLECT_REFUND overrides (including its CUSTOMER_OUTREACH_CALL row). The collect-refund
  scenario's ground truth is APP_COLLECT_REFUND. Telemetry targets as in
  docs/14 §5.1. Coaching adaptation off.
