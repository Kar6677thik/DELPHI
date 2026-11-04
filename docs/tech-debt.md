# Technical debt register

Each entry: what, why it was accepted, consequence, and the trigger to repay it. Reviewed at
every milestone close (doc 24 §7).

| # | Debt | Why accepted | Consequence | Repay when |
|---|---|---|---|---|
| TD-1 | M1 started before M0's deploy half (k8s, Argo CD, Cloudflare). Only the repo scaffolding M1 needs exists | The engine is pure and needs no infrastructure; the deploy half needs the owner's Pre-M0 answers (server arch, hostname, PAT) | Nothing is deployed yet; CI is not yet set up | Pre-M0 answers arrive → finish M0 |
| TD-2 | `delphi.probes.registry` lists classifier names, but their implementations land in M4 | KB validation rule 8 needs the names now; implementations need sim telemetry (M2) | Rule 8 checks names only; until M4, calling a classifier raises `NotImplementedError` | M4 |
| TD-3 | KB versions are not yet persisted (`kb_versions` table) | The table needs the M0 database layer | KB v1 is loaded from YAML in memory only | M0 database layer |
| TD-4 | Telugu/Hindi/Tamil question variants are absent from `kb/question_bank` | They need a native-speaker review (P-5, A-08) | The phone falls back to English | Before M4 closes |
| TD-6 | The S-4 calibration test (`backend/tests/golden/test_s4_calibration.py`, docs/14 §5.4) is skipped until `delphi.sensing` exists, and its sensing prior is not wired | The prior comes from the trained sensing model (M3); with a fixture prior, 10 of 15 sessions reach the threshold (needs 11) | Collect-refund generation may need tuning before S-4 works | M3 (#36): wire `_sensing_prior`, run the test, tune collect-refund telemetry and answers if it fails |
