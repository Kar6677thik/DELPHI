# Generator changelog

Every change to `backend/src/delphi/sim/config/*.yaml` or to the generation code that changes
generated data bumps `generator_version` and is recorded here (docs/14 §1 rule 4, §5.4).

The frozen answers also depend on numpy's Dirichlet and PCG64 implementations. If a numpy
upgrade changes them, `test_committed_freeze_matches_kb_v1_and_config` fails even though no
input changed. Then either keep the previous numpy, or regenerate with
`uv run delphi sim freeze-response-models`, bump `generator_version` and record the upgrade here.

## 1

- Response models frozen from KB v1 with seed 99 and concentration 50 (docs/14 §5.2), one
  name-derived stream per (scenario, source) row, with the coached-victim, GENUINE_UNUSUAL and
  UPI_COLLECT_REFUND overrides (including its CUSTOMER_OUTREACH_CALL row). The collect-refund
  scenario's ground truth is APP_COLLECT_REFUND. Telemetry targets as in
  docs/14 §5.1. Coaching adaptation off.
