# ADR-0015 — Manual, approved deploys to the home server; shared host tunnel

- **Status:** Accepted
- **Date:** 2026-11-08
- **Supersedes:** ADR-0009. **Amends:** ADR-0010 (where the tunnel runs).

## Context
ADR-0009 planned Argo CD and Image Updater deploying every push to `main`. Two facts changed
that:
- **The owner deploys by hand.** `main` gets dozens of merges a day, and the owner wants to
  choose when the public site changes. Deploys must be a manual `workflow_dispatch` in this
  repo.
- **DELPHI joins an existing server.** The home server ("Megatron") is a single node with about
  7.6 GiB of memory, already running several apps. It has no Argo CD and no Image Updater.
  Public sites go through **one shared Cloudflare Tunnel that runs on the host**, and apps
  already deploy with the pattern below (Sprawl).

Collaborators have write access, so they can start a `workflow_dispatch` too. The repo is
public, so it must not describe the server's network.

## Decision
- **Deploy = `deploy.yml`, `workflow_dispatch` only.** No push trigger. The run must be on
  `main`. It runs CI, builds and publishes the image, then deploys **by digest**.
- **Every deploy waits for the owner.** The deploy job uses the GitHub environment `megatron`,
  which requires the owner's approval and allows only `main`. A collaborator can start a run
  but cannot release it.
- **Reaching the cluster:**
  - The GitHub-hosted runner joins the tailnet as an **ephemeral node with a CI tag**, through
    Tailscale's OIDC federation, so no Tailscale secret is stored.
  - The tailnet ACL lets that tag reach only the cluster's Kubernetes API.
  - The runner authenticates as the namespace-scoped ServiceAccount `delphi-deployer`, with a
    token held as an environment secret.
- **Least privilege:**
  - `delphi-deployer` may patch Deployments and run Jobs in namespace `delphi`, and nothing
    else: no Secrets, ConfigMaps, Services, RBAC or cluster scope.
  - Structural changes (namespace, Services, NetworkPolicies, Postgres, RBAC) go through a
    reviewed **bootstrap** that the server's operator applies with admin rights. So a leaked CI
    token can only change images in `delphi`.
- **Integrity:** the build publishes a build-provenance attestation, and the deploy job
  verifies it for the digest before rolling out.
- **Order:** migrate Job (wait) → set the image on `api` and `worker` → wait for the rollout →
  on failure, `rollout undo`. A one-off seed Job runs only when the run asks for it. A final
  step checks the public `/health` version.
- **Public exposure:** the host's shared tunnel routes `delphi.karthiksurkanti.in` to the api
  Service's fixed ClusterIP. The server's operator adds the route and DNS. The cluster runs no
  `cloudflared` for DELPHI, and no tunnel token exists in the namespace.
- **No infrastructure details in the repo:** addresses, tailnet names, tags, ports of the
  server, account IDs and Secret contents live in GitHub environment variables and secrets, or
  in the operator's local bootstrap overlay. The repo ships an example overlay with
  placeholders.

## Alternatives considered
- **Keep ADR-0009** (Argo CD + Image Updater): it installs two controllers on a small shared
  node, and its whole point is deploying every push, which the owner does not want.
- **Self-hosted runner on the server:** runs public-repo workflows on the home machine.
- **cluster-admin kubeconfig in GitHub:** a leak would hand over the whole server.
- **In-cluster `cloudflared` for DELPHI:** a second tunnel and a token Secret to manage, for no
  gain over the shared host tunnel.

## Consequences
- **Positive:** deploys happen only when the owner releases them. CI holds a token that can
  change images in one namespace, and only while it is on the tailnet. This matches how the
  server's other apps deploy.
- **Negative:** GitHub holds a (narrow) cluster credential, which ADR-0009 avoided. It is
  mitigated by the tailnet ACL, the namespace scope, the approval gate, and rotation (doc 23
  §7).
- **Negative:** the manifests are applied by the operator, not synced continuously, so drift is
  possible. The bootstrap is re-applied from the reviewed files whenever they change.
