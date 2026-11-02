# 23 — Deployment and operations

ADR-0009 (GitOps on k3s) and ADR-0010 (Cloudflare Tunnel).

**Rule:** GitHub Actions builds, tests and publishes the image. It never holds cluster
credentials and never commits to the repo. The cluster pulls everything it needs (Argo CD pulls
manifests from git, Image Updater watches GHCR, kubelet pulls images, cloudflared dials out).

```
push to main / workflow_dispatch
   │
   ▼
GitHub Actions: ci (lint, test, e2e) ─► build image ─► push ghcr.io/kar6677thik/delphi:sha-<40-hex> (+ :main)
                                                          │
                     home server (k3s)                    │ polls registry (2 min)
   Argo CD Image Updater ◄────────────────────────────────┘
        │ sets the image override on Application "delphi" (write-back method: argocd; no git commit)
        ▼
   Argo CD sync: PreSync Job delphi-migrate → Deployments roll (api, worker) → PostSync Job delphi-seed (idempotent)
        ▼
   cloudflared ─► https://delphi.<domain>
   │
GitHub Actions verify job: polls https://delphi.<domain>/api/v1/health until version == commit SHA (≤ 15 min)
```

## 1. Repository layout for deployment

```
.github/workflows/ci.yml            # reusable + PR/push CI
.github/workflows/deploy.yml        # main + workflow_dispatch: ci → build/push → verify
Dockerfile                          # multi-stage: frontend build → python runtime
compose.yaml                        # local dev + CI e2e stack
deploy/k8s/base/                    # kustomize base (namespace delphi)
  namespace.yaml  api.yaml  worker.yaml  postgres.yaml  migrate-job.yaml  seed-job.yaml
  backup-cronjob.yaml  cloudflared.yaml  networkpolicies.yaml  kustomization.yaml
deploy/k8s/overlays/home/           # replicas, resources, public host, images: newName/newTag
deploy/argocd/application.yaml      # Argo CD Application "delphi" (applied once, manually)
deploy/argocd/image-updater.yaml    # ImageUpdater CR (applied once, manually)
deploy/bootstrap/README.md          # the runbook in §6, copied as executable steps
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
- Platforms: `${{ vars.IMAGE_PLATFORMS || 'linux/amd64' }}` (A-02).
- The image must start with a read-only root filesystem. Writable `/tmp` is an `emptyDir`.

## 3. Workflows

### `ci.yml` (on: pull_request, push to non-main branches, workflow_call)

Jobs run in parallel where possible:

1. `backend`: uv sync → ruff → mypy → import-linter → `delphi kb validate kb/` → pytest
   (unit, property, golden, integration with `postgres:16` service) → coverage gate →
   pip-audit → bandit.
2. `frontend`: pnpm install → eslint → tsc → prettier check → vitest (coverage) → pnpm audit →
   build.
3. `contract`: regenerate `openapi.json` and the TS types; `git diff --exit-code`.
4. `secrets`: gitleaks.
5. `e2e` (only for PRs targeting `main` and for `workflow_call` from deploy): `docker compose up`
   with the `test` profile and the template LLM → Playwright → upload the report as an artifact.
6. `image-scan`: docker build (no push) → Trivy (fail on HIGH/CRITICAL without a waiver).

All third-party actions are pinned to a full commit SHA. `permissions: contents: read` by
default.

### `deploy.yml` (on: push to `main`, workflow_dispatch)

```yaml
concurrency: { group: deploy-main, cancel-in-progress: false }
jobs:
  guard:       # workflow_dispatch is allowed only on refs/heads/main
  ci:          uses: ./.github/workflows/ci.yml
  publish:     needs ci; permissions: { contents: read, packages: write }
               docker/setup-buildx → login ghcr with GITHUB_TOKEN → build-push
               tags: ghcr.io/kar6677thik/delphi:sha-${{ github.sha }}, ghcr.io/kar6677thik/delphi:main
               cache: type=gha
  verify:      needs publish; runs-on ubuntu-latest; timeout-minutes 20
               poll ${{ vars.DELPHI_PUBLIC_URL }}/api/v1/health every 20 s until .version == github.sha
               fail after 15 min with a message linking to the runbook §8
```

- No step creates commits, tags or releases in the repo.
- Re-running `workflow_dispatch` on the same SHA rebuilds and pushes the same tag. The cluster
  is unaffected, because the image is identical, and verify passes at once.
- GitHub repo settings: branch protection on `main` (required checks: backend, frontend,
  contract, secrets, e2e, image-scan; linear history; squash merge only). Repo variable
  `DELPHI_PUBLIC_URL` (e.g. `https://delphi.example.com`) and optional `IMAGE_PLATFORMS`.
  No repo secrets are needed.

## 4. Kubernetes manifests (kustomize base, namespace `delphi`)

| Resource | Key settings |
|---|---|
| `Deployment delphi-api` | 1 replica; RollingUpdate maxSurge 1 / maxUnavailable 0; `imagePullSecrets: ghcr-pull`; env from Secret `delphi-app` + ConfigMap `delphi-config`; ports 8000 and 9000; startupProbe `/api/v1/health` (failureThreshold 30, period 2 s); livenessProbe `/api/v1/health` (period 10 s); readinessProbe `/api/v1/ready` (period 5 s); requests 250m/512Mi, limits 2 CPU/1.5Gi; securityContext runAsNonRoot, readOnlyRootFilesystem, allowPrivilegeEscalation false, capabilities drop ALL, seccomp RuntimeDefault |
| `Deployment delphi-worker` | 1 replica; same image, `args: ["worker"]`; requests 500m/1Gi, limits 3 CPU/3Gi; liveness on worker `:9000/health`; `terminationGracePeriodSeconds: 60` (finish or release the current job) |
| `StatefulSet delphi-postgres` | `postgres:16` pinned; PVC 10Gi `local-path`; requests 250m/512Mi; init script creates the `delphi_app` / `delphi_sim` roles from Secret `delphi-db`; liveness `pg_isready` |
| `Service delphi-api` (ClusterIP 8000), `delphi-postgres` (headless 5432) | |
| `Job delphi-migrate` | annotation `argocd.argoproj.io/hook: PreSync`, `hook-delete-policy: BeforeHookCreation`; `args: ["migrate"]`; backoffLimit 2. `delphi migrate` = `alembic upgrade head`, then **load KB v1** from the image's `kb/` if no KB version exists (idempotent). So `/ready` can pass on the very first sync |
| `Job delphi-seed` | `argocd.argoproj.io/hook: PostSync`; `args: ["seed", "--if-empty", "--profile", "demo"]`; idempotent; it enqueues `GENERATE_DATASET` + `TRAIN_SENSING` jobs and exits; the worker does the work. Ordering on the first deploy: PreSync migrate (schema + KB v1) → api/worker become Ready (readiness does not need data or models) → PostSync seed enqueues jobs → the worker generates and trains (≈ 10–20 min) → `/status` flips to ready |
| `CronJob delphi-pg-backup` | `30 21 * * *` UTC (= 03:00 IST); `pg_dump -Fc` to PVC `delphi-backups` (5Gi); keep 7 most recent; insert a row into `delphi.ops_backup_runs` |
| `Deployment cloudflared` | `cloudflare/cloudflared` pinned; `tunnel --no-autoupdate run --token $(TUNNEL_TOKEN)` from Secret `cloudflared-token`; 1 replica; metrics port for liveness |
| `NetworkPolicy` | **ingress only** (egress is not restricted: plain NetworkPolicy cannot filter by FQDN): default-deny ingress for the namespace; allow pods labelled `app=cloudflared` → `delphi-api:8000`; allow pods labelled `delphi.io/db-client: "true"` → `delphi-postgres:5432`. The api, worker, migrate, seed, backup and restore-test pods carry that label. Port 9000 (metrics) gets no ingress rule from outside the namespace, except an optional rule for the owner's Prometheus namespace |
| `ConfigMap delphi-config` | `DELPHI_ENV=demo`, `DELPHI_PUBLIC_ORIGIN`, `DELPHI_LLM_PROVIDER=anthropic`, `DELPHI_LLM_MODEL=claude-opus-5-5`, `DELPHI_TIME_MODE=DEMO`, `DELPHI_LOG_LEVEL=INFO` |

Overlay `home` sets the image `ghcr.io/kar6677thik/delphi` with the initial `newTag:
sha-<first deployed sha>` (Image Updater overrides it afterwards), and the public host.

## 5. Argo CD and Image Updater

`deploy/argocd/application.yaml`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata: { name: delphi, namespace: argocd }
spec:
  project: default
  source:
    repoURL: https://github.com/Kar6677thik/DELPHI.git
    targetRevision: main
    path: deploy/k8s/overlays/home
  destination: { server: https://kubernetes.default.svc, namespace: delphi }
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions: [CreateNamespace=true]
    retry: { limit: 3, backoff: { duration: 30s, factor: 2, maxDuration: 5m } }
```

`deploy/argocd/image-updater.yaml` (CRD form, Image Updater v1.x. Field names MUST be checked
against the docs of the pinned version during M0, and this file corrected if needed):

```yaml
apiVersion: argocd-image-updater.argoproj.io/v1alpha1
kind: ImageUpdater
metadata: { name: delphi, namespace: argocd }
spec:
  writeBackConfig: { method: argocd }          # no git write-back → no bot commits
  applicationRefs:
    - namePattern: delphi
      images:
        - alias: delphi
          imageName: ghcr.io/kar6677thik/delphi
          commonUpdateSettings:
            updateStrategy: newest-build
            allowTags: "regexp:^sha-[0-9a-f]{40}$"
```

Notes:

- `newest-build` relies on image creation time. We never rebuild an *older* SHA (deploy is
  guarded to `main` HEAD), so the newest build is always the newest commit.
- The `argocd` write-back stores the override in the Application. If the Application is
  recreated, Image Updater re-applies the newest tag within one polling interval. Until then,
  the overlay's pinned tag runs. This is acceptable.
- Image Updater needs GHCR read credentials (Secret `ghcr-creds` in `argocd`, referenced per
  the pinned version's registry config).
- If the repo is private, Argo CD needs a read-only deploy key (`argocd repo add`).

## 6. One-time bootstrap runbook (owner, on the server)

Prerequisites: k3s running; `kubectl` access; a Cloudflare account with the domain; a GitHub
fine-grained PAT with `read:packages` (for pulls).

1. Install Argo CD (pinned version) into namespace `argocd` (upstream install manifest).
2. Install Argo CD Image Updater (pinned v1.x) into `argocd`. Create the GHCR credentials
   secret.
3. If the repo is private: add the repo to Argo CD with a read-only deploy key.
4. Create namespace `delphi` and the secrets (values generated locally, e.g. `openssl rand -hex 32`):
   - `delphi-db`: `POSTGRES_PASSWORD`, `DELPHI_APP_DB_PASSWORD`, `DELPHI_SIM_DB_PASSWORD`
   - `delphi-app`: `DELPHI_SECRET_KEY`, `ANTHROPIC_API_KEY`, `DELPHI_BOOTSTRAP_ADMIN_USERNAME`,
     `DELPHI_BOOTSTRAP_ADMIN_PASSWORD`
   - `ghcr-pull`: `kubectl create secret docker-registry ghcr-pull --docker-server=ghcr.io …`
   - `cloudflared-token`: `TUNNEL_TOKEN`
5. In Cloudflare Zero Trust: create a remotely-managed tunnel; add the public hostname
   `delphi.<domain>` → service `http://delphi-api.delphi.svc.cluster.local:8000`. Optional: a
   rate-limiting rule on `/api/v1/auth/login` (10 req/min/IP).
6. Push the first commit to `main` so an image exists, and set `newTag` in the overlay to it.
7. `kubectl apply -f deploy/argocd/application.yaml -f deploy/argocd/image-updater.yaml`.
8. Watch the sync: `kubectl -n delphi get pods -w`. Wait for the seed jobs (Admin → Jobs page).
9. Set the GitHub repo variable `DELPHI_PUBLIC_URL`. Log in with the bootstrap admin, rotate the
   password, and create the other users (an FDE engineer, an investigator, a guest viewer).

## 7. Backups and restore

- Nightly `pg_dump -Fc`, 7 retained, on the PVC `delphi-backups`.
- **Restore drill** (M9, then monthly), to prove backups work (EFG §34 "Backup Without Restore
  Testing"):
  1. `kubectl apply -f deploy/k8s/ops/restore-test-job.yaml`. This file is not part of the Argo
     CD path. The Job (postgres image) restores the latest dump into a temporary database
     `delphi_restore_test`, then runs a second container (DELPHI image, `args:
     ["verify-restore", "--db", "delphi_restore_test"]`). That container checks table counts,
     that there is exactly one ACTIVE KB, and that the audit chain verifies. Then it drops the
     temporary database.
  2. Record the result in `docs/reports/restore-drill-<date>.md`, and delete the Job.
- Disaster recovery: re-run the bootstrap §6 on a new k3s node, then restore the latest dump
  instead of seeding (`delphi seed` is skipped because the DB is not empty).

## 8. Runbooks (short form)

| Symptom | Steps |
|---|---|
| Deploy verify job timed out | `kubectl -n argocd get applications delphi -o yaml` (sync status, image override); Image Updater logs (did it see the tag? registry auth?); `kubectl -n delphi get pods` (ImagePullBackOff → `ghcr-pull`; CrashLoop → logs; migrate hook failed → `kubectl logs job/delphi-migrate`) |
| Rollback | Fast: in the Argo CD UI, roll back the `delphi` Application to the previous revision (the image override is part of the history), *or* revert the commit on `main` (a new image deploys). A migration that is not backward-compatible must never ship (expand/contract), so rollbacks are safe |
| Site down but pods healthy | `kubectl -n delphi logs deploy/cloudflared`; Cloudflare tunnel status page |
| LLM failing | Overview alert shows the fallback rate; check `llm_calls` errors; set `DELPHI_LLM_PROVIDER=template` in the ConfigMap via git to kill it |
| Jobs stuck | Admin → Jobs; jobs with `locked_until` in the past are re-claimed automatically; failed jobs show errors; retry from the UI |
| Disk filling | `local-path` volumes in `/var/lib/rancher/k3s/storage`; prune backups; run `RETENTION_SWEEP` |

## 9. Local development

`compose.yaml` services: `postgres`, `api` (hot reload, `uv run delphi api --reload`),
`worker`, `frontend` (Vite dev server with a proxy to `api`). `just dev` starts them;
`just seed-test` loads the `test` profile. The developer's LLM provider defaults to `template`.
