# Deploying DELPHI

DELPHI runs on the owner's home k3s server and is public at https://delphi.karthiksurkanti.in.
Deploys are manual and approved: Actions → **Deploy** → Run workflow on `main`, then the owner
approves the `megatron` environment. Merging to `main` never deploys.

- Design: [ADR-0015](../docs/adr/0015-manual-approved-deploys.md).
- Everything else: [docs/23](../docs/23-deployment.md), including the bootstrap runbook (§6), credential rotation (§7), backups (§8) and runbooks (§9).

## How to deploy

Live site: https://delphi.karthiksurkanti.in. Health and version:
https://delphi.karthiksurkanti.in/api/v1/health (`version` is the commit SHA that is live).

Merging to `main` never deploys. A deploy is a manual run of **Actions → Deploy**, always on
`main`, and nothing reaches the server until the owner approves it.

### Inputs

| Input | Values | When |
|---|---|---|
| `action` | `deploy` (default) | Normal release: build the current `main`, test, publish the image, migrate, roll out |
| | `redeploy` | Release an image that was already built, by its digest (no build) |
| | `rollback` | Put the api and worker back on the previous release (no build, no migration) |
| `digest` | `sha256:` + 64 hex | **Only** with `redeploy`. Leave it empty for `deploy` and `rollback`, or stage 0 fails |
| `seed` | off (default) | Leave it off. Turn it on only for the first release on an empty database (it is idempotent) |

Stages: `0 · Check request` → `1 · Build & test` → `2 · Publish image` → `3 · Approve & deploy`
(waits for the owner) → `4 · Verify` (waits until the public site serves the new version).
Only one deploy runs at a time; a second one queues.

### Collaborators: requesting a deploy

You can start a run, but you cannot release it. The owner decides when the public site changes.

1. Make sure your change is merged to `main` and the `main` CI is green.
2. **Actions → Deploy → Run workflow**, branch `main`, `action = deploy`, `digest` empty,
   `seed` off. Or from a terminal:

   ```sh
   gh workflow run deploy.yml --ref main -f action=deploy
   ```

3. Stages 0 to 2 run on their own. The run then waits at **3 · Approve & deploy**.
4. Tell the owner (link the run, and say what it ships). If they reject it, or nobody approves
   it within 30 days, the run ends and nothing changes on the server.
5. After it finishes, check the run summary ("Live: … serves version …") and the site.

Don't:
- deploy from a branch other than `main` (stage 0 refuses it);
- start a `rollback` or `redeploy` without asking the owner first;
- re-run a failed deploy without reading why it failed. Failures before stage 3 are CI or
  build problems: fix them with a PR as usual.

### Owner: releasing

**Approving a requested run.** On the waiting run, click **Review deployments** → tick
`megatron` → **Approve and deploy**. This also works from the GitHub mobile app. **Reject** ends
the run with nothing changed. Approve only runs that are on `main` and whose CI stages are green.

**Releasing yourself.** Start the run as above, then approve it yourself. The environment
allows this as long as "Prevent self-review" stays off.

```sh
gh workflow run deploy.yml --ref main -f action=deploy
```

**Something is wrong after a release.**
- Fastest: `action = rollback`, `digest` empty. It goes back to the most recent earlier release,
  with no build or migration. Migrations are expand/contract, so older code still works on the
  newer schema.
- Or a specific known-good build: `action = redeploy` with that build's digest. Every run
  summary shows the digest it released (`Released ghcr.io/kar6677thik/delphi@sha256:…`).

  ```sh
  gh workflow run deploy.yml --ref main -f action=redeploy -f digest=sha256:<64 hex>
  ```

**If a run fails at stage 3:**
- At migrate: the run prints the Job's logs and nothing was rolled out. Fix forward with a PR,
  or redeploy the previous digest.
- At rollout: the run has already undone the rollout. Check the pods with the server's operator
  (image pull, probes, out of memory).
- At verify, with pods healthy: the tunnel or DNS. Check the host tunnel's route.

More: [docs/23 §3](../docs/23-deployment.md) (the workflow), §7 (credential rotation) and §9
(runbooks).

## What is in this directory

| Path | What |
|---|---|
| `k8s/base/` | What the bootstrap applies to namespace `delphi` |
| `k8s/jobs/` | The migrate and seed Jobs; the workflow fills in the image digest |
| `k8s/overlays/example/` | The shape of the operator's private overlay, with placeholders |
| `scripts/wait-job.sh` | Waits for a Job to succeed or fail |

This repo is public. Never commit the server's addresses, tailnet names or tags, ports, account
IDs or secret values. They belong in the GitHub environment `megatron` or in the git-ignored
`k8s/overlays/local/`.
