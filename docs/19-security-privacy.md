# 19 — Security and privacy

The demo is **public on the internet** (ADR-0010), so it is designed as an internet-facing app,
even though all data is synthetic.

## 1. Threat model (STRIDE summary)

Assets: availability of the demo, the Anthropic API key and budget, admin and FDE accounts,
the integrity of KB versions and the audit log, and the home server itself.

| Threat | Example | Mitigation |
|---|---|---|
| Spoofing | Credential stuffing on `/auth/login` | Argon2id hashes; per-(username, IP) lockout; generic errors; no user enumeration; Cloudflare in front |
| Tampering | Editing KB or audit rows | RBAC; optimistic locking; four-eyes promotion; hash-chained audit; the app role cannot UPDATE/DELETE `audit_log` |
| Repudiation | "I didn't approve that lien" | Audit entries carry the user id; DD pack shows approvers |
| Information disclosure | Leaking `ANTHROPIC_API_KEY`, stack traces | Secrets only in k8s Secrets; problem+json without internals; no secrets in logs (redaction filter); `/metrics` not exposed via the tunnel |
| Denial of service | Hammering Guardian or LLM endpoints | Rate limits (doc 16); LLM daily token budget; bounded job concurrency (worker: 2 concurrent jobs); Cloudflare rate limiting rule on `/api/v1/auth/login` |
| Elevation of privilege | VIEWER calling write endpoints; container escape | Permission check in every router dependency (deny by default) with tests per endpoint; containers non-root, read-only root FS, no privilege escalation, all capabilities dropped |
| Prompt injection | Malicious text in evidence reaching the LLM | Evidence is data inside delimiters; the LLM has no tools; its output is validated; the LLM cannot affect decisions (ADR-0004) |
| Malicious image publish | A collaborator's branch workflow pushes a `sha-…` tag that Image Updater deploys | Package linked to the repo with Read only; publishing only via the `main`-restricted `production` environment secret; reviewed PRs to `main` (doc 23 §3) |
| Supply chain | Malicious dependency or action | Lockfiles (uv, pnpm); dependency review; `pip-audit`, `pnpm audit`, Trivy image scan; actions pinned by commit SHA; Dependabot for updates |
| Home network exposure | Inbound ports on the router | Cloudflare Tunnel is outbound-only; no port forwarding; the cluster API is never exposed to GitHub (ADR-0009) |

## 2. Authentication

- Username + password. Argon2id (`argon2-cffi` defaults: time_cost 3, memory 64 MiB,
  parallelism 4). Minimum password length 12. Usernames are stored trimmed and lower-case, and
  matched case-insensitively. An unknown username still costs one Argon2 verification (against
  a dummy hash), so response time does not reveal which usernames exist.
- Session: a random 256-bit token in cookie `delphi_session`
  (`HttpOnly; Secure; SameSite=Strict; Path=/`). Only the SHA-256 of the token is stored
  server-side. Idle timeout 12 h, absolute 7 days. The session id rotates on login.
- Bootstrap: on start, if `users` is empty, create an ADMIN from `DELPHI_BOOTSTRAP_ADMIN_USERNAME`
  and `DELPHI_BOOTSTRAP_ADMIN_PASSWORD`, then log a warning to rotate it. If the table is empty
  and these are unset → fail fast at startup.
- The judges' guest account (VIEWER) is created by the admin. Its password is shared out-of-band.

## 3. Roles and permissions

| Permission | VIEWER | INVESTIGATOR | FDE_ENGINEER | ADMIN |
|---|---|---|---|---|
| `view` (all GETs, SSE, DD packs) | ✔ | ✔ | ✔ | ✔ |
| `demo:drive` (phone, story runner) | — | ✔ | ✔ | ✔ |
| `case:write` (evidence, outreach, disposition, alerts) | — | ✔ | ✔ | ✔ |
| `chase:write` (start chase, approve/release liens) | — | ✔ | ✔ | ✔ |
| `fde` (Scout, drafts, KB promote/rollback, learning, question bank) | — | — | ✔ | ✔ |
| `admin` (users, datasets, training, evaluation runs, demo reset, audit verify) | — | — | — | ✔ |

Four-eyes: a typology promotion needs an approver who has never edited any revision of the
draft. This applies even to ADMIN.

## 4. Web security

- CSRF: SameSite=Strict + a required custom header + an Origin check (doc 16 §1).
- Security headers on every response: `Content-Security-Policy: default-src 'self';
  img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self';
  frame-ancestors 'none'`; `Strict-Transport-Security: max-age=31536000`;
  `X-Content-Type-Options: nosniff`; `Referrer-Policy: same-origin`.
- CORS: none (same origin).
- Output encoding: React escapes by default. `dangerouslySetInnerHTML` is banned (ESLint rule).
  DD pack HTML is rendered server-side with Jinja2 autoescape.
- Input validation: Pydantic models with explicit bounds (amount ≤ ₹10 crore, string lengths,
  enum values). Mass-assignment is prevented by separate request DTOs.

## 5. Secrets

| Secret | Where | Rotation |
|---|---|---|
| DB passwords (`delphi_app`, `delphi_sim`, postgres superuser) | k8s Secret `delphi-db` | Manual (runbook) |
| `ANTHROPIC_API_KEY`, `DELPHI_SECRET_KEY`, bootstrap admin | k8s Secret `delphi-app` | Manual; rotate the API key if it ever appears in a log |
| Cloudflare tunnel token | k8s Secret `cloudflared-token` | Via the Cloudflare dashboard |
| `GHCR_PUSH_TOKEN` (classic PAT, `write:packages` only) | GitHub environment `production` secret, usable only from `main` (doc 23 §3) | On PAT expiry; immediately if leaked. No cluster pull credentials are needed (public package, A-05) |

Secrets are never committed. `gitleaks` runs in CI. `.env` files are git-ignored, and
`.env.example` documents the names.

## 6. Privacy posture

All data is synthetic, but the design follows DPDP-style practice so it carries over to a real
deployment: data minimisation (the planner only fetches what it needs), consent gating
(`CALL_STATUS`), pseudonymised LLM payloads, retention limits (doc 17 §6), and metadata only for
calls (no call content).
