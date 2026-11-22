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

**M5 case queue and Navigator console (#105):** `/cases` gains an order control (soonest SLA or
newest, `GET /cases?sort=`). The expected-loss column can now be filled: the backend stores an
open case's recommendation and risks from the moment it opens (#208), and `GET /cases` sorts by
`expected_loss`; the campaign column arrives with campaigns (#107). The console does not show
the police alert yet (its own issue). `/cases/:id` follows doc 09 §2 from `GET /cases/{id}`:
- the posterior `HypothesisBar` and the `RiskTable`;
- the "Evidence sufficient — recommend {decision} (expected loss ₹X)" banner while
  `evidence_sufficient` and not disposed;
- the next-best-evidence rows in the server's order, with the "Protocol" badge, minutes, "X% of
  remaining uncertainty" and net value;
- "What would change my mind";
- one `EvidenceCard` per record, whose narrative is labelled "AI-written summary" (`LLM`) or
  "Template summary". Its `[record_id]` citations render as chips that scroll to, focus and
  highlight that record's fact table (a citation to a record not on the page is plain text).
  Facts named `*_paise` show as ₹ and `*_share` as a percentage;
- loading and error states; a disposed case shows "(decision taken)" in the risk table and
  neither the banner nor the next-best evidence.

`evidence.narrative_ready` invalidates the `['cases']` queries, like `case.*`. The console's write
controls (fetch, the outreach call, the disposition and its 412 handling) follow in part 2.

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
for the projector (Escape returns). Until the Guardian API (#98) lands, the phone's endpoints are
typed by hand in `src/api/phone.ts` from doc 08 §4 and doc 16, and the page says so when they
404.

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
