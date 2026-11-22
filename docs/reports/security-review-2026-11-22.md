# Security review, 2026-11-22 (#127, docs/19)

Every docs/19 control was checked against the code and config on `main` at the time of review,
not against the docs. Paths are relative to `backend/src/delphi/` unless they say otherwise.

**Status:**
- **OK:** enforced in code or config.
- **Fixed:** fixed in this review's PR.
- **Open:** still to do (see the end of this report).
- **Later:** the feature isn't built yet.

## Authentication (docs/19 §2)

| Control | Status | Where |
|---|---|---|
| Argon2id with the argon2-cffi defaults (t=3, 64 MiB, p=4); minimum length 12 | OK | `platform/accounts.py` (`PasswordHasher()`, length check), admin DTOs |
| Usernames trimmed and lower-cased; matched case-insensitively | OK | `platform/accounts.py`, `api/routers/auth.py` |
| Lockout: 5 failures per (username, IP) in 15 min → 429 + `Retry-After`, even for the right password | OK | `platform/auth_sessions.py` throttle, checked before authenticating |
| Dummy-hash check for unknown usernames; sign-in, failure and sign-out audited | OK | `platform/accounts.py`, `api/routers/auth.py` |
| Session cookie `HttpOnly; Secure; SameSite=Strict; Path=/`; only SHA-256 stored; idle 12 h and absolute 7 d; new token on sign-in; disabled user's sessions end | OK | `api/routers/auth.py`, `platform/auth_sessions.py` |
| Bootstrap admin, fail fast when `users` is empty | OK | `platform/accounts.py`, `cli.py` |

## Permissions (docs/19 §3)

| Control | Status | Where |
|---|---|---|
| Every route declares its permission (deny by default) | OK | `api/security.py` `require()`; `tests/integration/test_rbac.py` fails on any route it doesn't list, and tests every role against every route |
| Public routes: health, ready, login | Fixed (docs) | `GET /api/v1/openapi.json` is also public. The same file is in this public repo, so docs/16 now says so |
| Four-eyes promotion | Later | No KB promotion endpoint yet (#119) |

## Web security (docs/19 §4)

| Control | Status | Where |
|---|---|---|
| CSRF: SameSite + `X-Delphi-Csrf` + Origin, before routing, login included | OK | `api/web_security.py` |
| Security headers on every response, with the exact CSP | OK | `api/web_security.py`; wraps the rate-limit and CSRF responses too |
| No CORS | OK | No CORS middleware |
| `dangerouslySetInnerHTML` banned | OK | `frontend/eslint.config.js`; it is never used |
| DD pack HTML escaped | Fixed (docs) | `guardian/ddpack.py` escapes every value with `html.escape`. docs/19 named Jinja2, so it now says what the code does |
| Bounded request strings | Fixed | The disposition `decision`/`typology` and the evidence request `source_key`/`outcome` were unbounded. They are now 1–64 characters (`api/routers/cases.py`) |
| Amount ≤ ₹10 crore | Later | No request takes an amount yet; add the limit with the payment-create endpoint |
| Separate request DTOs; idempotency per docs/16 | OK | `api/routers/*`, `platform/idempotency.py` |

## Information disclosure

| Control | Status | Where |
|---|---|---|
| problem+json hides internals | OK | `api/request_context.py` → generic message + request id |
| Secrets kept out of logs | Fixed | Masking went by key name only. Strings (messages, values, formatted tracebacks) are now scrubbed of URL passwords and Anthropic keys too (`platform/logging.py`) |
| `/metrics` not reachable through the tunnel | OK | Own port (9000); the Service exposes only 8000 |

## Tampering

| Control | Status | Where |
|---|---|---|
| Hash-chained audit log; the app role cannot UPDATE/DELETE/TRUNCATE it | OK | `platform/audit.py`; migration 0001, tested in `test_migrations.py` |
| Optimistic locking | Open | `If-Match` is required on user PATCH and disposition. The evidence-request and police-alert actions return an ETag but don't require it. docs/16 §1's general rule and its per-endpoint table disagree |

## Denial of service

| Control | Status | Where |
|---|---|---|
| Global rate limit | Fixed | **Bypass:** any session cookie value got a fresh bucket, so rotating made-up cookies was never limited, including on sign-in. Each sign-in attempt costs a 64 MiB Argon2 hash. Every request now also takes a token from its client address's bucket (100/s, burst 200), and `POST /auth/login` from a per-address sign-in bucket (1/s, burst 10) (`api/rate_limit.py`, docs/16 §1) |
| LLM daily token budget | OK (minor) | `llm/service.py`. Check-then-call isn't atomic, so concurrent calls can overshoot slightly. That's acceptable at the demo's concurrency |
| Bounded worker concurrency | Fixed (docs) | 2 default-lane jobs + 1 fast-lane job; docs/19 said 2 |

## Containers and deployment

| Control | Status | Where |
|---|---|---|
| Non-root image, base images pinned by digest | OK | `Dockerfile` (`USER 10001`) |
| Pod security on every workload | OK | The namespace enforces `restricted`. The api, worker, postgres, backup CronJob, migrate and seed all set runAsNonRoot, readOnlyRootFilesystem, no privilege escalation, drop ALL and RuntimeDefault seccomp (`deploy/k8s/`) |
| No `packages: write`; the deploy needs owner approval | OK | `.github/workflows/deploy.yml` |

## Supply chain and secrets

| Control | Status | Where |
|---|---|---|
| Lockfiles used frozen in CI and the image | OK | `ci.yml`, `nightly.yml`, `Dockerfile` |
| Actions pinned by commit SHA | OK | Every external `uses:` is pinned |
| `pnpm audit` | OK | `ci.yml` (frontend job) |
| gitleaks, pip-audit, bandit, Trivy image scan, dependency review | Open | docs/19 §5 and docs/21 §6 list them, but no workflow runs them |
| Dependabot | Open | There's no `.github/dependabot.yml` |
| No secrets committed; `.env` ignored | OK | Scans for API keys, tokens, private keys and high-entropy strings found only labelled placeholders |
| `.env.example` | Fixed (docs) | None exists. docs/19 now points to docs/23 §4.1, which lists every setting |

## LLM (docs/19 §1, prompt injection)

| Control | Status | Where |
|---|---|---|
| Evidence is data inside delimiters | Fixed | A value holding `</evidence>` could close the tag early. `<` is now written as the JSON escape `<` (`llm/anthropic_llm.py`) |
| No tools; output schema-validated with number and citation checks; payload minimised | OK | `llm/anthropic_llm.py`, `llm/validators.py`, `llm/minimise.py` |

## Open items

1. **CI scanners:** gitleaks, pip-audit, bandit, a Trivy scan of a no-push image build, and
   dependency review, as a `security` job in `ci.yml`. This needs owner approval
   (`.github/workflows`).
2. **Dependabot:** for uv/pip, npm, GitHub Actions and Docker.
3. **`If-Match`:** require it on the evidence-request and police-alert actions, or narrow the
   docs/16 §1 rule. That's a docs/16 decision.
4. **Recheck when built:** four-eyes promotion (#119), the ₹10 crore amount limit (payment
   create), and the `DELPHI_SECRET_KEY` OTP key (#171, #215).
