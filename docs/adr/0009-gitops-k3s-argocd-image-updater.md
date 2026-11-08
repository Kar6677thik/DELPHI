# ADR-0009 — GitOps on k3s: GHCR + Argo CD + Argo CD Image Updater (no bot commits)

- **Status:** Superseded by ADR-0015 (2026-11-08): deploys are manual and approved, and the
  home server has no Argo CD
- **Date:** 2026-10-31

## Context
The owner hosts on a home server running k3s. They want GitHub Actions to build the image, and
k3s to manage everything else, on every push to `main` and on manual runs. Every commit must be
authored by the owner only, so CI must not commit.

## Decision
- GitHub Actions runs CI, builds the image and pushes `ghcr.io/kar6677thik/delphi:sha-<sha>`.
  The push uses a `write:packages` token held as a secret of the `production` environment,
  which only `main` may use. The repo's own `GITHUB_TOKEN` has Read access to the package. So a
  workflow on another branch cannot publish a deployable tag (amended 2026-11-02, when
  collaborators gained write access; doc 23 §3).
- In the cluster, Argo CD syncs `deploy/k8s/overlays/home` from `main`.
- Argo CD Image Updater (strategy newest-build, tags `^sha-[0-9a-f]{40}$`, write-back method
  `argocd`) sets the image override on the Application without committing to git.
- Migrations run as a PreSync hook; seeding runs as an idempotent PostSync hook.
- A final workflow job verifies the public `/health` version.

## Alternatives considered
- A self-hosted runner on the server running `kubectl`: puts a CI runner (and cluster
  credentials) on the home server, and is risky for any public repo.
- GitHub-hosted runner + Tailscale + kubeconfig: cluster credentials in GitHub secrets.
- Image Updater or Flux with git write-back: bot commits violate the authorship rule.
- Keel: less standard, and no declarative sync of the manifests.

## Consequences
- Positive: pull-based; no inbound access to the home network; no cluster credentials in
  GitHub. Manifests are versioned in git and self-healing.
- Negative: Argo CD and Image Updater must be installed once (runbook). The Image Updater CRD
  fields are verified in M0.
- Negative: the image override lives in the Application object rather than in git (documented).
