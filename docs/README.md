# DELPHI specification — index

This folder is the **single source of truth** for what DELPHI does and how it is built. The
code implements this spec. If they disagree, the spec wins until a PR changes both.

## Reading order

| # | Document | Purpose |
|---|---|---|
| 01 | [Vision and scope](01-vision-and-scope.md) | Problem, goals, non-goals, personas, demo stories, assumptions |
| 02 | [Glossary](02-glossary.md) | Ubiquitous language. Code identifiers use these terms |
| 03 | [Requirements](03-requirements.md) | Numbered functional (FR) and non-functional (NFR) requirements with acceptance criteria |
| 04 | [Architecture](04-architecture.md) | C4 context and container views, modules, dependency rules, runtime flows |
| 05 | [Domain model](05-domain-model.md) | Entities, aggregates, state machines, invariants |
| 06 | [Decision engine](06-decision-engine.md) | The maths: belief, likelihoods, loss, VoI, lookahead, stopping, exploration |
| 07 | [Knowledge base](07-knowledge-base.md) | KB format, versioning, and the full KB v1 content (hypotheses, probes, evidence, likelihoods, losses, costs) |
| 08 | [Guardian loop](08-guardian.md) | Payment-time probing |
| 09 | [Navigator loop](09-navigator.md) | Investigation: next best evidence, campaigns, due-diligence pack |
| 10 | [Jatayu (Chase) loop](10-chase.md) | Golden-hour tracing and targeted liens |
| 11 | [Typology Scout](11-typology-scout.md) | Discovering unknown patterns; the typology lifecycle |
| 12 | [Learning loop](12-learning-loop.md) | Likelihood updates, exploration, off-policy evaluation, drift |
| 13 | [Sensing layer](13-sensing.md) | Features, behavioural baselines, prior models, mule scorer |
| 14 | [Simulation](14-simulation.md) | Synthetic bank, scenarios, response models, label arrival, simulated external systems |
| 15 | [LLM integration](15-llm.md) | Claude usage, prompts, validation, fallback |
| 16 | [API](16-api.md) | REST + SSE contract |
| 17 | [Data model](17-data-model.md) | PostgreSQL schema, migrations, retention |
| 18 | [Frontend](18-frontend.md) | Screens, components, states |
| 19 | [Security and privacy](19-security-privacy.md) | Threat model, auth, RBAC, secrets |
| 20 | [Observability](20-observability.md) | Logs, metrics, health, alerts |
| 21 | [Testing](21-testing.md) | Test portfolio, golden tests, quality gates |
| 22 | [Evaluation](22-evaluation.md) | Baselines, metrics, experiment protocol |
| 23 | [Deployment and operations](23-deployment.md) | CI/CD, k3s, Argo CD, Cloudflare Tunnel, runbooks |
| 24 | [Engineering conventions](24-engineering-conventions.md) | Repo layout, tooling, coding standards, git workflow |
| 25 | [Milestones](25-milestones.md) | M0–M9 with deliverables and exit criteria |
| — | [ADRs](adr/) | Architecture Decision Records |
| — | [Reference](reference/) | Source research and engineering field guide |

## Conventions used in this spec

- **MUST / MUST NOT / SHOULD / MAY** follow RFC 2119. "MUST" is a hard requirement checked by a
  test or review. "SHOULD" can be broken only with a written reason in the PR.
- **IDs.** Requirements are `FR-<AREA>-<NN>` and `NFR-<NN>`. Decisions are `ADR-<NNNN>`.
  Assumptions are `A-<NN>` (in 01). Open risks are `R-<NN>` (in 25).
- **Code-facing names** (hypothesis keys, probe keys, enum values, table names) are written in
  `monospace` and MUST be used verbatim in code.
- **Money** is always in Indian rupees (₹). It is stored as integer **paise**. ₹1 = 100 paise.
- **Time** is stored in UTC and displayed in IST (`Asia/Kolkata`).
- **Research references** look like "research §9.3". They point to
  `reference/research-behavioral-fraud-intelligence.md`.
- **Engineering guide references** look like "EFG §17". They point to
  `reference/engineering-field-guide.md`.

## Decision summary (the short version)

| Topic | Decision | ADR |
|---|---|---|
| Shape | Modular monolith, Python 3.12 + FastAPI, one container image, `api` and `worker` processes | 0002 |
| Storage | PostgreSQL 16 only: data, job queue, model artifacts, audit log. No Redis or Kafka | 0003 |
| Decisions | Pure Bayesian engine (functional core). The LLM never decides | 0004 |
| Probe selection | Depth-limited lookahead value of information, run after a passive-first phase | 0005 |
| Detection-as-code | The knowledge base is an immutable, versioned bundle. KB v1 is seeded from YAML in git | 0006 |
| LLM | Claude API (`claude-opus-5-5`) behind a provider port, with a deterministic template fallback | 0007 |
| UI | React + TypeScript SPA (Vite), served by the API process | 0008 |
| Deploy | GitHub Actions → GHCR → Argo CD + Image Updater on k3s. No bot commits | 0009 |
| Access | Cloudflare Tunnel to a public hostname, protected by app login | 0010 |
| Data | Synthetic-first simulator. External datasets are an optional stretch | 0011 |
| Chase maths | Proportional taint tracing + exponential dwell-time survival | 0012 |
| Live UI updates | Server-Sent Events | 0013 |
| Units | Integer paise, UTC timestamps, float64 probabilities | 0014 |
