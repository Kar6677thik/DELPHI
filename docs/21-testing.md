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
| Smoke load | pytest `-m load` (`just smoke-load`) against a running stack | §5: NFR-02 for 2 minutes | M4 and on demand (manual), never in CI |
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

**Implementation (#131).** `frontend/e2e/rbac.spec.ts`, `stories.spec.ts` and `a11y.spec.ts` run
with the login smoke test, against the same stack.
- **E2E-RBAC** creates a VIEWER through `POST /admin/users`.
  - The navigation hides Admin and Phone, and those routes show "No access" (`rbac.spec.ts`).
  - Every write in the running api's `openapi.json` (POST, PUT, PATCH, DELETE), except an
    allowlist of the writes a viewer may make (login, logout and `POST /cases/{id}/trace-events`,
    which needs only `view`), answers 403 with code `FORBIDDEN`, so a request that fails the CSRF
    check (`CSRF_REJECTED`) cannot pass for one RBAC refused. A new write endpoint is covered
    without editing the test.
  - On a real case (`stories.spec.ts`), the admin sees the disposition form and an enabled
    evidence request; the viewer sees the form hidden ("Only investigators decide cases.") and
    every evidence-request button disabled.
- **The data** (`stories.spec.ts`): the admin seeds `test` with the personas
  (`POST /admin/datasets {profile: "test", personas: true}`) and waits for `/status` to report the
  dataset and models ready, about 10 s. The case comes from S-1 (autoplay). The autoplay
  customer's answers are sampled, so about one S-1 run in five takes the cooling-off path, whose
  window is 600 s in REAL time and opens no case in time; the test runs S-1 again from a reset bank,
  up to three times (60 s each).
- **E2E-RESET** (`stories.spec.ts`): S-1 starts a Guardian session; `POST /admin/demo/reset`
  succeeds, after which that session is gone (404) and there are no cases; S-1 then runs again for
  the same persona, as a new session.
- **E2E-A11Y** runs axe (`@axe-core/playwright`, the WCAG 2.0, 2.1 and 2.2 A and AA rules:
  `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`, `wcag22aa`) on `/login`, the Overview, every route
  in `SCREENS`, the Theatre and the not-found page, signed in as the admin. A route with an id
  opens with one that does not exist, so its not-found state is scanned. These scans run without
  data; a rescan of the screens with data (a case, a session) is a follow-up with the story
  journeys.

## 5. Load test (Locust, M9)

The scenario keeps 20 Guardian sessions/s for 10 minutes using autoplay customers (60%
fast-path, 40% full), plus 5 investigators browsing cases. Pass: Guardian create p95 ≤ 400 ms
(fast-path + passive phase), planner p95 ≤ 50 ms, no 5xx, worker queue lag < 60 s, api memory
stable (< 10% growth over the run).

**Smoke load (M4, #101).** Before the Locust run, `backend/tests/load/test_nfr02_smoke.py`
checks NFR-02 alone: it pays from the personas at 20 sessions/s for 2 minutes through
`POST /guardian/sessions`, as the mock phone does, with 40% of the payments made on a call with
remote access on. It times each session from the request to its first response: the response
itself when the fast path decided it, otherwise the session's first event on `/phone/stream`.
Pass: every payment 201, every session reached the phone, and p95 ≤ 400 ms. Nobody answers the
probes, so those sessions time out on their own. `DELPHI_LOAD_RATE_PER_S` and
`DELPHI_LOAD_DURATION_S` change the rate and length when diagnosing, but such a run isn't the
NFR-02 check.

## 6. Quality gates (CI must be green to merge to `main`)

- ruff (lint + format check), mypy `--strict` for `engine`, `kb` and `chase.tracing`;
  mypy standard for the rest.
- ESLint, `tsc --noEmit`, Prettier check.
- Coverage: `engine` ≥ 95% branches, backend overall ≥ 80% lines; frontend components ≥ 70%.
- All test levels marked "every PR" pass. (CI runs on pull requests only, not on branch pushes:
  #83.)
- E2E passes on PRs to `main`.
- No high/critical vulnerabilities from pip-audit/pnpm audit/Trivy without a documented waiver
  in `security/waivers.yaml` (each waiver expires within 30 days; a Trivy waiver also goes in
  `.trivyignore`). gitleaks scans the whole history, bandit the backend code, and dependency
  review the dependencies a PR adds (`ci.yml`, #127).
- Flaky tests are fixed or quarantined with an issue within 48 h. Retrying until green is not
  allowed (EFG §23 #31).
