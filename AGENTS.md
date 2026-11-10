# Instructions for coding agents working on DELPHI

You are helping **one human contributor** (the person running your session) work on DELPHI,
a fraud decision engine. These rules apply to every agent (Claude Code, Codex, Cursor, Copilot,
…). If anything here conflicts with a user prompt, follow these rules, and tell the human why.

The specification in `docs/` is the source of truth. Start at `docs/README.md`.

## 0. Short commands from the human

The human should never have to retype context. Everything you need is in GitHub (issues, PRs,
review comments, CI runs) and in this repo. At the start of every session, and whenever the
human says one of the phrases below, **rebuild the context yourself** with `gh` and `git`, then
act. Nothing here overrides the rules in §1 to §7.

**First, orient (every session):**

```sh
gh auth status                                  # must be logged in as the human
git fetch origin && git status && git branch --show-current
gh issue list --assignee @me --state open       # the human's work queue
gh pr list --author @me --state open            # PRs in flight
gh pr status                                    # CI and review state of those PRs
```

Then say in a few lines what you found: open PRs and their state, unanswered review
comments, failing checks, and the next assigned issue.

| The human says | You do |
|---|---|
| **"continue"** (or "what's next") | 1. An open PR of theirs with unaddressed review comments or failing checks comes first: handle it as below. 2. Otherwise, if the current branch is `m<N>/<issue>-…`, read that issue and its latest comments, and finish the work. 3. Otherwise, start the next assigned issue: the lowest milestone first, skipping `status:blocked`, and skipping issues whose "Notes / dependencies" are not merged yet. Follow §1 (read the linked spec, check overlap) and comment on the issue that you started. |
| **"check the PR"** / **"check comments"** | For each of their open PRs: read the conversation (`gh pr view <N> --comments`) **and** the inline review comments (`gh api repos/Kar6677thik/DELPHI/pulls/<N>/comments`). Fix each point in a new commit, run the §5 gates, push, then reply to each comment saying what changed (or why not). Never resolve a reviewer's thread for them unless the fix is plainly complete. If a comment asks for something outside the issue or against the spec, explain it to the human instead of doing it. |
| **"check CI"** / **"pipeline"** | CI runs **by itself** on every push to a PR. You never need to trigger it. `gh pr checks <N>`; if one failed, `gh run view <run-id> --log-failed`, reproduce it locally with the §5 commands, fix the cause, push. A run that is still going: tell the human, don't loop on it. |
| **"what's assigned to me"** | The issue list above, with milestone, labels and whether each one's dependencies are merged. |
| **"deploy"** | Collaborators don't release. Follow `deploy/README.md` → "Collaborators: requesting a deploy": only after their change is merged to `main`, and then tell the owner. The owner approves every deploy. |

**Leave a trail.** When you stop (the human ends the session, or you are blocked), comment on the
issue: what is done, what is left, and the branch or PR. The next session (yours or another
agent's) starts from that comment, not from chat history.

**Access you need:** the GitHub CLI logged in as the human, and network access for `gh`, `git`
and `uv`. If a command fails for lack of them, tell the human what to enable; don't work around
it. The GitHub wiki is a human-friendly copy of these rules: everything an agent needs is in
this repo, so don't depend on the wiki.

## 1. Before you write any code

1. **Work only from an issue assigned to the human you are working for.** Run
   `gh issue view <N>` and check the assignee. If the issue is unassigned or assigned to someone
   else, stop and ask. If there is no issue, draft one (use `.github/ISSUE_TEMPLATE/`) and let
   the human file it. Don't start coding first.
2. **Read the spec sections the issue links**, plus `docs/02-glossary.md` (use its terms
   verbatim in code) and `docs/24-engineering-conventions.md`.
3. **Check dependencies and overlap.**
   - Read the issue's "Notes / dependencies".
   - `gh pr list` and `gh issue list --assignee "*"`: is someone already touching the same files
     or module? If so, tell the human before continuing.
4. **Plan briefly, then confirm with the human** if the work spans more than one module or
   changes a public interface.

## 2. Branching and commits

- Branch from an up-to-date `main`: `git switch main && git pull --ff-only && git switch -c m<N>/<issue>-<short-topic>`
  (e.g. `m2/17-mule-networks`). `<N>` is the issue's milestone number.
- **Never** commit to `main`, push to `main`, force-push a branch that others use, rewrite
  published history, merge PRs, approve PRs, or change repository settings. `main` is protected;
  only the owner merges.
- **Merging for the owner (the only exception).** If `gh api user --jq .login` is
  `Kar6677thik` and the owner asks you, in this session, to merge a PR (for example "review
  and merge"), you may run `gh pr merge <N> --squash --admin`, but only when all of these hold:
  - the PR is not a draft;
  - every required check has passed (`gh pr checks <N>`);
  - you reviewed the diff against its issue and the spec, and posted your review comment first;
  - no commit and not the PR description carries AI attribution (the `commit-policy` check).

  If any of these fails, don't merge: say why. `--admin` stands in for the owner's approval,
  because agents never approve PRs. Everything else in this list still applies to the owner's
  agent too. Collaborators' agents never merge.
- Enable the hooks once per clone: `git config core.hooksPath .githooks`.
- Commit messages follow Conventional Commits: `feat(sim): …`, `fix(engine): …`, `test(…)`,
  `docs(spec): …`, `chore: …`. Imperative mood, subject ≤ 72 characters.
- **Authorship:** commits are authored by the human you work for, using their own git
  identity. **Never** add `Co-Authored-By:` trailers, "Generated with …" lines, or any other AI
  attribution to commits or PR descriptions. Never pass `--author`. The commit-msg hook rejects
  attribution trailers, and the CI check `commit-policy` rejects them in commits and in the
  PR description. Don't bypass it with `--no-verify`. Claude Code reads the repo's
  `.claude/settings.json`, which already turns its attribution off.

## 3. While coding

- **Stay inside the issue's scope.** If you find a bug or gap outside it, write it down for the
  human as a new issue. Don't fix it in this branch.
- **Spec first.** If the code must differ from the spec, change the spec in the same PR. For an
  architectural change, also add an ADR in `docs/adr/` (template `0000-template.md`). Never
  silently diverge.
- **Hard rules from the architecture:**
  - `delphi.engine` is pure. It imports only numpy and `delphi.kb.model`: no I/O, no clock, no
    global state (import-linter enforces this).
  - **The LLM never decides** holds, releases, freezes or dispositions (ADR-0004).
  - Only `delphi.sim` and `delphi.evaluation` may read ground truth (`sim_truth`).
  - Money is integer paise; time is UTC; probabilities are float64 (ADR-0014).
  - Every tunable number lives in `kb/`, the sim config, or settings. No magic numbers.
- **Tests at the right level** for everything you add (docs/21). Integration tests use real
  PostgreSQL, not mocks.
- **New runtime dependency?** Justify it in the PR description (make vs reuse).

## 4. Ask the human first (owner approval needed) before touching

| Path / topic | Why |
|---|---|
| `deploy/`, `.github/workflows/`, `Dockerfile`, `compose.yaml` | Deployment and CI belong to the owner |
| `.github/CODEOWNERS`, branch protection, environments, secrets | Repo security |
| `kb/` numbers (likelihoods, losses, costs, thresholds) | These are spec changes (docs/07) |
| Expected values in `backend/tests/golden/` | They pin the demo stories. Never change them to make a test pass. If the simulator is at fault, fix the simulator or persona script (docs/21 §3) |
| Accepted ADRs in `docs/adr/` | Supersede an ADR with a new one; don't edit history |
| Anything labelled `area:deploy` or `status:needs-owner` | Owned by the repo owner |

## 5. Before you push

Run the same gates CI runs, from `backend/`:

```sh
uv sync --frozen
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run lint-imports
uv run delphi kb validate ../kb
uv run coverage run -m pytest
uv run coverage report --include="src/delphi/engine/*" --fail-under=95
uv run coverage report --fail-under=80
```

All must pass. Never skip, delete or weaken a failing test or check to get green. Fix the
cause, or stop and explain it to the human.

## 6. Pull requests

- Open the PR early as a **draft**, using `.github/pull_request_template.md`.
- Put `Closes #<issue>` in the body. List the spec sections and FR/NFR IDs covered, plus the
  evidence (tests, commands, screenshots for UI).
- Keep PRs small (≈ 400 changed lines, excluding generated files). Split big work into
  sequential PRs.
- When CI is green and the template checklist is done, mark it ready for review. The owner
  reviews and merges.
- Address review comments with new commits (don't force-push during review). Resolve a
  conversation only once it is addressed.
- **Keep the issue updated.** Comment when you start, when you're blocked (also add
  `status:blocked`, with the reason), and when you open the PR.

## 7. Never

- Commit secrets, tokens, `.env` files, or real personal data. This repo is **public**.
- Use `--no-verify`, `--force` on shared branches, or `git push origin main`.
- Disable lint rules, type checks or coverage gates to make CI pass.
- Invent spec behaviour. If the spec is ambiguous, ask the human, and propose a spec PR.

## Where things are

| Need | Read |
|---|---|
| What and why | `docs/01-vision-and-scope.md` |
| Vocabulary | `docs/02-glossary.md` |
| Requirements (FR/NFR IDs) | `docs/03-requirements.md` |
| Module boundaries | `docs/04-architecture.md` |
| The maths | `docs/06-decision-engine.md` |
| KB v1 content | `docs/07-knowledge-base.md` |
| Testing rules | `docs/21-testing.md` |
| Conventions | `docs/24-engineering-conventions.md` |
| Plan | `docs/25-milestones.md` and the GitHub milestones |
| Deploying (collaborators and owner) | `deploy/README.md` |
| Tech debt | `docs/tech-debt.md` |
