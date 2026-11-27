# 18 — Frontend

ADR-0008. React 18 + TypeScript (strict) + Vite, served as static files by the api process at
`/`. All API calls go to `/api/v1` on the same origin.

## 1. Stack

| Concern | Choice |
|---|---|
| Routing | React Router |
| Server state | TanStack Query. SSE events invalidate the relevant query keys |
| Client state | Component state + React context. No global store library |
| Styling | Tailwind CSS + Radix UI primitives (accessible dialogs, menus, tabs, tooltips) |
| Charts | Recharts |
| Graph (Chase) | `@xyflow/react` with a dagre left-to-right layout |
| Forms | react-hook-form + zod (validated locally; the server re-validates) |
| API types | Generated from `openapi.json` (doc 16), never hand-written |
| Tests | Vitest + Testing Library (components), Playwright (e2e, doc 21) |
| Lint/format | ESLint (typescript-eslint strict) + Prettier |

## 2. Routes and screens

| Route | Screen | Roles | Key contents |
|---|---|---|---|
| `/login` | Login | public | username/password; generic error message |
| `/` | Overview | all | KPI tiles (doc 22 §7), queues summary, recent decisions, active alerts |
| `/phone` | Mock phone (presenter view) | `demo:drive` | Left: phone frame (persona picker, payee list, amount, pay button, probe screens per `ui` type, decision screens). Right: live DELPHI panel (hypothesis bar, candidates, decision). Toggle "Phone only" for the judge projector |
| `/guardian` | Guardian monitor | all | session list (live via SSE), filters, unexplained flag |
| `/guardian/:id` | Session detail | all | hypothesis-bar timeline, candidates table per step, decision and risk table, baseline comparison, DD pack link |
| `/cases` | Case queue | all | sortable table: SLA countdown, type, protocol, leading hypothesis, expected loss, campaign |
| `/cases/:id` | Navigator console | all (write: `case:write`) | doc 09 §2 layout |
| `/cases/:id/dd-pack` | DD pack (printable) | all | print stylesheet; browser "Save as PDF" |
| `/campaigns`, `/campaigns/:id` | Campaigns | all | F3 stage strip, shared entities |
| `/chase/:id` | Chase | all (write: `chase:write`) | doc 10 §7 |
| `/scout` | Typology Scout | all (write: `fde`) | queue size, run button, clusters with distinguishing features |
| `/scout/drafts/:id` | Typology draft editor | all (write: `fde`) | metadata form, likelihood row editor with live normalisation and ESS picker, source enablement, backtest report, shadow progress, promote/reject |
| `/kb` | KB versions | all | version list with status and lineage, diff viewer, export, promote/rollback |
| `/kb/questions` | Question bank review | all (write: `fde`) | pending variants side by side with the English original; approve/edit/reject |
| `/learning` | Learning | all | learning runs and diff reports, OPE table, probe yield chart with alerts, trace analytics |
| `/evaluation` | Evaluation | all (run: `admin`) | results table B1/B2/DELPHI with CIs, charts |
| `/admin` | Admin | `admin` | users, datasets, sensing training, demo story runner, demo reset, jobs |

**M4 case stub (#97):** `/cases` lists the first page of `GET /cases`, soonest SLA first: the SLA
countdown (it ticks, rounds up, and takes "SLA breached" from the server's `sla_breached`),
the leading hypothesis's display name (linking to `/cases/:id`, still a placeholder), type,
protocol (a Coercion badge), the amount at stake (`MoneyText`) and status, with loading, empty
and error states. `case.opened` and `case.updated` invalidate the `['cases']` queries
(`lib/liveQueries.ts`, the first SSE → query-key map). Sorting controls, expected loss and
campaign arrive with the console and campaigns (#103, M5).

**Campaigns (#107):** `/campaigns` lists the live campaigns from `GET /campaigns`, highest
`rank_score` first: the leading hypothesis (a link), the F3 stage strip, the case count, the total
expected loss and the vulnerable victims. `/campaigns/:id` (`GET /campaigns/{id}`) shows the strip,
the totals (cases, vulnerable victims, expected loss, rank score), the shared infrastructure (each
entity's kind, value and case count) and the cases, oldest first, each linking to its console. With
more cases than the detail holds (200), it links to `/cases?campaign_id=`. A campaign that merged
says so and links to the one that took its cases.
- **The F3 stage strip** is four segments in kill-chain order, filled up to `f3_stage_max`, with the
  stage in words beside it (and in its accessible name). While `f3_stage_max` is null (doc 09 §8)
  it reads "F3 stage not known yet", never an empty chain.
- `campaign.updated` refreshes the campaign queries and the case queue (its campaign column).

**M5 case queue and Navigator console (#105):** `/cases` gains an order control (soonest SLA,
highest expected loss or newest, `GET /cases?sort=`) and an "Expected loss" column: the loss of
the case's `recommended_decision` in its `risk_by_decision`, which the backend stores from the
moment a case opens (#208), and "—" for a row without one. The "Campaign" column links a case
to its campaign ("—" for none), and `/cases?campaign_id=` lists one campaign's cases (#107).
`/cases/:id` follows doc 09 §2 from `GET /cases/{id}`:
- the posterior `HypothesisBar` and the `RiskTable`;
- the "Evidence sufficient — recommend {decision} (expected loss ₹X)" banner while
  `evidence_sufficient` and not disposed;
- the next-best-evidence rows in the server's order, with the "Protocol" badge, minutes, "X% of
  remaining uncertainty" and net value. "Fetch" posts the evidence request; the outreach call
  opens a form for its outcome (the KB source's `outcomes`) and note;
- "What would change my mind";
- one `EvidenceCard` per record, whose narrative is labelled "AI-written summary" (`LLM`) or
  "Template summary". Its `[record_id]` citations render as chips that scroll to, focus and
  highlight that record's fact table (a citation to a record not on the page is plain text).
  Facts named `*_paise` show as ₹ and `*_share` as a percentage;
- loading and error states; a disposed case shows "(decision taken)" in the risk table and
  neither the banner nor the next-best evidence;
- the disposition panel. Its decisions are the keys of `risk_by_decision`. CONFIRM_FRAUD names
  a typology (the case's fraud hypotheses, most likely first, then FRAUD_OTHER). Confidence is
  1–5, and an override needs a rationale of at least 20 characters.

Without `case:write`, the fetch buttons are disabled and the disposition form is replaced by a
note. Every POST sends a fresh `Idempotency-Key`; the disposition sends the ETag the console was
showing as `If-Match`. On 412 the panel shows "This case changed — reload" with the fields that
changed (status, leading hypothesis, recommendation, evidence count, disposition), and locks the
form until "Reload". `evidence.narrative_ready` invalidates the `['cases']` queries, like
`case.*`.

**The police alert in the console (#209):** a coercion case's console shows its police alert
draft (doc 09 §6) under the header: its status ("Draft, not sent", "Sent (simulated)" or
"Discarded", with who acted, "you" for the signed-in user, and when), the victim's pseudonym, the
amount, channel, beneficiary account and payment time, and once sent the evidence it carried.
With `case:write`, Send and Discard each ask for confirmation first, then call
`POST /cases/{id}/police-alert/{send|discard}`. The answer replaces the case and its ETag. A 409
(someone acted first) says so and reloads the case. A case without an alert shows nothing.

**Console view events and the DD pack page (#264):** the console sends the investigator's view
events (doc 09 §5) through `api/traces.ts`. It queues them and sends at most one batch per 5 s to
`POST /cases/{id}/trace-events` (at most 100 events per batch), plus whatever is left when the
console unmounts. A failed send is dropped: analytics never block the console. The events:
- `CASE_VIEWED` once when the console opens;
- `EVIDENCE_VIEWED` and `WCMM_VIEWED` the first time an evidence card or "What would change my
  mind" is at least half on screen (`IntersectionObserver`; without it, when shown). Each counts
  once per page;
- `NARRATIVE_EXPANDED` each time the investigator opens one of a narrative's citations, for the
  narrative's record.

The console links to `/cases/:id/dd-pack`. That page reads `GET /cases/{id}/dd-pack` with field
checks, which records `DD_PACK_VIEWED` on the server. It shows "Complete" or "Incomplete", then
the nine sections in order: each with "Required" or "Optional" ("· missing" for an empty required
one), its note, and its content as tables, lists and text, or "Not applicable". "Print or save as
PDF" prints the page, with the app's navigation and the page's buttons left out of print. "Open
the printable page" opens the server's HTML version.

**The knowledge base, read-only (part of #119):** `/kb` lists `GET /kb/versions` (status, change
summary, "From vN" lineage) and opens the ACTIVE version, or the one in `?v=`. For it: the
hypotheses with their descriptions (and the open-world Unexplained), every source grouped as
passive checks, questions asked of the customer, and investigator evidence, each with its
**separation** (the equal-weight Jensen–Shannon divergence of its likelihood rows, in bits: how far
apart the explanations' predicted answers sit). The chosen source shows its description, its
approved English question, and its likelihood table (outcome × hypothesis, washed in the
hypothesis colour); choosing an answer shows the belief that answer alone would leave from even
odds, with each hypothesis's factor against its even share. That is an explainer, not the engine's
update (which starts from the session's prior). A version with a parent shows `GET …/diff`;
"Export as YAML" downloads `GET …/export`. Promote and rollback follow with the shadow evaluations.

**Admin (#231):** `/admin` opens with one readiness line, then a strip from `GET /status`: the
dataset (with the seeding progress while it is made) and each sensing model's ACTIVE version,
marked "below its gate" when it is in `models_below_gate`. "Set up the demo" queues a dataset
(`POST /admin/datasets`, the profile chosen) and a retraining (`POST /admin/sensing/train`,
disabled without a dataset), each with a fresh `Idempotency-Key`; a 409 shows as a problem alert.
"Audit chain" runs `GET /admin/audit/verify` only on a click, because it reads every entry: it shows
"Intact" with the entries checked, or "Broken" with the first bad entry. "Jobs" lists `GET /jobs`
newest first, filtered by status and type, 25 more at a time: type, status (a live mark while
running), progress, tries, when it was queued, how long it took, and the error; a FAILED job has
"Retry" (`POST /jobs/{id}/retry`). `job.updated` invalidates the `['admin']` queries. "Users"
lists `GET /admin/users` (the signed-in admin marked "you"): a role select, "Disable" / "Enable"
(never offered on your own row) and "Reset password" each send `PATCH /admin/users/{id}` with the
row's `version` as `If-Match`; a 412 or the last-admin 409 shows as a problem alert and the list is
re-read. "Add a user" posts `POST /admin/users` (username pattern, display name, a password of at
least 12 characters, role). "Demo stories" lists S-1 and S-2 (#101): "Autoplay" queues
`POST /admin/demo/scenarios/{story}/run` with `mode: autoplay` (disabled until the dataset and
models are ready) and points to the Guardian monitor; "Play on the phone" opens
`/phone?story=S1`. "Reset the demo" asks first ("Yes, reset" / "Keep it"), then posts
`POST /admin/demo/reset` with an `Idempotency-Key` and reads the `DEMO_RESET` job each second
until it finishes: the rows removed and accounts restored, "another reset was already running"
for a `skipped` result, the job's error if it failed, and a warning to restart the api and the
worker when `restart_needed` (the processes load the ACTIVE KB at start-up). It is disabled
without a dataset.

**M4 Guardian screens (#100):** `/guardian` lists `GET /guardian/sessions` newest first, 50 at a
time, with "Load more sessions" while `next_cursor` is set: start time (linking to
`/guardian/:id`), amount (`MoneyText`), the leading hypothesis's display name (from each row's own
pinned KB version) with an "Unexplained" badge, status, decision and the B1 / B2 verdicts (B2's
middle band reads "OTP step-up"). Filters: status and mode (from the schema's enums), decision
(the ACTIVE KB's `GUARDIAN_PAYMENT` decisions) and "unexplained only". `/guardian/:id` shows the
decision with its terminal reason (a customer cancel shows the reason alone), the
`HypothesisTimeline` (the prior, then the posterior after each completed probe; "No observations
yet." when there are none), the `RiskTable`, the baseline comparison (B2's `OTP_STEP_UP` reads
"released if the OTP passes"), one `CandidatesTable` per planning step and the DD pack link.
Hypothesis names and colours come from the session's pinned KB version (`GET /kb/versions/{id}`,
fetched once per version); the KB content and the candidates are read with field checks, not
casts, until the api types them (#198). `guardian.session.updated` and `guardian.session.decided` invalidate
the `['guardian']` queries.
The session detail also shows the current belief as the Oracle (`BeliefView`, as on the phone's
live panel) with the posterior bar beneath; where WebGL is missing, the bar alone. In-flight
rows on `/guardian` carry a live mark; decisions are toned (release green, review amber, holds
and declines red) and always named in text too.

## 2a. Design system: "data terminal", dark (#99)

DELPHI is a monitoring tool read in long sessions, so it is dark and quiet: colour belongs to
data (the hypotheses' hues, a status), never to chrome. The tokens live in `src/index.css`:

- **Ground and surfaces.** A near-black ground (`#171719`), cards one step darker (`#131217`),
  separated by tone, not by lines or shadows. Text is white, with two greys (`#bdbdc0` soft,
  `#8a8c92` muted) that both pass 4.5:1.
- **One selection mark.** The active navigation item, and every other selection, is a 1px line
  in the accent (`#4a8ad6`) exactly as wide as its label. The primary action is inverted (a white
  pill with dark text). No gradients, glows, glass or tinted icon chips.
- **Type.** Public Sans for text, and IBM Plex Mono for every figure that stacks or updates
  (probabilities, ₹ amounts, bits, timers). Both are self-hosted (`@fontsource/*`), because the
  CSP is `default-src 'self'` (doc 19 §4).
- **Hypothesis colours** come from the KB (doc 07 §4). They are set for light paper, so the UI
  draws each at the same hue lifted to at least 60% lightness (`forDark`); the KB is unchanged.
- **Existing screens.** The classes the M4–M5 screens were written with (`slate-*` and the pale
  tints) are mapped onto this palette in `index.css`, so those screens are dark without being
  rewritten. New screens use the role names (`bg-surface`, `text-muted`, `text-accent` …).
- **Shell.** A labelled sidebar on the ground with DELPHI's mark and scope; on a phone it folds
  into a 48px bar with a menu sheet.

**The mock phone (`/phone`).** A drawn handset (status bar, camera island, side keys) hosts the
customer's bank app. That app is deliberately its own white-on-black look, not DELPHI's chrome.
Each probe `ui` type has its own screen (doc 08 §4); the protective hold turns the whole screen
paper-white with a red shield (doc 08 §7). The DELPHI side leads with the leading hypothesis's
share as the largest figure and the posterior as a tall bar, then what the customer said, why
this question (the candidates) and the risk table. "Phone only" shows the handset alone on black
for the projector (Escape returns). The phone's requests and personas use the generated types;
the phone stream's events (doc 16 §3) are typed by hand in `src/api/phone.ts` from doc 08 §4,
since an SSE body has no OpenAPI schema. A browser cannot see the phone's call or screen, so
the presenter sets them in **"Around the customer"**, above the phone (hidden in "Phone only"):
- **the call:** no call, on a call with an unknown number, or on a call with a contact;
- **"Screen shared or remote access":** sends both flags;
- **typing:** natural, pasted and hesitant (a paste and a 34 s field dwell), or not their rhythm
  (a cadence z of 4.1).

The payment carries these as its telemetry (doc 08 §5), with the session's real length from the
moment the form opened. Each preset sits clearly on one side of the KB's classifier thresholds
(a 20 s dwell, a cadence z of 3.0), so CALL_STATUS, DEVICE_POSTURE and SESSION_BEHAVIOUR observe
what the presenter chose. The default is calm: no call, no shared screen, natural typing. S-1
starts with "On a call, unknown number".

**Interactive stories.** The **Story** select (or `?story=S1`, from Admin) queues the story's
`DEMO_STORY` job in `mode: interactive` and reads `GET /jobs/{id}` each second until it
finishes. The job's result names the story's customer and the payment: the phone switches to
that customer, the form starts filled in (the story's payee, shown as "New payee" when it is not
saved; its amount, channel and device, S-2's being the new phone the job staged), and "Around the
customer" starts where the story's scripted telemetry puts it. The presenter can still change
either, then pays and answers as the customer. A failed job shows its error.

**The Theatre (`/theatre`).** The judges' view, full-screen on black and outside the app
shell. It is the one place the quiet rules above give way to spectacle (bloom, light,
motion), and it stays honest: what moves is the engine's numbers.

- **The Oracle** (`src/theatre/Oracle.tsx`, three.js through React Three Fiber, loaded only on
  this route) draws the posterior as 4,000 particles, one well per hypothesis and Unexplained at
  the centre.
- **Each particle is 1/4,000 of the probability.** On an update only the surplus flies, on
  curved paths, from the wells that lost mass to those that gained it. A decision sends a
  shockwave out of the winning well.
- **Around it:** the customer's phone on the left, what DELPHI weighs on the right (candidate
  questions by net value, or the expected loss of each decision) and a caption per step.
- **Act 1 (S-1) plays `src/theatre/replays/s1.json`.** That file is the real engine's run on the
  ENG-S1 golden fixtures, generated by `backend/tests/golden/s1_replay.py` and pinned by
  `test_theatre_replay.py`, so it cannot drift from the engine. It also serves as the
  live-demo fallback.
- **Presenter keys:** space pauses, the arrows step, R restarts. `?step=N&paused` opens at a
  step.

## 3. Shared components (single implementation each)

- `HypothesisBar`: horizontal stacked bar. Fixed colour per hypothesis (doc 07 §4). Every
  segment ≥ 4% shows its label and percentage; smaller segments are grouped into "other" with a
  tooltip. A `timeline` variant stacks one bar per step.
- `CandidatesTable`: source, eligible/reason, cost ₹, VoI ₹, net value ₹, settle %, selected
  badge (GREEDY/EXPLORE + propensity).
- `RiskTable`: decision → expected loss ₹, lowest first, with one row highlighted and marked in
  text: "(decision taken)" once the session or case has a decision (a fail-safe decision need
  not be the lowest-risk one), else the lowest-risk row as "(recommended)".
- `EvidenceCard`, `NarrativeWithCitations`: citations render as chips that scroll to and
  highlight the fact row.
- `MoneyText`: formats paise as ₹ in the Indian grouping (₹4,80,000.00); `sim` durations show
  in sim minutes.
- `ProblemAlert`: renders an RFC 9457 error with its `code` and `request_id`.
- `RoleGate`: hides or disables write controls by permission (the server enforces them anyway).

## 4. UI state rules

- Every data view models `loading`, `empty`, `error` and `success` explicitly (finite-state UI,
  EFG §26).
- Mutations show pending state and are disabled while in flight. On `412 VERSION_CONFLICT`, show
  "This case changed — reload" with a diff of what changed.
- SSE connection status appears in the header (connected / reconnecting).
- The phone shows probe screens in the persona's language, using the variant text sent by the
  server. App chrome is English only.

## 5. Accessibility

WCAG 2.2 AA: keyboard-operable everything, visible focus, labels on all inputs, contrast ≥ 4.5:1,
no information by colour alone (hypothesis bars carry text labels), `aria-live="polite"` for
decision announcements. Playwright runs `@axe-core/playwright` on every route (no serious or
critical violations).

## 6. Performance budget

Initial JS ≤ 300 KB gzipped, via route-level code splitting (the Chase graph and charts are lazy
loaded). LCP ≤ 2.5 s on the demo server from a residential connection.
