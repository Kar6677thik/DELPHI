# 11 — Typology Scout and the typology lifecycle

Research §9.7. Module `delphi.scout`. This is how DELPHI finds **previously unknown** fraud and
turns it into a versioned typology, with FDE engineers and investigators in the loop.

## 1. Input: the unexplained queue

A Guardian session or case enters the unexplained queue when its **terminal** posterior has
P(UNEXPLAINED) ≥ `unexplained_routing_threshold` (0.35). It stays there until it is assigned to
a promoted typology, or dismissed by an FDE engineer.

**Implementation (#117).** The queue is one SQL query (`delphi.scout.queue`), bounded by every
caller (a page, a count, one item):
- The sources are `guardian.api.unexplained_sessions_query` (decided sessions flagged
  `unexplained`, partial index on `decided_at`) and `navigator.api.unexplained_reviews_query`
  (disposed ACCOUNT_REVIEW cases, with the P(UNEXPLAINED) threshold applied in SQL).
- Dismissed items and members of promoted clusters are excluded with `NOT EXISTS`, the latter
  through a GIN index on `scout_clusters.member_refs`.
- Items are ordered by `(entered_at, subject_id)`, with a keyset cursor.

## 2. Scout run (`RUN_SCOUT` job)

Triggered manually (`POST /scout/runs`) or automatically when ≥ 10 new items have entered the
queue since the last run.

**Feature vector per item:**

- probe/evidence outcomes, one-hot per source, plus one `not_observed` indicator per source;
- every payment feature in doc 13 §3 marked `scout` ✔ (including the ones excluded from the
  prior model), z-scored using the training means and standard deviations;
- payment channel one-hot;
- amount band (log10 amount, z-scored);
- F3 stage indicators of the customer's last 24 h of events.

**Clustering:** scikit-learn `HDBSCAN(min_cluster_size=5, min_samples=3, metric="euclidean",
allow_single_cluster=True)` on the queue items from the last 30 days. A queue holding one
pattern (the Scout's main case: in S-4, collect-refund scams dominate the queue) is one cluster,
not all noise. With a single cluster, HDBSCAN keeps only its densest members unless
`cluster_selection_epsilon` is set: `scout.cluster_selection_epsilon` (1.0, a KB model default,
approved on #117), below the √2 distance of one differing outcome, so a
pattern keeps all its members while a pattern that differs in one answer stays separate. Points
labelled noise stay in the queue.

**For each cluster:**

- `size`, the time span, and the member ids.
- **Distinguishing features**: the top 8 features by absolute standardised mean difference
  between the cluster and the known-typology-attributed sessions of the same period.
- **Outcome profile**: for each observed source, the cluster's empirical outcome frequencies.
- **Draft likelihood rows**: for each source, (counts + 1) / (n + |O|), i.e. Laplace smoothing,
  with ESS = min(n + |O|, 20). Sources the cluster never observed get no row (uniform by doc 06
  §2 rule 3).
- **Suggested name and description**: `LLM_TYPOLOGY_DESCRIPTION` job (doc 15). The template
  fallback is "Candidate pattern {n}: {top 3 distinguishing features}".

The output is `scout_clusters` rows plus one `typology_drafts` row per cluster, with status
`CANDIDATE`. A run also marks, in the same transaction, the earlier runs' drafts that are still
`CANDIDATE` as `SUPERSEDED` (the owner's decision on #255), so only the latest run's candidates
are open. A draft past `CANDIDATE` (edited, backtested, in shadow, promoted or rejected) is never
touched. A `SUPERSEDED` draft is final: it cannot be edited, and the draft list shows
`CANDIDATE` drafts unless asked for another status. The run audits the transition once, listing
the drafts it superseded (`scout.drafts.superseded`, by the system, on the run). A run that finds
no cluster supersedes the earlier candidates too: it read the newer queue, and a candidate it no
longer finds is stale.

**Implementation** (#117): the clustering core is `delphi.scout.clustering` (pure functions):
`matrix`, `cluster_labels`, `distinguishing_features` (absolute standardised mean difference,
`(mean_c − mean_r) / sqrt((var_c + var_r) / 2)`; a column constant in both groups at different
values separates them perfectly and ranks first, reported as `smd: null, separates: true`; a
column with no difference beyond float noise is left out; with no known-typology items to
compare against there are no distinguishing features),
`outcome_profile`, `draft_likelihoods` (ESS capped at `elicitation.default_ess`) and
`template_name`, composed by `scout()`. Its numbers are the KB's `scout:` section. A feature
missing from an item sits at its training mean (z = 0).

**The queue** (`delphi.scout.queue`, #117):
- **Items:** decided Guardian sessions with the `unexplained` flag, and disposed ACCOUNT_REVIEW
  cases whose posterior at disposition is ≥ `unexplained_routing_threshold`. A PAYMENT_HOLD case
  is not a second item, because its session already is.
- **Leaving the queue:** an item leaves when an FDE engineer dismisses it
  (`scout_queue_dismissals`, one row per item, audited), or when it is a member of a cluster
  whose typology draft is PROMOTED. Members are stored as `{subject_type, subject_id}` refs.
- **The run** (`delphi.scout.run`): `RUN_SCOUT` (`POST /scout/runs`, or `SCOUT_AUTO_TRIGGER`
  once `auto_trigger_new_items` items have entered since the last run) loads the queue items of
  the last `lookback_days`.
  - It is bounded (after #263): at most `DELPHI_SCOUT_MAX_ITEMS` (500) queue items, the newest
    of the lookback, and as many reference sessions (newest first, served by a partial index
    on `guardian_sessions (decided_at DESC, id) WHERE NOT unexplained`). The sensing config is
    parsed once per run. The run's transaction has `SET LOCAL statement_timeout` (30 s) and
    `work_mem` (16 MB).
  - Runs are serialised: a run takes `pg_try_advisory_xact_lock(55)` and, while another holds
    it, skips and says so in its result (a manual and an automatic run, or two manual ones).
  - It is idempotent: a retried job (a lost lease) finds its run by `scout_runs.job_id` (unique)
    and returns it.
  - The auto-trigger enqueues nothing while a `RUN_SCOUT` is QUEUED or RUNNING, nor for
    `DELPHI_SCOUT_FAILURE_BACKOFF_MIN` (60) after the latest one FAILED. It counts the items
    that entered since the last run started (before any run: within the lookback) only up to
    `auto_trigger_new_items`, so the count stays bounded.
  - The reference set is that period's sessions attributed to a known typology: not flagged
    unexplained, with an argmax (taken in SQL) that is neither GENUINE nor UNEXPLAINED.
  - Each item's outcomes are a session's completed probe runs or a case's current evidence
    records. Its features are the doc 13 §3 payment features (none for an account review).
  - Each cluster is stored with its size, members and time span (`first_at`/`last_at`, next to
    its features), plus a CANDIDATE `typology_drafts` row. The draft's spec holds the suggested
    name, the draft likelihood rows and the outcome profile.
- **Gap fills:**
  - z-scores use the run's own items, because the sensing artifacts store no feature means.
  - F3 stage indicators are empty until the event → stage mapping is specified (A-07 names only
    the stages).

## 3. FDE drafting (`DRAFT`)

An FDE_ENGINEER opens a candidate and edits:

- `key` (UPPER_SNAKE_CASE, unique), display name, description, colour, F3 stages, contexts.
- Likelihood rows: accept the empirical rows, or edit them with an elicitation helper. The
  helper takes probabilities plus a confidence (LOW/MEDIUM/HIGH → ESS 10/20/50).
- `open_mass_share` (default 0.5). This is required because the sensing model is not trained on
  the new typology (doc 06 §11).
- The loss block for each decision profile column. The default is copied from
  `APP_INVESTMENT`.
- **Enable existing but disabled sources** (e.g. `COLLECT_INTENT_CHECK`) and set their rows for
  *all* hypotheses (the editor pre-fills KB values for the known hypotheses).

Every save increments `revision` (with an optimistic lock) and returns the draft to `DRAFT`.

**Implementation (#118).** `delphi.scout.drafts` and `GET`/`PATCH /typology-drafts`:
- **The spec.** A draft's `spec` holds:
  - `name` (the display name), `key`, `description`, `color`, `f3_stages` and `contexts`;
  - `likelihoods`: the new hypothesis's rows, starting from the cluster's;
  - `open_mass_share` (0.5), and `loss` (null means `APP_INVESTMENT`'s block);
  - `enabled_sources`: each source ACTIVE has disabled, with a row for every hypothesis;
  - the cluster's `outcome_profile`.
- **Validation.** Every save builds the candidate KB, ACTIVE plus the new hypothesis appended to
  the order and its enabled sources switched on, and runs the KB's own rules (`kb.validate`).
  - A draft that breaks one is a 422 listing the issues in `errors`, and nothing is saved.
  - A draft that is valid but unsaved shows its issues in `GET` (a CANDIDATE lacks a key and a
    colour).
- **A save** needs `If-Match` with the draft's `version` (412 when stale). It is the next
  `revision`, the status returns to DRAFT, and the editor is added to `editors`; it is audited.
- **Not editable:** a draft that is IN_SHADOW, PROMOTED, REJECTED or SUPERSEDED (409).
- **Rows.** An enabled source needs a row for every hypothesis, the new key included; a missing
  or unknown hypothesis is an issue. When `likelihoods` (which starts with the cluster's
  empirical rows, for every source it observed, disabled ones included) and `enabled_sources`
  both give the new hypothesis's row for a source, the `enabled_sources` row wins: enabling a
  source is where the engineer sets it deliberately. A draft with no `loss` copies `APP_INVESTMENT`'s; with no `APP_INVESTMENT` in ACTIVE,
  that is an issue too.
- **Keys.** A draft's key must not be a hypothesis in ACTIVE. Two open drafts may claim the same
  key; promotion enforces uniqueness (§6), because the KB it builds is validated then.
- **Stored specs are read leniently** (unknown fields kept, no length limit on the name), so a
  later field or a long suggested name never breaks a read; an edit is validated strictly.

## 4. Backtest (`RUN_BACKTEST` job) → `BACKTESTED`

Replays, under candidate KB = (ACTIVE + draft), every Guardian session and case from the last 30
days that has stored prior components and observations:

- rebuild the prior (doc 06 §11) and re-apply the stored observations in their original order;
- recompute the terminal decision.

A replay cannot run probes that were never run. The report states this limitation.

**Report:**

| Metric | Definition |
|---|---|
| attributed | # queue items whose argmax is now the new typology |
| attributed_labelled_precision | among attributed items with a label, the share labelled as fraud (any fraud hypothesis) |
| genuine_disturbed | # sessions labelled GENUINE whose decision changed |
| decision_changes | a table of old decision → new decision counts |
| unexplained_rate | before vs after |

There is no automatic pass/fail. The FDE engineer judges, and the report is attached to the
draft.

## 5. Shadow (`IN_SHADOW`)

- Creates a KB version (parent = ACTIVE + draft) with status `SHADOW`.
- For every new Guardian session and every case update, the shadow KB is evaluated **on the
  same observations** (it cannot choose its own probes). The engine also records which probe the
  shadow *would* have chosen first. The result is stored in `shadow_evaluations`
  `{session_or_case_id, kb_version_id, posterior, decision, would_select_first}`.
- Exception for sources that are disabled in ACTIVE but enabled in SHADOW (like
  `COLLECT_INTENT_CHECK`): they are never shown to customers while in shadow, so their effect is
  only visible in the backtest and after promotion. The shadow report says so.
- Shadow report: decision agreement rate with ACTIVE, the list of disagreements, new-typology
  attribution count.

## 6. Promotion (`PROMOTED`) — four-eyes

- Requires ≥ `shadow_min_sessions` shadow evaluations (default 200; **10 in DEMO mode**).
- Requires an approver with role FDE_ENGINEER or ADMIN who has **never edited any revision** of
  the draft (`typology_drafts.editors`).
- Requires the shadow KB version's `parent_id` to still be the current ACTIVE (stale-parent
  rule, doc 07 §1).
- The shadow KB version becomes `ACTIVE`, and the previous ACTIVE becomes `RETIRED`. This
  happens in one transaction, with an audit entry.
- Queue items attributed to the new typology under the new ACTIVE version leave the unexplained
  queue.
- Rollback: an ADMIN/FDE can re-activate the previous version (doc 07 §1) with a reason.

## 7. Demo story S-4 ("the reveal")

The demo dataset's live window contains ~15 `UPI_COLLECT_REFUND` scam payments (doc 14 §6).
Their pattern: channel `UPI_COLLECT_APPROVAL`; purpose answer `BELIEVES_RECEIVING_MONEY`; on a
call with an unknown number in 60% of cases; clean known device; a new payee that looks like a
merchant; amounts ₹2,000–₹50,000. KB v1 has no typology for it, so these sessions end with a
high P(UNEXPLAINED), because `BELIEVES_RECEIVING_MONEY` has likelihood 0.01 under every known
hypothesis and 0.25 under ∅.

Script:

1. Run Scout → one cluster of ≥ 10 → candidate "Customer believes they are receiving money
   (collect request)".
2. The FDE engineer sets key `APP_COLLECT_REFUND` and enables `COLLECT_INTENT_CHECK` with row
   EXPECTS_TO_RECEIVE 0.85 for the new typology.
3. Backtest → attributed ≥ 80% of the cluster.
4. Shadow → autoplay generates ≥ 10 sessions.
5. A second user approves → ACTIVE.
6. A fresh collect-refund payment gets `COLLECT_INTENT_CHECK`, is attributed to
   `APP_COLLECT_REFUND`, and is held.

Acceptance: e2e test E2E-S4 (doc 21).
