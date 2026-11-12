# 21 — Testing strategy and quality gates

The test portfolio follows EFG §23: many fast focused tests, fewer integration tests, a
handful of e2e journeys. Tests MUST be hermetic: no network except the CI Postgres service, no
real LLM, fixed seeds.

## 1. Portfolio

| Level | Tool | Scope | Runs |
|---|---|---|---|
| Unit | pytest | engine, classifiers, taint tracing, feature functions, KB validation, validators, prompts' templates | every PR |
| Property-based | Hypothesis | engine invariants (§2), taint conservation, KB round-trip | every PR |
| Golden | pytest | doc 06 §16 worked example; demo-story engine traces; template narratives | every PR |
| Simulation determinism | pytest | `test` profile dataset SHA-256; response-model freeze file unchanged | every PR |
| Integration | pytest + real PostgreSQL 16 (CI service container; locally `docker compose up -d postgres`). The tests read the server from `DELPHI_TEST_DATABASE_URL` and create and drop their own databases; without it they skip, except in CI, where `DELPHI_REQUIRE_INTEGRATION=1` turns the skip into a failure | repositories, migrations, jobs (claim/retry/idempotency), API routers with auth/RBAC, SSE outbox replay, Guardian session resume after "crash" | every PR |
| Contract | pytest | `openapi.json` up to date; generated TS types up to date | every PR |
| Architecture | import-linter | doc 04 §4.1 contracts | every PR |
| Frontend unit | Vitest + Testing Library | components (HypothesisBar, CandidatesTable, MoneyText, forms) | every PR |
| Slow | pytest `-m slow` with `DELPHI_RUN_SLOW=1` and PostgreSQL 16 | minutes-long acceptance checks, e.g. FR-SIM-02 (the `eval` profile's stats within ±10%) | nightly at 20:30 UTC and on demand (`.github/workflows/nightly.yml`), not on PRs |
| E2E | Playwright (Chromium) against the docker compose stack, `DELPHI_LLM_PROVIDER=template`, `test`+personas dataset | E2E-S1…S5, login/RBAC, demo reset, accessibility scan | PRs to `main` and `main` |
| Load | Locust | §5 | M9 and before demos (manual) |
| Security | gitleaks, pip-audit, pnpm audit, Trivy, bandit | | every PR (Trivy on image builds) |
| LLM eval | `just llm-eval` | doc 15 §8 | manual |
| Restore drill | runbook | doc 23 §7 | M9, then monthly |

## 2. Engine invariants (property tests)

For random valid KBs (2–8 hypotheses, 1–8 sources, 2–4 outcomes, probabilities ≥ 1e-3),
random beliefs and random amounts:

1. The posterior sums to 1 (1e-12) and is non-negative.
2. Observation order does not matter (1e-9).
3. Uninformative source (identical rows) → belief unchanged.
4. VoI_1(a) ≥ −1e-9.
5. V_k(p, S) ≤ R(p) and V_k is non-increasing in k.
6. net_value(a*) > ε ⇒ the plan is not STOP; STOP ⇒ every net_value ≤ ε.
7. Determinism: the same inputs and seed give an identical `PlanResult` (including the
   candidates order).
8. Exploration: the empirical selection frequency over 20,000 seeds matches the propensities
   within 3σ; exploration never picks a probe with net_value < 0 or a higher tier than a*.
9. Guardrails: budgets are never exceeded across random sequences of responses.
10. Prior construction: Σ p0 = 1; p0(UNEXPLAINED) = u·(1 − Σw) exactly.

## 3. Demo-story acceptance tests (engine level, no UI)

ENG-S1 and ENG-S2 inject the fixture priors from doc 06 §17 (`prior_override`). They test the
engine and KB v1 independently of the trained sensing model. The E2E tests (§4) run the same
stories with the real model's prior. If an E2E story diverges because of the prior, that is
risk R-01: fix it in the persona or simulator, never by tuning KB v1 to the test.

| ID | Setup (demo dataset personas, KB v1, scripted responses) | Assertions |
|---|---|---|
| ENG-S1 | Kamala payment; passive outcomes from the persona telemetry (ON_CALL_UNKNOWN_NUMBER, NORMAL session, KNOWN_DEVICE, CLEAN_UNKNOWN beneficiary); coached purpose answer FAMILY_OR_FRIEND in 6 s; spell-breaker answered NO after 11 s with 1 app switch | `SPELL_BREAKER_DIGITAL_ARREST` is among the active probes run; `OTP_STEP_UP` is never selected; the leading hypothesis is `APP_DIGITAL_ARREST` (≥ 0.65); decision `PROTECTIVE_HOLD`; B2 baseline decision `RELEASE` |
| ENG-S2 | Arjun payment; biometric MATCH; any other question answered normally | The first active probe is `BIOMETRIC_STEP_UP`; `OTP_STEP_UP` is never selected; ≤ 2 active probes; decision `RELEASE`; B1 baseline `HOLD_FOR_REVIEW` |
| ENG-S3 | Ravi complaint at T0; Ring-7 schedule | The top 3 candidates have net > 0; every lien amount ≤ traced taint; conservation holds |
| ENG-S4 | 15 collect-refund sessions under KB v1, then KB v1 + `APP_COLLECT_REFUND` | ≥ 70% have P(UNEXPLAINED) ≥ 0.35 before; ≥ 80% are attributed to the new typology after |

If a story assertion fails because of the simulator rather than the engine, fix the
**simulator persona script** (and document it). Never fix it by tuning KB v1 numbers to the
test.

## 4. E2E journeys (Playwright)

| ID | Journey |
|---|---|
| E2E-S1 | Presenter logs in → `/phone` → Kamala → pays → answers each probe per the persona script (purpose: FAMILY_OR_FRIEND promptly; spell-breaker: NO after a delay and with a tab switch). The spell-breaker text is asserted to be Telugu if an APPROVED `te` variant exists in `kb/`, else English → the phone shows the protective screen → `/cases/:id` shows the COERCION protocol, outreach pinned → record CONFIRMS_COERCION → dispose CONFIRM_FRAUD → DD pack complete |
| E2E-S2 | Arjun → biometric → "Payment sent" within 30 s; the session shows biometric selected over OTP |
| E2E-S3 | Run story S3 → chase screen → approve the top 3 → liens PLACED/TICKET_SENT → the golden-hour gauge > 0 |
| E2E-S4 | Run story S4 → Scout run → edit the draft → backtest → shadow → promote as a second user → a new collect-refund payment is attributed to the new typology |
| E2E-S5 | Admin runs an evaluation on the `test` profile → the results table renders three policies |
| E2E-RBAC | A VIEWER cannot see enabled write controls; direct API writes return 403 |
| E2E-RESET | Demo reset returns personas to their initial state; S-1 can run again |
| E2E-A11Y | axe scan on every route: no serious or critical violations |

## 5. Load test (Locust, M9)

The scenario keeps 20 Guardian sessions/s for 10 minutes using autoplay customers (60%
fast-path, 40% full), plus 5 investigators browsing cases. Pass: Guardian create p95 ≤ 400 ms
(fast-path + passive phase), planner p95 ≤ 50 ms, no 5xx, worker queue lag < 60 s, api memory
stable (< 10% growth over the run).

## 6. Quality gates (CI must be green to merge to `main`)

- ruff (lint + format check), mypy `--strict` for `engine`, `kb` and `chase.tracing`;
  mypy standard for the rest.
- ESLint, `tsc --noEmit`, Prettier check.
- Coverage: `engine` ≥ 95% branches, backend overall ≥ 80% lines; frontend components ≥ 70%.
- All test levels marked "every PR" pass. (CI runs on pull requests only, not on branch pushes:
  #83.)
- E2E passes on PRs to `main`.
- No high/critical vulnerabilities from pip-audit/pnpm audit/Trivy without a documented waiver
  in `security/waivers.yaml` (each waiver expires within 30 days).
- Flaky tests are fixed or quarantined with an issue within 48 h. Retrying until green is not
  allowed (EFG §23 #31).
