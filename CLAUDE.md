# DELPHI — instructions for coding agents

DELPHI = **D**ynamic **E**vidence-**L**ed **P**robing of fraud **H**ypotheses & **I**nterventions.
The specification lives in `docs/` and is the source of truth. Start at `docs/README.md`.

## Non-negotiable rules

1. **Commit authorship.** Every commit has the repository owner as its only author. Never add
   `Co-Authored-By:` trailers, "Generated with" lines, or any AI attribution to commit messages
   or PR descriptions. Never pass `--author`. CI must never create commits or push to the repo.
2. **Deployment.** Production is the owner's home server running k3s. GitHub Actions builds and
   pushes the image to GHCR on every push to `main` and on manual `workflow_dispatch`; Argo CD +
   Argo CD Image Updater in the cluster roll it out. See `docs/23-deployment.md`. Never add a
   deploy path that needs cluster credentials in GitHub.
3. **Spec first.** If the code needs to differ from the spec, update the spec (and add an ADR in
   `docs/adr/` for architectural changes) in the same PR. Do not silently diverge.
4. **The LLM never decides.** Hold, release, decline, freeze and disposition decisions come only
   from the deterministic Bayesian engine or a human. See `docs/adr/0004-*`.
5. **Engineering principles** follow `docs/reference/engineering-field-guide.md`, applied as
   described in `docs/24-engineering-conventions.md` (modular monolith, KISS/YAGNI, explicit
   contracts, tests at every level).

## Where things are

| Need | Read |
|---|---|
| What we are building and why | `docs/01-vision-and-scope.md` |
| Vocabulary (use these exact terms in code) | `docs/02-glossary.md` |
| Requirement IDs (FR-*, NFR-*) | `docs/03-requirements.md` |
| Module boundaries | `docs/04-architecture.md` |
| The maths | `docs/06-decision-engine.md` |
| KB v1 numbers (likelihoods, losses, costs) | `docs/07-knowledge-base.md` |
| Milestones and the current plan | `docs/25-milestones.md` |
