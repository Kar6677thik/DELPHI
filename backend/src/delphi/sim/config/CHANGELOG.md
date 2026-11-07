# Generator changelog

Every change to `backend/src/delphi/sim/config/*.yaml` or to the generation code that changes
generated data bumps `generator_version` and is recorded here (docs/14 §1 rule 4, §5.4).

The frozen answers also depend on numpy's Dirichlet and PCG64 implementations. If a numpy
upgrade changes them, `test_committed_freeze_matches_kb_v1_and_config` fails even though no
input changed. Then either keep the previous numpy, or regenerate with
`uv run delphi sim freeze-response-models`, bump `generator_version` and record the upgrade here.

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
