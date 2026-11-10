# DELPHI — instructions for Claude Code

The rules for every coding agent are in `AGENTS.md`. Follow them in full:

@AGENTS.md

## Additional notes for Claude Code

- The spec in `docs/` is the source of truth. Start at `docs/README.md`.
- Use the `gh` CLI for issues and PRs. Never run `gh pr merge`, `gh pr review --approve`,
  `gh repo edit`, or any `gh api` call that changes repository settings. The one exception is
  merging for the owner, under the conditions in `AGENTS.md` §2 ("Merging for the owner").
- Production deployment (k3s on the owner's home server, via GitHub Actions → GHCR → Argo CD;
  docs/23) is owned by the repo owner. Never add a deploy path that needs cluster credentials in
  GitHub, and never request `packages: write` in a workflow.
