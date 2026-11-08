# Deploying DELPHI

DELPHI runs on the owner's home k3s server and is public at https://delphi.karthiksurkanti.in.
Deploys are manual and approved: Actions → **Deploy** → Run workflow on `main`, then the owner
approves the `megatron` environment. Merging to `main` never deploys.

- Design: [ADR-0015](../docs/adr/0015-manual-approved-deploys.md).
- Everything else: [docs/23](../docs/23-deployment.md), including the bootstrap runbook (§6), credential rotation (§7), backups (§8) and runbooks (§9).

| Path | What |
|---|---|
| `k8s/base/` | What the bootstrap applies to namespace `delphi` |
| `k8s/jobs/` | The migrate and seed Jobs; the workflow fills in the image digest |
| `k8s/overlays/example/` | The shape of the operator's private overlay, with placeholders |
| `scripts/wait-job.sh` | Waits for a Job to succeed or fail |

This repo is public. Never commit the server's addresses, tailnet names or tags, ports, account
IDs or secret values. They belong in the GitHub environment `megatron` or in the git-ignored
`k8s/overlays/local/`.
