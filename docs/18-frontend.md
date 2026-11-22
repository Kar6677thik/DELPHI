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
