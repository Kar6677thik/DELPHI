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

## 3. Shared components (single implementation each)

- `HypothesisBar`: horizontal stacked bar. Fixed colour per hypothesis (doc 07 §4). Every
  segment ≥ 4% shows its label and percentage; smaller segments are grouped into "other" with a
  tooltip. A `timeline` variant stacks one bar per step.
- `CandidatesTable`: source, eligible/reason, cost ₹, VoI ₹, net value ₹, settle %, selected
  badge (GREEDY/EXPLORE + propensity).
- `RiskTable`: decision → expected loss ₹, with the recommended decision highlighted.
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
