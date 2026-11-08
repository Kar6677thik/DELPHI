# 01 — Vision and scope

## 1. Problem

Fraud systems produce a **risk score** and map it to a fixed action (allow / OTP / hold / block).
That breaks in exactly the cases that matter most now (research §1, §5):

1. **Coerced victims pass 2FA.** In authorised push payment (APP) scams such as digital arrest
   or investment scams, the genuine customer pays from their own device. OTP and biometrics tell
   the bank almost nothing.
2. **Holds are becoming mandatory and liability is shifting to banks.** Banks must hold more,
   yet every hold must be specific and reasoned (research §6). A score cannot resolve a hold.
   Only new evidence can.
3. **Investigators drown in alerts.** Existing copilots summarise. None picks the single most
   useful piece of evidence to fetch next.
4. **Held and blocked transactions never get labels.** Models stay blind exactly where the bank
   intervened (the selective-labels problem).

## 2. Product vision

DELPHI is a **decision layer above detection**. For every risky payment, case or loss it keeps
an explicit belief over competing hypotheses, and chooses the action with the highest
**value of information net of cost** (research §9.2). It records every step, so each decision
comes with a due-diligence trail.

## 3. Goals of this PoC

| ID | Goal | Measured by |
|---|---|---|
| G-1 | Show that choosing probes by value of information beats score-plus-fixed-friction on coerced-victim cases | Coercion catch rate, genuine-hold rate (doc 22) |
| G-2 | Resolve holds in seconds to minutes, with an auditable trail | Hold resolution time, due-diligence completeness |
| G-3 | Give investigators "next best evidence", not summaries | Uncertainty reduced per investigator minute |
| G-4 | Recover more money after a loss by ranking liens on expected recoverable ₹ and freezing only the traced amount | Golden-hour recovery ratio, tracing delay |
| G-5 | Learn from probe outcomes and fix selective-label bias | Label coverage, likelihood-table convergence |
| G-6 | Discover a previously unknown fraud pattern live | Unknown-pattern time-to-typology |
| G-7 | Run as a live, public, continuously deployed demo on the owner's home server | Push to `main` is live within 15 minutes |

## 4. Non-goals (explicitly out of scope)

- Integration with any real bank, payment switch, telco, I4C, DoT FRI, NPCI, DPIP or CFCFRMS
  system. Every external system is **simulated behind a port** (doc 04), so a real adapter can
  replace it later.
- Real personal data. All customers, accounts and payments are synthetic.
- Sending real police alerts, STRs or CFCFRMS tickets. They are drafted and simulated only.
- Production scale, high availability, multi-tenancy, multi-region.
- A native mobile app. The "mobile banking app" is a mock phone screen in the web UI.
- These hypotheses: first-party fraud and rogue AI agent. They are roadmap items. The KB format
  supports them, but KB v1 does not ship them.
- Graph neural networks, transaction foundation models, federated learning, and ARGUS-style
  adversarial mutation. These are roadmap items.
- Imitation learning on investigator traces. The PoC logs traces and reports analytics only.
- PDF generation. The due-diligence pack is printable HTML plus JSON.
- Legal advice. The regulatory parameters (hold windows etc.) are configuration, not legal
  interpretation.

## 5. Personas and roles

| Persona | Role in app | Uses |
|---|---|---|
| **Presenter / demo driver** (the team) | `ADMIN` | Runs demo scenarios, resets the demo, drives the mock phone |
| **Fraud investigator** | `INVESTIGATOR` | Navigator case console, Chase approvals, Guardian monitor |
| **FDE engineer** | `FDE_ENGINEER` | Typology Scout, KB drafts, learning runs, promotions, question bank review |
| **Judge / guest** | `VIEWER` | Read-only access to every screen; cannot change state |
| **Bank customer** (simulated) | none (persona on the mock phone) | Initiates payments, answers probes |

## 6. Demo stories (the acceptance backbone)

These stories come from research §12 and drive the milestones. Each one is an automated
end-to-end test (doc 21).

| ID | Story | What must happen |
|---|---|---|
| S-1 | **"Kamala-amma under digital arrest."** Kamala (68, Telugu) sends ₹4,80,000 to a new payee at 23:40 while on a call with an unknown number. She has been coached to say it is for family | Baseline B2 releases it after OTP passes. DELPHI's passive probes (call status) raise coercion. The planner asks the purpose question; Kamala gives the coached "family" answer. The planner then picks the digital-arrest spell-breaker (shown in Telugu) and observes hesitation and an app switch. `APP_DIGITAL_ARREST` becomes the leading hypothesis and DELPHI decides `PROTECTIVE_HOLD`; OTP is never asked. A case opens with `CUSTOMER_OUTREACH_CALL` pinned first, and the outreach call confirms coercion. The due-diligence pack is complete |
| S-2 | **"Arjun paying a hospital from a new phone."** ₹1,80,000 to a known hospital merchant, from a device first seen today | Baseline B1 holds it. DELPHI's passive probes find a clean, known beneficiary. The first active probe is `BIOMETRIC_STEP_UP` (OTP is never asked: one step-up per session), which passes. At most one more low-friction probe runs, and DELPHI decides `RELEASE` within 30 s of initiation |
| S-3 | **"The money already left."** A released ₹3,00,000 scam payment is reported 10 minutes later | Chase traces the taint across mule layers and ranks liens by net expected recoverable value. The investigator approves the top 3, each for the traced amount only, and CFCFRMS tickets are drafted. The UI shows ₹ recovered vs ₹ lost inside the golden hour while the mule network keeps moving money |
| S-4 | **"The reveal."** UPI collect-request "refund" scams fit no known typology | Their cases end with high `UNEXPLAINED` mass. The Scout clusters them and proposes a candidate. An FDE engineer names it, enables the `COLLECT_INTENT_CHECK` probe, backtests and shadow-runs it, and a second user approves it. New cases are then attributed to the new typology |
| S-5 | **"The results table."** | The evaluation page shows B1 (static rules), B2 (score + fixed friction) and DELPHI side by side on the metrics in doc 22, with confidence intervals |

## 7. Assumptions

| ID | Assumption | Consequence if wrong |
|---|---|---|
| A-01 | The home server runs k3s on Linux with the default `local-path` storage class, and DELPHI gets the budget in doc 23 §4 (≈ 1.5 GiB requested, ≈ 3.25 GiB at the limits) on a node shared with other apps (confirmed 2026-11-08) | Resize requests and limits (doc 23), or reduce the dataset profile. The `eval` profile does not fit and runs elsewhere |
| A-02 | Server CPU architecture is `linux/amd64` | Set repo variable `IMAGE_PLATFORMS=linux/arm64` (doc 23) |
| A-03 | The owner's domain is on Cloudflare, and the server's shared tunnel publishes `delphi.karthiksurkanti.in` (confirmed 2026-11-08) | Fall back to Tailscale-only access. Nothing else changes |
| A-04 | An Anthropic API key is available. The demo never depends on it, because the template fallback always works | Narratives use templates |
| A-05 | The GitHub repo `Kar6677thik/DELPHI` is **public** (confirmed 2026-11-02), so the GHCR package is public too | No image pull secret or registry credentials are needed in the cluster (doc 23 §4–6), and Pre-M0 item P-3 is dropped. If the repo is ever made private, add an image pull secret to the namespace |
| A-06 | In the simulation, the Chase loop has network-wide visibility of downstream hops (as DPIP/CFCFRMS data sharing would provide), delayed by a configurable latency | Chase would see only first hops; the ranking logic is unchanged |
| A-07 | MITRE F3 stage labels are a 4-stage subset: `INITIAL_ACCESS`, `POSITIONING`, `EXECUTION`, `MONETIZATION` (research §9.5). They are stored as configuration | Rename in config once checked against the official F3 matrix |
| A-08 | Telugu, Hindi and Tamil question translations are approved by a native speaker before being marked `APPROVED` | Unapproved languages fall back to English (FR-GDN-09) |
| A-09 | Regulatory parameters (60-day maximum hold, 20-day customer response window, 10-day bank decision window, 1-hour golden hour) follow the draft RBI mule SOP of 11 Sep 2026 and are configurable | Change config. No code change |

## 8. Success criteria for the PoC

The PoC succeeds when:

1. All five demo stories pass as automated e2e tests against the deployed build (doc 21).
2. The evaluation (doc 22) report is generated reproducibly from a fixed seed, and the headline
   targets are either met or honestly reported as not met.
3. Every push to `main` deploys automatically, and the deployment is verified (doc 23).
4. Milestones M0–M9 are closed against their exit criteria (doc 25).
