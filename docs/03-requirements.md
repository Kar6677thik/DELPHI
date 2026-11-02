# 03 — Requirements

Every requirement has an ID, a statement, and an acceptance check (test id or observable
condition). Milestones (doc 25) reference these IDs. "Doc" points to the detailed spec.

## 1. Functional requirements

### Platform (PLT)

| ID | Requirement | Acceptance | Doc |
|---|---|---|---|
| FR-PLT-01 | Users log in with username/password and get a server-side session; logout ends it | Integration tests; E2E-RBAC | 19 §2 |
| FR-PLT-02 | Every endpoint enforces the permission matrix (deny by default) | Per-endpoint parametrised RBAC test | 19 §3 |
| FR-PLT-03 | The first start with an empty `users` table creates the bootstrap ADMIN from env, or fails fast if unset | Integration test | 19 §2 |
| FR-PLT-04 | Every state transition writes a hash-chained audit entry in the same transaction | Test: rollback leaves no audit row; chain verify passes | 17 §4 |
| FR-PLT-05 | State-creating POSTs honour `Idempotency-Key` | Integration test (replay → same response; different body → 409) | 16 §1 |
| FR-PLT-06 | Mutable aggregates use optimistic locking via ETag/If-Match | Test: stale If-Match → 412 | 16 §1 |
| FR-PLT-07 | Jobs are claimed with SKIP LOCKED, retried up to 3 times with backoff, and are idempotent | Integration tests incl. a crash mid-job | 04 §5.4 |
| FR-PLT-08 | SSE delivers events with replay via Last-Event-ID | Integration test | 16 §3 |
| FR-PLT-09 | `/health` returns the build version without touching the DB; `/ready` reflects the DB, the migration head and (from the KB versions table) an ACTIVE KB. Dataset and model readiness are reported by `/status`, not `/ready` | Integration test; deploy verify | 20 §3 |
| FR-PLT-10 | Admins manage users (create, disable, reset password, change role) | E2E-RBAC | 16 §2 |

### Knowledge base (KB)

| ID | Requirement | Acceptance | Doc |
|---|---|---|---|
| FR-KB-01 | KB v1 is authored as YAML in `kb/` and validated by `delphi kb validate` (all rules in doc 07 §3) | CI step; unit tests per rule | 07 |
| FR-KB-02 | KB versions are immutable, content-hashed, with exactly one ACTIVE and ≤ 1 SHADOW | DB constraints + tests | 07 §1 |
| FR-KB-03 | Every session/case/chase records its KB version id | Integration test | 05 |
| FR-KB-04 | KB versions can be diffed, exported as YAML zip, promoted, rejected, and rolled back with audit | API tests | 16 §2 |
| FR-KB-05 | Question variants are reviewed (approve/edit/reject) before inclusion; no LLM text reaches customers unreviewed | API test: PENDING variants are never served | 07 §9, 15 §1 |

### Engine (ENG)

| ID | Requirement | Acceptance | Doc |
|---|---|---|---|
| FR-ENG-01 | Bayesian update in log space with likelihood floors and uniform UNEXPLAINED | Property tests 1–3 | 06 §2–3 |
| FR-ENG-02 | Bayes risk and decision from the profile loss tables, including vulnerability scaling | Golden test | 06 §4 |
| FR-ENG-03 | Myopic VoI computed and reported for every candidate | Golden test (OTP ₹0.00, CALL_STATUS ₹1,685.80) | 06 §5, §16 |
| FR-ENG-04 | Depth-limited lookahead selection with deterministic tie-breaks and stopping rule | Property tests 5–7 | 06 §6 |
| FR-ENG-05 | Guardrails and eligibility with recorded reasons | Property test 9; unit tests | 06 §8 |
| FR-ENG-06 | ε-exploration with logged propensities, never adding friction or net-negative probes | Property test 8 | 06 §10 |
| FR-ENG-07 | Prior construction with open mass and KB-only typologies | Property test 10 | 06 §11 |
| FR-ENG-08 | "What would change my mind" and settle % | Unit tests | 06 §12–13 |

### Guardian (GDN)

| ID | Requirement | Acceptance | Doc |
|---|---|---|---|
| FR-GDN-01 | Create a session per payment, pin the KB version, build the prior | Integration test | 08 §2 |
| FR-GDN-02 | Fast path releases qualifying payments without probes but persists the session | Unit + integration | 08 §3 |
| FR-GDN-03 | Run all eligible passive probes in parallel with a 300 ms timeout; failures do not block | Test with an injected slow adapter | 08 §2 |
| FR-GDN-04 | Plan active probes with the engine and deliver them to the customer channel | ENG-S1, ENG-S2 | 08 §2 |
| FR-GDN-05 | Classify responses deterministically per doc 08 §5 | Unit tests per classifier and threshold edge | 08 §5 |
| FR-GDN-06 | Handle timeouts (step-ups → FAILED_OR_ABANDONED; others → no update) | Unit tests | 08 §2 |
| FR-GDN-07 | Customer cancel ends the session and cancels the payment | Integration test | 05 §2 |
| FR-GDN-08 | Apply decisions to payment status and open cases for holds (protocol COERCION for PROTECTIVE_HOLD) | Integration test | 08 §2 |
| FR-GDN-09 | Render question probes in the customer's language from APPROVED variants, falling back to English; rotate variants per customer | Unit test | 07 §9 |
| FR-GDN-10 | Show the protective education screen on PROTECTIVE_HOLD | E2E-S1 | 08 §7 |
| FR-GDN-11 | Flag sessions with terminal P(UNEXPLAINED) ≥ 0.35 for the Scout | Unit test | 11 §1 |
| FR-GDN-12 | Compute B1/B2 baseline decisions per session for display | Unit test | 08 §6 |
| FR-GDN-13 | Fail-safe: an engine error results in HOLD_FOR_REVIEW with terminal_reason ENGINE_ERROR | Fault-injection test | 05 §2 |
| FR-GDN-14 | Sessions resume correctly after an api restart | Integration test (kill task mid-session, restart) | 08 §2 |
| FR-GDN-15 | Session DD pack with all sections | Unit test of completeness | 09 §7 |

### Navigator (NAV)

| ID | Requirement | Acceptance | Doc |
|---|---|---|---|
| FR-NAV-01 | Cases are created by every trigger in doc 09 §1, with the correct initial posterior | Integration tests | 09 §1 |
| FR-NAV-02 | Next-best-evidence ranking by net value (depth 2) with settle % and minutes | Unit + API tests | 09 §2 |
| FR-NAV-03 | "Evidence sufficient" banner when the planner stops | UI test | 09 §2 |
| FR-NAV-04 | What-would-change-my-mind | Unit test | 06 §12 |
| FR-NAV-05 | Evidence fetch is deterministic per doc 09 §4 and updates the posterior | Unit tests per rule and boundary | 09 §4 |
| FR-NAV-06 | LLM narratives with citations, validated, with template fallback; labelled by source | Unit tests with a fake provider (valid, invalid citations, wrong numbers, refusal, timeout) | 15 |
| FR-NAV-07 | Dispositions with override rationale and optimistic locking | API tests | 05 §4 |
| FR-NAV-08 | Investigator trace events recorded and analytics computed | Integration test | 09 §5 |
| FR-NAV-09 | Campaign grouping per the merge rule, with stable ids | Unit tests (merge, chains, GENUINE excluded) | 05 §5 |
| FR-NAV-10 | Coercion protocol: outreach pinned; police alert drafted, sent only by a human | E2E-S1; API test | 09 §6 |
| FR-NAV-11 | Case DD pack (JSON + printable HTML) with completeness flag | Unit + E2E | 09 §7 |
| FR-NAV-12 | Outreach outcomes create OUTREACH_CONFIRMED labels | Integration test | 09 §3 |

### Chase (CHS)

| ID | Requirement | Acceptance | Doc |
|---|---|---|---|
| FR-CHS-01 | Proportional taint tracing with cash-out leakage and pruning | Unit tests + conservation property test | 10 §3 |
| FR-CHS-02 | Candidate scoring (p_present, p_mule, ERV, wrongful cost, net) | Unit tests with hand-computed fixtures | 10 §4 |
| FR-CHS-03 | Ranked recommendations with net > 0 | ENG-S3 | 10 §4 |
| FR-CHS-04 | Liens only after explicit human approval | API test: no lien without the approve call | 10 §5 |
| FR-CHS-05 | Lien amount ≤ traced taint and ≤ balance; never the whole account unless all of it is taint | Property test | 05 §6 |
| FR-CHS-06 | Recorded reasons and evidence refs on every lien | Unit test | 10 §5 |
| FR-CHS-07 | External liens via simulated CFCFRMS tickets with latency and failure | Integration test | 10 §5 |
| FR-CHS-08 | Periodic re-tracing while the operation is open | Integration test with a virtual clock | 10 §5 |
| FR-CHS-09 | Golden-hour recovery ratio and tracing delay | Unit test | 10 §6 |

### Scout (SCT)

| ID | Requirement | Acceptance | Doc |
|---|---|---|---|
| FR-SCT-01 | Unexplained queue with entry and exit rules | Integration test | 11 §1 |
| FR-SCT-02 | HDBSCAN clustering with distinguishing features and draft likelihood rows | Unit test on a synthetic cluster | 11 §2 |
| FR-SCT-03 | Typology draft editing with validation and revisioning | API tests | 11 §3 |
| FR-SCT-04 | Backtest report | Integration test | 11 §4 |
| FR-SCT-05 | Shadow evaluations on live traffic | Integration test | 11 §5 |
| FR-SCT-06 | Four-eyes promotion with shadow minimum; atomic flip; rollback | API tests | 11 §6 |
| FR-SCT-07 | Demo story S-4 works end to end | ENG-S4, E2E-S4 | 11 §7 |

### Learning (LRN)

| ID | Requirement | Acceptance | Doc |
|---|---|---|---|
| FR-LRN-01 | Dirichlet updates from labelled observations, idempotent, ESS cap, held-out log loss | Unit + integration | 12 §2 |
| FR-LRN-02 | Learning output is a CANDIDATE KB with a diff report; the promotion guard applies | API test | 12 §2 |
| FR-LRN-03 | OPE (SNIPS) for the first active probe, with ESS and CIs | Unit test against a hand-computed example | 12 §3 |
| FR-LRN-04 | Label coverage metric | Unit test | 12 §4 |
| FR-LRN-05 | Probe-yield drift alert | Unit test with an injected drift | 12 §5 |

### Sensing (SNS)

| ID | Requirement | Acceptance | Doc |
|---|---|---|---|
| FR-SNS-01 | Payment and account features per doc 13 with leakage guard | Unit tests | 13 §2–3 |
| FR-SNS-02 | Online/offline feature parity | Parity test (500 payments) | 13 §2 |
| FR-SNS-03 | Calibrated prior model with gates (ECE ≤ 0.05, macro AUC ≥ 0.85 on eval) | Training job report | 13 §4 |
| FR-SNS-04 | Isolation-forest anomaly percentile | Unit test | 13 §5 |
| FR-SNS-05 | Mule scorer with gate (AUC ≥ 0.90 on eval) and periodic sweep | Training report; integration test | 13 §6 |
| FR-SNS-06 | Model artifacts versioned in the DB; a failed gate keeps the previous artifact | Integration test | 13 §7 |

### Simulation (SIM)

| ID | Requirement | Acceptance | Doc |
|---|---|---|---|
| FR-SIM-01 | Deterministic generation of the `test`/`demo`/`eval` profiles | Hash test on `test` | 14 §1–2 |
| FR-SIM-02 | All scenarios incl. the hidden one, with the configured rates | Stats report within ±10% of the configured rates on `eval` | 14 §4 |
| FR-SIM-03 | Response models frozen from a seeded Dirichlet perturbation, with the override rules | Snapshot test | 14 §5 |
| FR-SIM-04 | Label arrival model incl. selective labels and label noise | Unit tests | 14 §7 |
| FR-SIM-05 | Personas and demo stories S-1…S-4 runnable interactive/autoplay | E2E | 14 §6, §10 |
| FR-SIM-06 | Live sim tick (mule transfers, background payments, ticket acks, labels) with time scale | Integration test | 14 §8 |
| FR-SIM-07 | Ground truth readable only by `sim`/`evaluation` (code contract + DB grants) | Architecture test; DB permission test | 04 §4.1, 17 §2 |
| FR-SIM-08 | Demo reset ≤ 60 s restores the post-seed state | E2E-RESET | 14 §10 |

### Evaluation (EVL)

| ID | Requirement | Acceptance | Doc |
|---|---|---|---|
| FR-EVL-01 | Harness replays the identical stream through B1, B2, DELPHI and the ablations, with common random numbers | Integration test on `test` | 22 §1–2 |
| FR-EVL-02 | Simulated investigation queue with equal capacity | Unit test | 22 §3 |
| FR-EVL-03 | All metrics per doc 22 §4 with cluster-bootstrap CIs | Unit tests on hand-built micro-streams | 22 §4 |
| FR-EVL-04 | Results page and markdown export with reproducibility block | E2E-S5 | 22 §6 |
| FR-EVL-05 | Live KPI summary endpoint | API test | 22 §7 |

### Frontend (UI)

| ID | Requirement | Acceptance | Doc |
|---|---|---|---|
| FR-UI-01 | All routes in doc 18 §2 with role-aware controls | E2E | 18 |
| FR-UI-02 | Mock phone with all probe UI types and persona languages | E2E-S1/S2 | 08 §4, 18 |
| FR-UI-03 | Shared components implemented once (HypothesisBar, CandidatesTable, RiskTable, MoneyText, …) | Component tests | 18 §3 |
| FR-UI-04 | Explicit loading/empty/error states; conflict handling | Component tests | 18 §4 |
| FR-UI-05 | Accessibility WCAG 2.2 AA | E2E-A11Y | 18 §5 |

### Deployment (DEP)

| ID | Requirement | Acceptance | Doc |
|---|---|---|---|
| FR-DEP-01 | Every push to `main` and every manual dispatch on `main` builds, tests, publishes and verifies the deployment | A green deploy workflow run with the verify step | 23 §3 |
| FR-DEP-02 | No cluster credentials or repo-write credentials in GitHub Actions; no bot commits; only `main` can publish a deployable image | Workflow review checklist; `permissions` blocks; a test workflow run from a non-main branch that names `production` is rejected; the package's repo access is Read | 23 §3 |
| FR-DEP-03 | Migrations run as a PreSync hook; seeding is idempotent as a PostSync hook | First deploy on an empty cluster; a second deploy is a no-op seed | 23 §4 |
| FR-DEP-04 | Public access only via the Cloudflare Tunnel; metrics not reachable publicly | From outside: `GET /metrics` and `GET /api/v1/metrics` must not return a Prometheus exposition body (no `# HELP` lines). The SPA fallback serves `index.html` only for non-`/api` paths, and unknown `/api/*` paths return a 404 problem+json | 23 §4 |
| FR-DEP-05 | Nightly backups and a passing restore drill | Drill report | 23 §7 |

## 2. Non-functional requirements

| ID | Requirement | Acceptance |
|---|---|---|
| NFR-01 | **Determinism**: same dataset seed + KB + code → identical datasets, sessions (incl. exploration), evaluations | Hash and replay tests |
| NFR-02 | **Guardian latency**: session creation to first response (fast path or first probe prompt) p95 ≤ 400 ms at 20 sessions/s | Load test |
| NFR-03 | **Engine latency**: planner p95 ≤ 50 ms (|S| ≤ 8, max |O| = 4, k = 3, |H| ≤ 8); sensing inference p95 ≤ 20 ms | Benchmark test in CI (warn) and load test (gate) |
| NFR-04 | **Evidence fetch** p95 ≤ 2 s; chase trace ≤ 5 s on the demo dataset | Integration benchmark |
| NFR-05 | **Availability** (demo): planned downtime only during rollouts; zero-downtime rolling update for the api | Rollout observation (readiness gates) |
| NFR-06 | **Resource fit**: the whole namespace fits in 4 CPU / 8 GB RAM at steady state | `kubectl top` during the load test |
| NFR-07 | **Auditability**: 100% of decisions have complete DD packs; the audit chain verifies | Metric + verify endpoint |
| NFR-08 | **Security**: doc 19 controls; no HIGH/CRITICAL vulns without a waiver | CI gates |
| NFR-09 | **Maintainability**: CI gates in doc 21 §6; module contracts | CI |
| NFR-10 | **Deploy lead time**: push to `main` → live and verified ≤ 15 min (p90) | Workflow timings |
| NFR-11 | **Recoverability**: restore from the latest backup ≤ 30 min | Drill |
| NFR-12 | **LLM independence**: every feature works with `DELPHI_LLM_PROVIDER=template` | E2E runs in template mode |
| NFR-13 | **Cost guard**: LLM tokens ≤ the daily budget; above it, template | Unit test |
| NFR-14 | **Accessibility**: WCAG 2.2 AA | E2E-A11Y |
