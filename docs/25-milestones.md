# 25 — Milestones

The plan is **risk-driven** (EFG §1): deploy the walking skeleton first, so every later milestone
ships to the live server. Then build the riskiest piece, the engine, while it is still cheap to
change. Every milestone ends with:

- all its exit criteria met;
- CI green, deployed to the home server and verified;
- `docs/tech-debt.md` reviewed;
- the owner tags the merge commit `mN` (manual; CI never tags).

Estimates are in focused engineering days for one developer, and are for planning only.

## Pre-M0 — owner checklist (no code)

| # | Item | Needed by |
|---|---|---|
| P-1 | Confirm server CPU architecture (A-02), free CPU/RAM (A-01), and that k3s ships the NetworkPolicy controller and `local-path` | M0 |
| P-2 | Choose the public hostname (`delphi.<domain>`) on Cloudflare (A-03) | M0 |
| P-3 | ~~Create the GitHub fine-grained PAT (`read:packages`) for cluster pulls~~. Dropped: the repo and its package are public (A-05) | — |
| P-4 | Anthropic API key with a spend limit set in the Anthropic console | M5 |
| P-5 | Native-speaker reviewers for Telugu / Hindi / Tamil variants (A-08) | M4 (Telugu for S-1) |

## Overview

| M | Name | Est. days | Depends on | Demo stories unlocked |
|---|---|---|---|---|
| M0 | Walking skeleton and continuous deployment | 4 | — | — |
| M1 | Knowledge base and decision engine | 5 | M0 | golden test |
| M2 | Simulation | 6 | M1 (KB keys) | — |
| M3 | Sensing layer | 4 | M2 | — |
| M4 | Guardian loop and mock phone | 7 | M1, M3 | S-1 (Guardian half), S-2 |
| M5 | Navigator | 6 | M4 | S-1 complete |
| M6 | Jatayu (Chase) loop | 5 | M2, M3, M5 | S-3 |
| M7 | Learning loop and Typology Scout | 6 | M4, M5 | S-4 |
| M8 | Evaluation harness and metrics | 5 | M4–M7 | S-5 |
| M9 | Hardening and demo readiness | 4 | all | full rehearsal |
| | **Total** | **≈ 52** | | |

If time is short, the **demo-critical path** is M0 → M1 → M2 → M3 → M4 → M5 → M6 → M8 (with
M7's Scout part before M8, and the learning part after). The cut line, if scope must shrink: the
OPE and trace analytics (M7), the ablations (M8) and the load test (M9). These are marked
*(cuttable)* below.

---

## M0 — Walking skeleton and continuous deployment

**Goal:** a trivial app that is built, tested, published and deployed to the home server by a
manual, owner-approved run on `main` (ADR-0015), and is reachable at the public URL behind a
login.

Deliverables:

1. The repo layout (doc 24 §2), `CLAUDE.md`, `.githooks/commit-msg` (rejects Co-Authored-By),
   `docs/tech-debt.md`.
2. Backend skeleton: settings (fail fast), structured logging, DB session, Alembic with the first
   migration (roles, `users`, `auth_sessions`, `idempotency_keys`, `audit_log`, `jobs`,
   `outbox_events`), `/health`, `/ready`, login/logout/me, bootstrap admin, RBAC dependency, the
   audit hash chain, the job runner skeleton, SSE hub with outbox.
3. Frontend skeleton: login page, app shell with role-aware navigation, Overview placeholder,
   SSE connection indicator, generated API types.
4. Dockerfile (multi-stage, non-root, read-only FS), `compose.yaml`, `justfile`.
5. `ci.yml` and the manual `deploy.yml` (doc 23 §3), with the e2e job running a login smoke test.
6. `deploy/k8s` base, jobs and the example overlay, and the bootstrap runbook (doc 23 §6)
   executed on the server by its operator (ADR-0015).
7. ADRs 0001–0014 accepted (they already exist in `docs/adr/`).

Exit criteria:

- [ ] Running Deploy on `main` (and approving it) for a commit that changes a string on the
  Overview page shows it at `https://delphi.karthiksurkanti.in`. The deploy workflow's verify job
  is green
  (FR-DEP-01, NFR-10).
- [ ] `workflow_dispatch` on `main` works; dispatch on another branch is refused.
- [ ] No commit in the repo has a Co-Authored-By trailer. The hook is installed. The workflows
  have no write permission to contents (FR-DEP-02).
- [ ] `/metrics` is not reachable from the internet (FR-DEP-04).
- [ ] Integration tests for FR-PLT-01..09 pass.

## M1 — Knowledge base and decision engine

**Goal:** the pure engine and KB v1, proven by golden and property tests.

Deliverables: `kb.model` (Pydantic) + JSON Schemas; the YAML loader; the validator (doc 07 §3);
the KB v1 files (doc 07 §4–10); `kb_versions` table + seed of v1; `engine` (doc 06 in full);
the research-example fixture; benchmarks.

Exit criteria:

- [x] Golden test doc 06 §16 passes exactly (FR-ENG-02, FR-ENG-03).
- [x] Property tests 1–10 (doc 21 §2) pass, with 500 examples each for the cheap invariants
  and 200 for the planner ones (FR-ENG-01, 04–07).
- [x] Planner benchmark p95 ≤ 50 ms (NFR-03): measured 7.8 ms on the dev machine.
- [x] `delphi kb validate kb/` runs in CI; each validation rule has a failing-fixture test
  (FR-KB-01).
- [x] Engine branch coverage ≥ 95%; `mypy --strict` clean.
- [x] Story behaviours of doc 06 §17 pinned as golden tests (ENG-S1/S2 at engine level).
- [x] KB versions table, seed of v1, and the read-only API (list/get/diff/export)
  (FR-KB-02, FR-KB-04 partial): #15.

## M2 — Simulation

**Goal:** a deterministic synthetic bank with every scenario, response model, label process and
persona.

Deliverables: the `bank` and `sim_truth` schemas; the generator for the `test`/`demo`/`eval`
profiles; the frozen response models; the label arrival model; the legacy policy (B1) in the
history; personas; the sim registry/FRI/telco/CFCFRMS adapters; the `GENERATE_DATASET` job;
`delphi seed`; a dataset stats report.

Exit criteria:

- [ ] `test` profile hash is stable across runs and machines (FR-SIM-01).
- [ ] The `eval` stats report matches the configured scenario rates within ±10% (FR-SIM-02).
- [ ] The response-model snapshot is committed and tested (FR-SIM-03).
- [ ] Label tests: selective labels (B1-held items unlabeled), label noise (FR-SIM-04).
- [ ] Ground-truth isolation: the DB permission test plus the import contract (FR-SIM-07).
- [ ] `demo` generation completes in ≤ 10 min on the server; `eval` in ≤ 30 min.

## M3 — Sensing layer

**Goal:** calibrated priors, anomaly percentile and mule scores, served online.

Deliverables: feature builders (online and offline), the parity test, prior model training with
temperature scaling, the isolation forest, the mule scorer, `model_artifacts`, the
`TRAIN_SENSING` job, the `MULE_SWEEP` job (scores only; it starts opening ACCOUNT_REVIEW cases
in M5), the inference service, top feature contributions, and `/status` readiness reporting.

Exit criteria:

- [ ] The parity test passes (FR-SNS-02). The leakage-guard test passes (FR-SNS-01).
- [ ] On `eval`: ECE ≤ 0.05, macro AUC ≥ 0.85, mule AUC ≥ 0.90 (FR-SNS-03, FR-SNS-05), or a
  documented deviation with the owner's sign-off.
- [ ] A failed gate keeps the previous artifact; the first-ever artifact activates with
  `gate_passed = false` and a warning (FR-SNS-06).
- [ ] The `test` profile trains end to end (no gate) in ≤ 60 s, so CI and E2E have models.
- [ ] Inference p95 ≤ 20 ms (NFR-03).
- [ ] The seed job trains models on the server after the demo dataset is generated.

## M4 — Guardian loop and mock phone

**Goal:** live payment-time probing, end to end on the deployed site.

Deliverables: the `guardian` module (doc 08); probe adapters and classifiers; question bank
serving and rotation; the `MockPhoneChannel`/`SimCustomerChannel` (worker-run sessions); the
Guardian API and SSE; the `/phone` presenter view; the `/guardian` monitor and session detail;
the B1/B2 display baselines; the session DD pack; the demo story runner for S-1/S-2; Telugu
variants for the S-1 probes committed to `kb/` (reviewed, P-5).

**Case stub (pulled forward from M5 so that holds have somewhere to go):** the `cases` table and
migration, `navigator.open_case()` with the correct initial posterior and protocol, `GET /cases`
and `GET /cases/{id}` (read-only), and a minimal case list page. Evidence, dispositions and the
console are built in M5.

Exit criteria:

- [ ] ENG-S1 and ENG-S2 pass (FR-GDN-04).
- [ ] E2E-S2 passes. The Guardian half of E2E-S1 passes (up to the protective screen) (FR-GDN-10).
- [ ] FR-GDN-01..15 tests pass, including the restart-resume test and the fail-safe test.
- [ ] The background autoplay traffic runs on the server, and the monitor updates live.
- [ ] NFR-02 checked with a 2-minute smoke load (the full load test is in M9).

## M5 — Navigator

**Goal:** the investigator console with next-best-evidence, narratives, dispositions, campaigns
and DD packs.

Deliverables: the `navigator` module (doc 09), completing the M4 case stub; ACCOUNT_REVIEW
case creation from `MULE_SWEEP` and complaints; evidence adapters and rules; the case API; the LLM
module (doc 15) with the Anthropic adapter, the template adapter, validators, prompts v1 and the
budget guard; case console UI; the coercion protocol + police alert drafts; campaigns; the case
DD pack (JSON + HTML); trace events; `QUESTION_VARIANTS` generation + the review UI.

Exit criteria:

- [ ] E2E-S1 passes fully, in template mode (FR-NAV-10, FR-NAV-11).
- [ ] FR-NAV-01..12 tests pass.
- [ ] LLM validator tests cover valid output, a bad citation, a wrong number, decision
  language, refusal, timeout and budget exceeded (FR-NAV-06, NFR-13).
- [ ] `just llm-eval` run once with a real key; report committed (doc 15 §8).
- [ ] DD packs are complete for 100% of S-1 cases (NFR-07).

## M6 — Jatayu (Chase) loop

**Goal:** golden-hour tracing and targeted liens on a moving mule network.

Deliverables: the `chase` module (doc 10); taint tracing; scoring; liens and tickets; the
re-trace job; the live mule-movement tick; the Chase UI (graph, timer, gauge, candidates,
tickets); story S-3 runner.

Exit criteria:

- [ ] ENG-S3 and E2E-S3 pass (FR-CHS-03).
- [ ] Taint conservation and the lien ≤ taint property tests pass (FR-CHS-01, FR-CHS-05).
- [ ] No lien without approval (FR-CHS-04). Every lien has recorded reasons (FR-CHS-06).
- [ ] Trace ≤ 5 s on the demo dataset (NFR-04).

## M7 — Learning loop and Typology Scout

**Goal:** the system learns likelihoods and discovers the hidden typology live.

Deliverables: the `scout` module (doc 11): queue, HDBSCAN run, clusters, drafts, editor UI,
backtest, shadow evaluations, four-eyes promotion, rollback. The `learning` module (doc 12):
Dirichlet runs with diff reports and held-out log loss, promotion guard, label coverage, yield
drift, *(cuttable)* OPE and trace analytics. KB pages (versions, diff, export, promote,
rollback).

Exit criteria:

- [ ] ENG-S4 and E2E-S4 pass (FR-SCT-07).
- [ ] FR-SCT-01..06 and FR-LRN-01, 02, 04, 05 pass. FR-LRN-03 passes (if not cut).
- [ ] A learning run on the demo dataset produces a CANDIDATE whose held-out log loss is ≤ the
  parent's (expected, because the simulator's truth differs from KB v1).
- [ ] The coaching-adaptation switch triggers `PROBE_YIELD_DEGRADED` within 7 sim-days on the
  `test` profile.

## M8 — Evaluation harness and metrics

**Goal:** the reproducible results table (S-5).

Deliverables: the `evaluation` module (doc 22); B1/B2/DELPHI + *(cuttable)* ablations; the
simulated investigation queue; the metrics with bootstrap CIs; the evaluation page and charts;
the markdown export; the live KPIs on Overview.

Exit criteria:

- [ ] E2E-S5 passes on `test` (FR-EVL-04).
- [ ] A full `eval` run completes in ≤ 30 min on the server. Two runs with the same seed give
  identical metrics (NFR-01).
- [ ] The report is committed to `docs/reports/` with an honest statement for each
  pre-registered target (doc 22 §5).

## M9 — Hardening and demo readiness

**Goal:** confidence for a live demo in front of judges.

Deliverables: *(cuttable)* the Locust load test and fixes; `EXPLAIN ANALYZE` of the hot queries;
the security review checklist (doc 19) with fixes; the backup restore drill; the runbook dry run
(simulated failures: kill the api pod mid-session, break the tunnel, exhaust the LLM budget);
the demo reset rehearsal; the presenter "Phone only" projector view; a 7-minute autoplay script
for all stories; and the pitch-support artefacts (slide-ready golden-test table, evaluation
headline numbers, novelty statement from research §17.1).

Exit criteria:

- [ ] NFR-02, 03, 04, 06 met under load, or documented.
- [ ] The restore drill passes in ≤ 30 min (NFR-11, FR-DEP-05).
- [ ] E2E-RESET, E2E-RBAC and E2E-A11Y pass.
- [ ] Two consecutive full rehearsals (reset → S-1 → S-2 → S-3 → S-4 → S-5) on the live site
  without manual intervention.
- [ ] Zero open HIGH/CRITICAL vulnerabilities without a waiver.

---

## Risk register

| ID | Risk | Likelihood | Impact | Mitigation | Owner milestone |
|---|---|---|---|---|---|
| R-01 | Demo stories don't emerge naturally from KB v1 + simulator (e.g. the planner picks an unexpected probe) | Medium | High | Engine-level story tests from M4; tune only the persona scripts and simulator, never KB v1 to the test (doc 21 §3) | M4 |
| R-02 | The lookahead planner is too slow in Python | Low | Medium | Memoisation + numpy; fallback: depth 2 for Guardian (config) | M1 |
| R-03 | ~~Image Updater CRD fields differ from the spec~~. Closed: ADR-0015 replaced Argo CD and Image Updater | — | — | — | — |
| R-04 | The home server lacks resources for `eval` generation and evaluation | Medium | Medium | Run `eval` jobs at night; reduce to 10k customers (config) with a documented note | M2/M8 |
| R-05 | Sensing gates not met on synthetic data | Low | Medium | Document the deviation; the engine's value does not depend on prior quality | M3 |
| R-06 | Telugu/Hindi/Tamil review unavailable | Medium | Low | The English fallback keeps every story working; S-1 shows English | M4 |
| R-07 | LLM cost or outage during the demo | Low | Low | Template fallback, budget guard, kill switch (NFR-12) | M5 |
| R-08 | Public exposure attracts abuse | Medium | Medium | Login on everything, rate limits, Cloudflare, read-only guest | M0/M9 |
| R-09 | Results look circular (the simulator built from the KB) | Medium | High | Perturbed response models, coached victims, label noise, honest caveats box (doc 22 §6) | M8 |
| R-10 | Scope creep | High | High | Doc 01 §4 non-goals; the cut line above; a spec change needs a PR | all |
