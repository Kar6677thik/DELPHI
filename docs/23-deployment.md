# 23 — Deployment and operations

ADR-0015 (manual, approved deploys; shared host tunnel), which supersedes ADR-0009, and
ADR-0010 (public access through Cloudflare Tunnel, protected by the app login).

**Rules:**
- A deploy happens only when the owner runs **Deploy** (`workflow_dispatch`) and approves it.
  Merging to `main` never deploys.
- CI holds a cluster credential only for namespace `delphi`, and can only change images and run
  Jobs there.
- This repo is public. It never contains the server's addresses, tailnet names or tags, ports,
  account IDs or secret values. Those live in GitHub environment variables and secrets, or in
  the operator's local bootstrap overlay (§4).

```
owner: Actions → Deploy → Run workflow (on main) [seed: no]
   │
   ▼
guard (ref is main) ─► ci (full CI on that commit) ─► publish
   publish: build the image → push ghcr.io/kar6677thik/delphi:sha-<40-hex> → attest build provenance
   │                                                     (environment `production`, main only)
   ▼
deploy  (environment `megatron`: waits for the owner's approval; main only)
   join the tailnet (Tailscale OIDC, ephemeral CI tag; ACL: the Kubernetes API only)
   verify the attestation for the digest
   kubectl as ServiceAccount delphi-deployer (namespace `delphi` only):
     Job delphi-migrate-<sha7> (image@digest) ─ wait ─► set image api + worker (image@digest)
     ─► rollout status ─(fail)─► rollout undo
     [seed: yes] Job delphi-seed-<sha7> ─ wait
   │
   ▼
verify: poll https://delphi.karthiksurkanti.in/api/v1/health until .version == the commit SHA
```

## 1. Repository layout for deployment

```
Dockerfile                         # multi-stage: frontend build → python runtime
compose.yaml                       # local development (PostgreSQL; api/worker later)
.github/workflows/ci.yml           # PR CI, and reused by deploy.yml (workflow_call)
.github/workflows/deploy.yml       # workflow_dispatch only: guard → ci → publish → deploy → verify
deploy/k8s/base/                   # what the bootstrap applies (namespace delphi)
  namespace.yaml  config.yaml  postgres.yaml  api.yaml  worker.yaml  backup.yaml
  networkpolicies.yaml  deployer-rbac.yaml  kustomization.yaml
deploy/k8s/jobs/                   # templates the deploy workflow fills in with the digest
  migrate.yaml  seed.yaml
deploy/k8s/overlays/example/       # the overlay the operator copies and fills in locally (§4)
deploy/README.md                   # the bootstrap runbook (§6)
```

## 2. Container image

- Stage 1 `node:22-alpine` (pinned digest): `pnpm install --frozen-lockfile`, `pnpm build` →
  `frontend/dist`.
- Stage 2 `python:3.12-slim` (pinned digest): install `libgomp1` (LightGBM), copy `uv`,
  `uv sync --frozen --no-dev`, copy `backend/src`, `backend/migrations`, `kb/` and
  `frontend/dist` → `/app/static`.
- Runs as user `10001:10001`, `WORKDIR /app`, `ENTRYPOINT ["delphi"]`, default `CMD ["api"]`.
- Build arg and env `DELPHI_VERSION=<git sha>` (returned by `/health`). OCI labels: source,
  revision, created.
- Platform: `linux/amd64` (A-02).
- The image must start with a read-only root filesystem. Writable `/tmp` is an `emptyDir`.
- Deployed **by digest** (`ghcr.io/kar6677thik/delphi@sha256:…`), never by tag. The
  `sha-<40-hex>` tag only names the build.

## 3. Workflows

### `ci.yml` (on: pull_request, workflow_call)

1. `backend`: uv sync → ruff → mypy → import-linter → `delphi kb validate kb/` → pytest (unit,
   property, golden, integration with a `postgres:16` service) → coverage gate.
2. `frontend`: pnpm install → eslint → tsc → prettier check → vitest (coverage) →
   `pnpm audit --prod` → build.
3. `contract`: the generated TS types match `backend/openapi.json` (the backend checks the file
   against the code).
4. Later in M0: `secrets` (gitleaks), `image-scan` (Trivy on a no-push build), and `e2e`
   (compose + Playwright).

`policy.yml` (`commit-policy`) checks every PR's commits and description for AI attribution.
All third-party actions are pinned to a full commit SHA. `permissions: contents: read` by
default.

### `deploy.yml` (on: workflow_dispatch only)

Inputs:
- `seed` (boolean, default false): also run `delphi seed --if-empty --profile demo` after the
  rollout. Needed once, on the first deploy.
- `digest` (string, optional): redeploy an image that was already built (`sha256:` + 64 hex,
  validated). This skips `ci` and `publish`, which makes a rollback to a known-good digest a
  one-click run.

```yaml
concurrency: { group: deploy-megatron, cancel-in-progress: false }
jobs:
  guard:    fail unless github.ref == refs/heads/main; validate `digest` if given
  ci:       uses ./.github/workflows/ci.yml                   (skipped when `digest` is given)
  publish:  needs ci; environment: production
            permissions: { contents: read, id-token: write, attestations: write }
            login ghcr with secrets.GHCR_PUSH_TOKEN → build-push tag sha-<sha>
            → actions/attest-build-provenance (subject-digest)  → outputs: digest
  deploy:   needs publish (or guard); environment: megatron (owner approval; main only)
            permissions: { contents: read, id-token: write }
            tailscale/github-action (OIDC; vars.TS_OIDC_CLIENT_ID, vars.TS_OIDC_AUDIENCE; tag from vars)
            gh attestation verify oci://ghcr.io/kar6677thik/delphi@<digest> --owner Kar6677thik
            kubeconfig from vars.KUBE_SERVER, secrets.KUBE_CA, secrets.KUBE_DEPLOYER_TOKEN
              (tls-server-name from vars.KUBE_TLS_SERVER_NAME)
            migrate Job → wait (timeout 10 min; on failure print its logs and stop)
            kubectl set image deploy/delphi-api deploy/delphi-worker → rollout status (10 min)
              → on failure: rollout undo both, fail the run
            if seed: seed Job → wait (5 min; it only enqueues GENERATE_DATASET)
  verify:   needs deploy; poll https://delphi.karthiksurkanti.in/api/v1/health every 15 s
            until .version == the commit SHA (10 min)
```

- No step creates commits, tags or releases in the repo.
- A rejected or expired approval ends the run with nothing changed on the server.
- The ephemeral tailnet node disappears when the job ends.

### Secrets and variables (GitHub; never in the repo)

| Name | Kind | Environment | What |
|---|---|---|---|
| `GHCR_PUSH_TOKEN` | secret | `production` | Classic PAT, `write:packages` only, with an expiry |
| `TS_OIDC_CLIENT_ID`, `TS_OIDC_AUDIENCE` | variables | `megatron` | Tailscale OIDC trust credential bound to this repo and environment |
| `TS_CI_TAG` | variable | `megatron` | The ephemeral CI tag the runner joins as |
| `KUBE_SERVER`, `KUBE_TLS_SERVER_NAME` | variables | `megatron` | The API server address on the tailnet, and the name its certificate carries |
| `KUBE_CA`, `KUBE_DEPLOYER_TOKEN` | secrets | `megatron` | Cluster CA, and the `delphi-deployer` ServiceAccount token |

### Image publishing trust boundary

1. **The package is linked to the repo with Read access only.** No repo workflow's
   `GITHUB_TOKEN` can push. Workflows never request `packages: write`.
2. **Only the `production` environment can publish.** It allows only `main`, and its secret
   `GHCR_PUSH_TOKEN` is a classic PAT of the owner with only `write:packages` (classic PATs are
   required for GHCR pushes; set an expiry and a reminder).
3. **Nothing deploys a tag.** The deploy job rolls out the digest that the same run built (or
   that the owner typed in), after verifying its provenance attestation. So an image pushed by
   anything else is never deployed.
4. **`main` changes only through reviewed PRs**, so changing `deploy.yml` itself needs the
   owner's approval.

## 4. Kubernetes (namespace `delphi`)

The bootstrap (§6) applies `deploy/k8s/base` through the operator's local overlay. The overlay
fills in what must not be public: the api Service's fixed ClusterIP (the tunnel's target), the
node address range allowed to reach it, and the optional monitoring scrape rule.
`deploy/k8s/overlays/example` shows its shape with placeholders.

**Namespace:** labelled `pod-security.kubernetes.io/enforce: restricted`. Every pod:
- runs non-root, with a read-only root filesystem and an `emptyDir` `/tmp`;
- `allowPrivilegeEscalation: false`, capabilities `drop: [ALL]`, `seccompProfile: RuntimeDefault`;
- `automountServiceAccountToken: false`;
- digest-pinned images, and requests and limits on every container, including Jobs.

**Budget** (the node is shared and small; ADR-0015):

| Workload | Requests | Limits | Notes |
|---|---|---|---|
| `Deployment delphi-api` | 100m / 384Mi | 1 CPU / 768Mi | 1 replica, RollingUpdate maxSurge 1 / maxUnavailable 0; ports 8000 (app) and 9000 (metrics) |
| `Deployment delphi-worker` | 100m / 768Mi | 1.5 CPU / 1.5Gi | 1 replica, `Recreate`; `terminationGracePeriodSeconds: 60`; liveness on `:9000/health` |
| `StatefulSet delphi-postgres` | 100m / 384Mi | 1 CPU / 1Gi | `postgres:16` by digest; PVC 10Gi `local-path`; liveness `pg_isready` |
| Jobs (migrate, seed, backup) | 50m / 256Mi | 1 CPU / 768Mi | `backoffLimit: 1`; `ttlSecondsAfterFinished: 86400` |

- **`eval` is off on this server.** The `eval` dataset peaks at about 3.8 GiB (doc 22 §1), more
  than the node can spare. `DELPHI_DATASET_PROFILES=test,demo` makes the api and the worker
  refuse it, and evaluation runs elsewhere (a laptop or CI).
- **Single node.** One replica each; Postgres in a StatefulSet; startup probes that tolerate a
  cold start after the node reboots (`/api/v1/health`: period 5 s, failureThreshold 60).

**Configuration and secrets:**
- Non-secret settings: ConfigMap `delphi-config`:
  - `DELPHI_ENV=demo`, `DELPHI_PUBLIC_ORIGIN=https://delphi.karthiksurkanti.in`
  - `DELPHI_CLIENT_IP_HEADER=CF-Connecting-IP`, `DELPHI_DATASET_PROFILES=test,demo`
  - `DELPHI_LOG_LEVEL=INFO`, `DELPHI_MIGRATIONS_DIR=/app/migrations`, `DELPHI_STATIC_DIR=/app/static`
- Secrets are created by the bootstrap from values generated on the operator's machine, and
  mounted with `envFrom: secretRef`. Manifests never name keys and never inline a value.
  - **Secret `delphi-app`**, for the api and Jobs: the app's database URL and the bootstrap
    admin. Later also the LLM key.
  - **Secret `delphi-worker`**, for the worker only: the app and sim database URLs.
  - **Secret `delphi-migrate`**, for the migrate Job: the owner database URL.
  - **Secret `delphi-postgres`**, for Postgres and the backup: the superuser and role passwords.
- **The api never receives the sim URL** (doc 17 §2).
- Postgres creates the `delphi_app` and `delphi_sim` LOGIN roles on first start, from an init
  script in ConfigMap `delphi-postgres-init`, which reads the passwords from its environment.

**Services:**
- **`delphi-api`:** ClusterIP 8000, with the fixed address set in the overlay. It is the tunnel's
  target. Port 9000 is not in the Service.
- **`delphi-postgres`:** headless, 5432.

**NetworkPolicies:** default-deny ingress and egress for the namespace, then:
- **into `delphi-api:8000`:** from the node only (the host tunnel), with the address range set in
  the overlay;
- **into `delphi-postgres:5432`:** from pods labelled `delphi.io/db-client: "true"`, which are
  the api, the worker, the Jobs and the backup;
- **egress, all pods:** DNS to kube-dns;
- **egress from db clients:** to Postgres;
- **egress, api and worker:** HTTPS (443) to public addresses only, that is everything except
  the RFC 1918 and 100.64/10 ranges. The LLM provider needs this from M7;
- **optional:** into `:9000` from the operator's metrics scraper, set in the overlay.

**RBAC for CI (`deployer-rbac.yaml`):** ServiceAccount `delphi-deployer`, its token Secret, and
a Role bound to it in `delphi`:
- `apps/deployments`: get, list, watch, patch;
- `apps/replicasets`: get, list, watch (`rollout status` and `undo`);
- `batch/jobs`: create, get, list, watch, delete;
- `pods`: get, list, watch; `pods/log`: get.

Nothing else: no Secrets, ConfigMaps, Services, NetworkPolicies, RBAC, namespaces or cluster
scope.

**Jobs (`deploy/k8s/jobs/`), filled in by the deploy workflow with the digest:**
- **`delphi-migrate-<sha7>`** runs `delphi migrate`: `alembic upgrade head`, then **load KB v1**
  from the image's `kb/` if no KB version exists (idempotent, from #15). So `/ready` can pass on
  the very first deploy.
- **`delphi-seed-<sha7>`** runs `delphi seed --if-empty --profile demo`. It is idempotent: the
  job key names the profile and seed, and `--if-empty` does nothing once a dataset exists. It
  enqueues `GENERATE_DATASET` (and `TRAIN_SENSING` once sensing exists, M3) and exits; the
  worker does the work.

**First deploy order:**
1. migrate (schema + KB v1);
2. api and worker become Ready (readiness needs no data or models);
3. seed enqueues the jobs;
4. the worker generates and trains (≈ 10–20 min);
5. `/status` flips to ready.

### 4.1 Process settings

Every process reads `DELPHI_*` environment variables (`delphi.platform.settings`) and refuses
to start, exit code 2, naming the variable, if one is missing or invalid.

| Variable | Default | Used by | Source |
|---|---|---|---|
| `DELPHI_DATABASE_URL` | required | api, worker, migrate | Secrets `delphi-app` / `delphi-worker` / `delphi-migrate` (role `delphi_app`) |
| `DELPHI_SIM_DATABASE_URL` | unset | worker only | Secret `delphi-worker` (role `delphi_sim`). **Never mounted into the api pod**, so the api cannot read `sim_truth` even by mistake (doc 17 §2) |
| `DELPHI_MIGRATE_DATABASE_URL` | `DELPHI_DATABASE_URL` | migrate Job only | Secret `delphi-migrate` (the schema owner) |
| `DELPHI_MIGRATIONS_DIR` | `backend/migrations` in a checkout | migrate, api, worker | The image sets `/app/migrations` |
| `DELPHI_STATIC_DIR` | `frontend/dist` in a checkout | api | The image sets `/app/static`. Without an `index.html` there, the api serves no SPA and `/` is a 404 problem |
| `DELPHI_VERSION` | `dev` | all | Image build arg (§2) |
| `DELPHI_ENV` | `dev` | all | ConfigMap (`dev`, `test` or `demo`) |
| `DELPHI_LOG_LEVEL` | `INFO` | all | ConfigMap |
| `DELPHI_HTTP_HOST` / `DELPHI_HTTP_PORT` | `0.0.0.0` / `8000` | api | — |
| `DELPHI_METRICS_PORT` | `9000` | api, worker | — |
| `DELPHI_DB_POOL_SIZE` | `5` | api, worker | — |
| `DELPHI_DB_CONNECT_TIMEOUT_S` | `5` | api, worker | — |
| `DELPHI_READY_DB_TIMEOUT_MS` | `500` | api | doc 20 §3 |
| `DELPHI_JOB_LEASE_S` / `DELPHI_JOB_RETRY_BACKOFF_S` / `DELPHI_JOB_MAX_ATTEMPTS` | `60` / `30` / `3` | worker | doc 04 §5.4 |
| `DELPHI_WORKER_DEFAULT_CONCURRENCY` / `DELPHI_WORKER_FAST_CONCURRENCY` | `2` / `1` | worker | doc 04 §5.4 |
| `DELPHI_WORKER_POLL_INTERVAL_S` / `DELPHI_WORKER_HEARTBEAT_MAX_AGE_S` | `1.0` / `30` | worker | doc 20 §3 |
| `DELPHI_OUTBOX_RETENTION_H` / `DELPHI_IDEMPOTENCY_RETENTION_H` | `24` / `24` | worker | doc 17 §6 |
| `DELPHI_PUBLIC_ORIGIN` | unset | api | ConfigMap: `https://delphi.karthiksurkanti.in`. The Origin that mutating requests must carry (CSRF, doc 19 §4). Unset in development: the request's own origin |
| `DELPHI_CLIENT_IP_HEADER` | unset | api | ConfigMap: `CF-Connecting-IP`. The header with the real client IP behind the tunnel, used for the login lockout and audit. Safe because only the node (the host tunnel) can reach the api pod (§4 NetworkPolicies) |
| `DELPHI_SESSION_IDLE_H` / `DELPHI_SESSION_ABSOLUTE_DAYS` | `12` / `7` | api | doc 19 §2 |
| `DELPHI_LOGIN_MAX_FAILURES` / `DELPHI_LOGIN_WINDOW_MIN` | `5` / `15` | api | doc 16 §1 |
| `DELPHI_DATASET_PROFILES` | `test,demo,eval` | api, worker | ConfigMap: `test,demo` on the home server (the `eval` dataset needs about 3.8 GiB; §4). `POST /admin/datasets`, `delphi seed` and `GENERATE_DATASET` refuse other profiles |
| `DELPHI_BOOTSTRAP_ADMIN_USERNAME` / `DELPHI_BOOTSTRAP_ADMIN_PASSWORD` | unset | api | Secret `delphi-app`. Required only while `users` is empty: the api then creates this ADMIN, or exits with code 2 if they are unset (doc 19 §2) |

Later milestones add their settings to this table (`DELPHI_LLM_*`, `DELPHI_TIME_MODE`) together
with the code that reads them.

## 5. Public exposure

- `https://delphi.karthiksurkanti.in` is a proxied Cloudflare DNS name for the server's
  **shared, host-level Cloudflare Tunnel** (ADR-0010 as amended by ADR-0015). TLS ends at
  Cloudflare. The tunnel forwards plain HTTP on the node to the api Service's fixed ClusterIP,
  port 8000.
- Only the api's app port is routed. Metrics (`:9000`), Postgres and the worker are never
  routed.
- The server's operator adds the ingress rule and the DNS route (§6). There is no tunnel token
  in the namespace and no `cloudflared` in the cluster.
- Every page and API except `/api/v1/health` and `/api/v1/ready` needs a sign-in (doc 19).
  Optional, in the Cloudflare dashboard: a rate-limiting rule on `/api/v1/auth/login` (10
  requests per minute per IP).

## 6. Bootstrap runbook (once, and whenever `deploy/k8s/base` changes)

The server's operator (the owner, or the operations thread acting for the owner) runs this with
admin rights. Exact commands, addresses and names stay in the operator's private notes, not in
this repo.

**GitHub (owner):**
1. Create environment `megatron` with required reviewer = owner and deployment branches = `main`.
2. Fill in the §3 variables and secrets. The ServiceAccount token comes from step 8 below.
3. Package settings: link the package to the repo with **Read** access, and make it public (A-05).

**Tailscale (owner, admin console):**

4. Create the CI tag, owned by the owner, and an ACL grant that lets it reach only the cluster's
   Kubernetes API.
5. Create the OIDC trust credential for this repo and environment `megatron`, limited to that tag.

**Cluster (operator):**

6. Generate the secret values locally (e.g. `openssl rand -hex 32`) and create the four
   Secrets (§4).
7. Copy `deploy/k8s/overlays/example` to a private location and fill in:
   - a free fixed ClusterIP for `delphi-api`;
   - the node address range;
   - optionally, the scrape rule.
8. `kubectl apply -k <private overlay>`, then read the `delphi-deployer` token and the cluster
   CA into the GitHub secrets.
9. Add the tunnel ingress rule `delphi.karthiksurkanti.in → http://<api ClusterIP>:8000`,
   validate it, restart the tunnel service, and route the DNS name.
10. Add `delphi` to the operations console's namespace allowlist, after it checks for inline
    secrets.

**First release (owner):**

11. Actions → Deploy → Run workflow on `main` with **seed = yes**, and approve the `megatron`
    gate.
12. Sign in with the bootstrap admin, rotate the password, and create the other users (an FDE
    engineer, an investigator, a guest viewer).

## 7. Credential rotation

| Credential | Rotate | How |
|---|---|---|
| `delphi-deployer` token | every 90 days, and at once if leaked | Delete its token Secret, re-apply the bootstrap, and update `KUBE_DEPLOYER_TOKEN` |
| `GHCR_PUSH_TOKEN` | on PAT expiry | New classic PAT, `write:packages` only |
| Tailscale OIDC credential | if the repo or environment changes | Admin console |
| Database and app secrets | if leaked | Recreate the Secret, then restart the pods with `kubectl rollout restart` as the operator |

## 8. Backups and restore

- `CronJob delphi-pg-backup` runs `30 21 * * *` UTC (= 03:00 IST). It writes `pg_dump -Fc` to
  PVC `delphi-backups` (5Gi, `local-path`), keeps the 7 most recent, and inserts a row into
  `delphi.ops_backup_runs`.
- **The backups live on the same disk as the database**, so they protect against mistakes, not
  against losing the disk. An off-host copy is the owner's decision (not in scope).
- **Restore drill** (M9, then monthly), to prove backups work (EFG §34):
  1. As the operator, apply `deploy/k8s/ops/restore-test-job.yaml`. The Job restores the latest
     dump into a temporary database `delphi_restore_test`.
  2. A second container runs `verify-restore`, which checks table counts, that exactly one
     ACTIVE KB exists, and that the audit chain verifies. Then it drops the database.
  3. Record the result in `docs/reports/restore-drill-<date>.md`.
- **Disaster recovery:** re-run the bootstrap (§6) on a new node, then restore the latest dump
  instead of seeding. `delphi seed --if-empty` skips a non-empty database.

## 9. Runbooks (short form)

| Symptom | Steps |
|---|---|
| Deploy run fails at migrate | The run prints the Job's logs. Nothing was rolled out. Fix forward with a new PR, or redeploy the previous digest |
| Rollout timed out | The run already undid it. Check pod events (image pull, probes, OOM) as the operator |
| Bad release that passed | Actions → Deploy with `digest` = the previous good digest (from the last successful run's summary). Migrations are expand/contract, so rolling the code back is safe |
| verify timed out, pods healthy | The tunnel or DNS: ask the operator to check the host tunnel service and its rule for the name |
| Server rebooted | Pods restart on their own. Startup probes allow a cold start; the worker reclaims expired job leases |
| Jobs stuck | Admin → Jobs; jobs whose `locked_until` has passed are reclaimed automatically; retry failed ones |
| LLM failing | Overview shows the fallback rate; check `llm_calls`; set `DELPHI_LLM_PROVIDER=template` in `delphi-config` (operator) and restart the api |
| Disk filling | Prune old backups; run `RETENTION_SWEEP`; check the node's free disk with the operator |

## 10. Local development

`compose.yaml` services: `postgres`, `api` (hot reload, `uv run delphi api --reload`),
`worker`, `frontend` (Vite dev server with a proxy to `api`). `just dev` starts them;
`just seed-test` loads the `test` profile. The developer's LLM provider defaults to `template`.
