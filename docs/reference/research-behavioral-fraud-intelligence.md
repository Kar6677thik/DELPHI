# Behavioral Fraud Intelligence — Deep Research, Gap Analysis & a Winning Idea
### Virtusa Jatayu Season 6 · Use Case 2 (FDE — Behavioral Fraud Intelligence)
*Prepared 30 Oct 2026, revised the same day after a second research round (§17), which filled the gaps, verified the India figures and ran a patent check. Companion to the ChatGPT "ARGUS" report, not a copy of it: it covers different ground, checks ARGUS's novelty claims against prior art, and proposes a different flagship.*

> ## DELPHI
> ### **D**ynamic **E**vidence-**L**ed **P**robing of fraud **H**ypotheses & **I**nterventions
> *The Oracle that answers by asking the right question.*
> The name works three ways. Delphi was the Greek oracle that people consulted to find the truth before acting. The Delphic maxim "Know thyself" maps onto behavioural baselines. And the **Delphi method** (RAND, 1950s) is the standard technique for eliciting structured judgements from experts, which is exactly how DELPHI builds its likelihood tables from fraud investigators in FDE workshops. Naming rationale and rejected alternatives are in §18.

---

## Contents
1. [TL;DR — the idea in 60 seconds](#1-tldr)
2. [How this research was done (and its limits)](#2-method)
3. [How fraud actually flows today — attack side and defense side](#3-flow)
4. [What the industry already does (what judges will consider table stakes)](#4-landscape)
5. [What engineering teams admit is still unsolved](#5-unsolved)
6. [India in late 2026: the regulatory earthquake nobody is pitching for](#6-india)
7. [Novelty check — what is crowded, what is open (including ARGUS)](#7-novelty)
8. [Idea portfolio — 8 candidates, scored](#8-portfolio)
9. [**Flagship: DELPHI — fraud intelligence that asks the right question**](#9-viveka)
10. [Architecture, agents and the FDE learning loop](#10-architecture)
11. [Metrics nobody else will show](#11-metrics)
12. [Stage 2 PoC plan (4 weeks) + demo script](#12-poc)
13. [Draft content for the 1-slide template](#13-slide)
14. [Judge Q&A prep](#14-qa)
15. [DELPHI vs ARGUS — and how to merge them](#15-argus)
16. [Sources](#16-sources)
17. [**Round 2 — gap-fill research (patents, verified India facts, global regulation, remaining vendors and blogs)**](#17-round2)
18. [Naming — why DELPHI, and alternatives considered](#18-naming)

---

<a name="1-tldr"></a>
## 1. TL;DR — the idea in 60 seconds

**The insight from the research:** every fraud system on the market — Featurespace/Visa, Feedzai, BioCatch, FICO, Sardine, NICE Actimize, SAS, Mastercard — answers one question: ***"How risky is this?"*** They produce a **score**. The score is then mapped to a fixed action: allow, OTP, hold, block, alert.

That model is breaking for three reasons the research makes very clear:

1. **The fastest-growing fraud is authorized by the genuine customer.** Examples are digital-arrest, investment and impersonation scams, known together as authorised push payment (APP) fraud. The real customer uses their real device and passes 2FA. So step-up authentication, the default response to a high score, gives **almost no information** in exactly the cases that matter.
2. **India is moving to make "hold first, verify later" mandatory, and to make banks liable when they get it wrong.**
   - At the **29 Oct 2026** hearing of the digital-arrest case (*suo motu*, SMW (Crl.) 3/2025), the Supreme Court bench **proposed** that RBI rules require banks to:
     - **stall unusual transactions until their genuineness is verified**;
     - **alert the local police**;
     - **compensate victims at least the amount lost** where due diligence failed. Justice Bagchi called this "strict liability".
   - These are *proposed* directions, not yet a final order. The court asked RBI to notify the KYC Amendment Directions by **30 Nov** and bring them into force by **31 Dec 2026**.
   - The RBI's draft money-mule SOP (11 Sep 2026) allows **debit holds of up to 60 days**, with no compensation for wrongful holds.
   - High Courts are already striking down blanket freezes:
     - Delhi HC, *Malabar Gold*, Jan 2026;
     - Rajasthan HC, 105 petitions, Aug 2026: a lien on only the disputed amount is the default, and every freeze needs recorded reasons.
   - Banks are therefore squeezed from both sides: hold more, but justify every hold. **A score cannot resolve a hold. Only new evidence can.**
3. **Investigators drown in alerts.** Investigation agents now exist (Feedzai Farol, Unit21, NICE, FIS+Anthropic, Lucinity), but they *summarize*. None of them decides **which single piece of evidence to fetch next** to settle a case fastest.

**The idea — DELPHI** (*Dynamic Evidence-Led Probing of fraud Hypotheses & Interventions*):

> **Every fraud system asks "how risky is this?". DELPHI asks "what is the one question, check or action that would most quickly reveal the truth, at the lowest cost to an innocent customer?"**

DELPHI treats every risky payment, alert and case as a contest between **competing hypotheses**:
- Genuine
- Account takeover
- Coerced victim (APP / digital arrest)
- Money mule
- First-party fraud
- Rogue AI agent
- ***Unknown pattern***

It then chooses its next action by **expected information gain per unit of customer friction** (formally, *value of information* from Bayesian decision theory). The same principle drives three loops:

| Loop | When | What DELPHI decides | Replaces |
|---|---|---|---|
| **Guardian** | Payment time (seconds) | Which *probe* to run: call-status check, beneficiary mule lookup, screen-share check, a scam-specific "spell-breaker" question, a cooling-off delay, a trusted-person callback | Blind OTP or a blanket hold |
| **Navigator** | Investigation time (minutes) | Which *evidence* the investigator or agent should pull next | "Here's a summary of 40 data points" |
| **Chase** (the Jatayu loop) | After money moves (golden hour) | Which downstream accounts to freeze first, and for **how much**, ranked by expected *recoverable* rupees | Bank-by-bank manual lien tickets and blanket freezes |

**Why it is new.** A second-round patent and paper check (§17.1) opened the closest patents: EMC (2013), BioCatch (2010, 2017), Callsign (2022), JPMorgan (2004), Vocalink (2018) and Micro Focus (2018). It found **nothing that keeps an explicit posterior over typed fraud hypotheses** (genuine / ATO / coerced victim / mule / first-party / rogue agent / unexplained) **and picks the next probe, evidence request or freeze by expected value of information net of cost**. The individual ingredients do exist and are cited honestly in §7 and §17.1:
- challenge selection by threat type (EMC);
- coercion-priming challenges with timing analysis (Callsign);
- information-gain test selection (active diagnosis literature).

**The novelty is the unified decision engine, not any single probe.**

**The technical bonus (worth a slide).** Every probe outcome is also a **label for a decision that would otherwise never get one**. Blocked or held transactions never reveal whether they were really fraud. This "selective labels" problem is one Stripe, Mastercard and academia all call open. DELPHI's probes, plus a small randomised exploration budget with logged probabilities, give the bank **unbiased labels for its held and blocked decisions**. The system gets smarter precisely where today's systems go blind.

**The FDE angle (the use-case theme).** The likelihood tables are elicited from **investigators in FDE workshops**. They encode statements like "how often does a genuine customer hesitate more than 8 seconds on this question?" or "how often is a digital-arrest victim on a live call?". Real outcomes then update them continuously. Investigators' expertise becomes the model's prior, and their decisions become its training signal.

**The 10-second explanation — the Judgment of Solomon.** Two women claim the same baby. Solomon doesn't hunt for more documents. He proposes a test (*"cut the baby in half"*) that the true mother and the impostor will **react to differently**. That is a probe chosen because its outcome likelihoods differ sharply between the competing hypotheses. DELPHI does this systematically, thousands of times a day, at the lowest possible friction.

**The story hook for Virtusa judges.** In the Ramayana, the golden deer (Maricha) was an illusion built to lure a guarded person across the **Lakshman Rekha** *voluntarily*. That is the first recorded authorized-push-payment scam. The Rekha (rules, 2FA) held against force but not against deception. **Jatayu** was the one who intercepted the abduction and told Rama *which way the money went*. DELPHI's Chase loop is named the **Jatayu loop** for that reason.

---

<a name="2-method"></a>
## 2. How this research was done (and its limits)

**Method.** Five parallel research tracks ran on 30 Oct 2026, with roughly 330 web searches and page fetches in total:

1. In-house engineering blogs and papers: Stripe, Uber, Grab, Nubank, Monzo, Capital One, Meta, LinkedIn, PayPal, Ant Group, Pinterest, Revolut, Amex and others.
2. Commercial vendors, new agentic-AI investigation tools, market size, and Virtusa's own footprint.
3. India's fraud data, regulators and infrastructure (RBI, I4C, NPCI, DoT), plus global regulatory shifts.
4. The academic frontier and a novelty check of 10 idea components, against papers, patents and products.
5. Ideas transferable from other domains: cybersecurity, scam-baiting, epidemiology, game theory, process mining, agentic commerce.

**I personally re-verified the four claims the pitch depends on**, by opening their source pages:
- MITRE F3 (9 Apr 2026)
- RBI draft mule SOP (11 Sep 2026)
- Apate.ai/CommBank (TechCrunch, 14 Oct 2026)
- The Supreme Court order (29 Oct 2026)

**Limits — be honest about these in front of judges:**
- In round 1 the shared web-search budget ran out before Amazon, Tencent, Lynx, Effectiv, Resistant AI and a few others were covered. **Round 2 (§17) filled most of these gaps**, verified the India figures and added a patent check.
- Some EU, Australia and US regulatory details came from prior knowledge and are marked *[unverified]*.
- Almost all vendor metrics are **self-reported**. I found **no independent benchmark of any AI fraud-investigation agent**, and that absence is itself a finding.
- Some India figures come from a single secondary source; these are flagged. Check the PIB or Parliament original before putting a number on the slide.
- **"Nobody has ever thought of this" cannot be proven.** What I can say is: *"Across ~330 searches of vendors, papers and patents, we found no system that does X; the closest are A, B and C."* That is a stronger statement to make to judges anyway.

---

<a name="3-flow"></a>
## 3. How fraud actually flows today

### 3.1 The attack side: a typical Indian digital scam in 2026

```
HOOK ──► AUTHORISED PAYMENT ──► LAYER-1 MULE ──► LAYERS 2..N ──► CASH-OUT
call / WhatsApp /   victim pays from        rented / insider-    split across       ATM, POS, cheque,
deepfake video /    OWN device, OWN SIM,    opened account       banks, wallets,    or cash→USDT via
"CBI officer"       passes 2FA              (one Delhi account:  PAs within         P2P agents → offshore
                                            10,000+ txns,        minutes
                                            ₹5.24 cr in 5 days)
```

- **Why bank detection fails at step 2:** the payment *is* authentic. Device, OTP, biometrics and location all check out. The signal is not *who* is paying but **whether they are acting freely**. Coercion shows up in other signals:
  - an active call with an unknown number;
  - a screen-sharing or remote-access app running;
  - hesitation;
  - switching to WhatsApp mid-flow;
  - an unusual hour;
  - a new payee combined with a large amount.
- **Why detection fails at step 3:** the receiving bank sees an ordinary account until complaints arrive. Insiders also defeat KYC: bank-staff arrests in Oct 2025, Jun 2026 and Oct 2026 included one co-op bank account linked to 159 complaints and about ₹68 cr.

### 3.2 The defense side: inside a bank's fraud operation (generic, all banks)

```
Real-time scoring (rules + ML) ──► Alert queue (thousands/day, 90-95% false in rule-based AML)
      │                                   │
      ▼                                   ▼
Fixed action map                   L1 triage → L2 investigation → disposition → STR/SAR
(allow / OTP / hold / block)              │                              │
                                          ▼                              ▼
                              Investigator clicks through       Label = final disposition only
                              5-15 screens; reasoning is        (reasoning lost; held/blocked
                              never captured                    items never get true labels)
```

### 3.3 The recovery side: India's national response

```
Victim calls 1930 ─► CFCFRMS ticket ─► victim bank → beneficiary bank ─► lien on remaining balance
                                         └─► ticket chases money layer by layer (human speed)
                                                                    │
                                                    restoration: <₹50k without court order (MHA SOP, 2 Jan 2026)
                                                                     >₹50k still needs court / Lok Adalat
```

### 3.4 Where time and money are lost (evidence)

| Gap | Evidence |
|---|---|
| Money moves in minutes; tickets move at human speed | Funds are split across layers within minutes, while the CFCFRMS ticket hops bank by bank |
| "Saved" is a fraction of what is lost | ₹11,158 cr "saved" by CFCFRMS (as of 30 Jun 2026) vs ₹55,050+ cr lost over 5 years (Lok Sabha, 21 Jul 2026) |
| A freeze is not a refund | MHA review meeting data (Indian Express, Jul 2026): 2021 to May 2026, **₹64,447 cr reported → ₹10,718 cr under lien → ₹323 cr refunded (<1%)**. H1 2026: 29% of losses put under lien. *(Cite as "reported in MHA review"; the often-quoted ₹9,079 cr / ₹206 cr figures have no primary source.)* |
| Blunt freezes hurt legitimate businesses | **Delhi HC, *Malabar Gold v. UoI* (16 Jan 2026)**: police cannot debit-freeze accounts under §106 BNSS; blanket freezes on non-suspects violate Arts. 19(1)(g) and 21. **Rajasthan HC, *Shree Balaji Enterprises v. RBI*** (105 petitions, Aug 2026): the default is a lien on the **disputed amount only**, with recorded reasons and periodic review. Cyberabad Police ran a "Defreeze Accounts Mela" (Feb 2026). One ₹4.43 cr Faridabad case reportedly froze ~36,000 accounts nationwide *(unverified)* |
| Official statistics miss scams | RBI counts **293 digital bank frauds / ₹29 cr** in FY26 (≥₹1 lakh threshold), while NCRP logged **24 lakh complaints / ₹22,495 cr** in 2025. Scams sit almost entirely outside banks' own fraud statistics |
| Fraud value sits in large payments | 98.5% of fraud value is in payments above ₹10k (RBI discussion paper, Apr 2026, via IANS) |

---

<a name="4-landscape"></a>
## 4. What the industry already does

### 4.1 What engineering teams built (primary technical posts)

| Company | What they built | Number worth quoting | What they admit is hard |
|---|---|---|---|
| **Stripe** | Radar moved from logistic regression to XGBoost + DNN to a pure DNN; 1,000+ features; <100 ms. Payments Foundation Model (2025) treats each charge as a token. LLM compliance agents with 100+ agents inside a DAG | Card-testing detection on large merchants went from 59% to 97%; compliance-agent handling time fell 26% | Labels arrive ~60 days late; blocked charges never get labels (fixed with **random holdback + inverse-propensity weighting**); DNNs are hard to explain; agents still need human evaluation |
| **Uber** | Project RADAR: anomaly detection on hourly loss time series → projects immature losses to maturity → **auto-generates short-lived rules** (FP-Growth) → analyst approves | — | Chargebacks arrive weeks later; pattern explosion; graph model (RGCN, +15% precision) **never reached production** |
| **Grab** | Graph of 1B+ nodes; **Spade** incremental dense-subgraph detection; **SOP-driven LLM agent** for ATO cases | Spade is ~10⁶× faster (µs per update); the agent cut cases from **22 to 3 minutes** and automates 87% of ATO cases | Shared-IP edges are noisy (mobile carriers); real-time graph updates are "heavy"; cold start for each new product |
| **Nubank** | nuFormer: GPT-style transaction model + DCNv2, trained jointly | +1.25% relative AUC ("~33× a typical launch") | **Labels mature at 6 months**; "a systems problem, not just ML" |
| **Monzo** | Controls written as pure functions (Starlark) that can be **backtested**; 3-tier features; multi-task neural network | ~30% better on rare fraud subtypes | "Whack-a-mole"; delayed and partial labels; drift |
| **Capital One** | ATLAS: a time-respecting session graph, GraphSAGE, 100M nodes, 1B edges | +6.38% AUC, >50% less customer friction | **Graph construction mattered more than GNN complexity** |
| **Meta / LinkedIn** | Classify accounts using their graph neighbours' features; cluster-level scoring with label propagation | Meta: a further 27% fewer abusive accounts | Too little signal at signup |
| **PayPal** | Real-time asset graph (Aerospike + Gremlin) | Detects the "ABABA" back-and-forth pattern | Needed 4+ self-joins in SQL |

### 4.2 What vendors sell (late 2026)

| Vendor | Core | Agentic layer | Gap we can exploit |
|---|---|---|---|
| **Visa + Featurespace** | Adaptive behavioural analytics, TallierLTM, A2A Protect (2026); **buying BioCatch for $2.4B** (Aug 2026) | Plain-language alert explanations | Signals and scores, no case reasoning; no mule or receiving-side tooling |
| **BioCatch** | Behavioural biometrics, mule detection, Trust network (AU, AR), DeviceIQ for detecting AI agents | — | Signals, not decisions; Trust network exists in only 2 countries |
| **Feedzai** | RiskOps, IQ Score (federated network score), ScamAlert | **Farol** (Sep 2026): finds noisy rules, summarizes alerts, drafts SARs | Unclear which actions run on their own; CPO: banks are "reluctant to hand over control" |
| **NICE Actimize** | X-Sight, IFM-X | Investigator agent plans from policy, assembles context "the moment an alert is opened" | Implementations take months; its own webinar warned of "faster broken workflows" |
| **SAS** | Fraud Decisioning; Forrester Wave Leader Q3 2026 | "Agatha" voice agent; investigation copilot | Agentic features are mostly demos or roadmap |
| **FICO** | Falcon consortium model, Focused Foundation Model, Scam Detection Score | None | Rule authoring is hard; false negatives are invisible |
| **Sardine** | Device + behaviour SDK, ~40M-parameter payments foundation model | Scheduled agents that write rules and audit dead ones | Strong in fintech, light in large banks |
| **Unit21 / Oscilar / DataVisor / Hawk / ThetaRay / Lucinity / Hummingbird / Silent Eight / FIS + Anthropic** | Various | Detection and investigation agents, SAR drafting | **Most bank-grade agents target AML, not scam/fraud.** FIS lists fraud as "a later step" |
| **India: Clari5 (Perfios), Bureau, RBIH MuleHunter.AI** | FRM, mule scores; MuleHunter is free and runs in 26–29 banks | Clari5 Genie (GenAI investigation assistant) | MuleHunter runs **per bank**, with **no published precision or false-positive data** |

### 4.3 Table stakes — **do NOT pitch any of these as your innovation**
- Real-time ML on top of rules; behavioural baselines per customer and device; velocity features
- Device fingerprinting and behavioural biometrics
- Graph / GNN fraud rings, shared device or IP detection
- Transaction foundation models (Stripe, Nubank, Featurespace, FICO, Revolut PRAGMA, Sardine)
- An LLM copilot that summarizes alerts and drafts SAR/STR narratives
- "Continuous learning from analyst feedback" (Unit21 claims it)
- Agents that recommend rules or thresholds (Farol, Sardine, Unit21, Uber RADAR)
- In-call / screen-share scam detection (Google Android, BioCatch, Telstra–CBA)
- Scam-baiting bots (Apate.ai, used by CommBank)
- A fraud ATT&CK taxonomy (**MITRE F3**, Apr 2026)

**These become DELPHI's *sensing layer*, presented as "we build on the state of the art", not as the USP.**

---

<a name="5-unsolved"></a>
## 5. What engineering teams admit is still unsolved

These recur across companies, in their own words:

1. **Label delay**: 60 days at Stripe, weeks at Uber, 6 months at Nubank; Monzo also cites it.
2. **Selective labels / feedback loop.** Whatever you block or hold *never reveals the truth*. Stripe's random holdback is the only production fix found. Mastercard's 2026 paper "Causal Label Recovery in Payment Networks" calls a declined transaction's fraud status "permanently unobserved."
3. **Adversarial adaptation**: Monzo's "whack-a-mole", Uber's "supervised models can't stop unseen patterns".
4. **Rule sprawl vs safe iteration**: Uber and Pinterest auto-expire rules; Monzo and DoorDash backtest and run rules in shadow first.
5. **Explaining decisions to analysts.** Stripe says explanation matters "nearly as much as detection". Yet a Feedzai field study found **SHAP/LIME explanations added no decision utility for real analysts**.
6. **Learning from *how* investigators reason, not just what they decide.** Open in research. Only security operations (ContextBuddy, AACT) learn from analyst actions.
7. **Evaluating AI agents.** No public benchmark. FraudBench (Aug 2026) found agents' resistance to attacks was only **49–65%**, with mule and first-party fraud the weak spots across models.
8. **Coerced customers (APP).** Peer-reviewed APP-scam work is "strikingly scarce"; the field is driven by vendors and regulators.
9. **Static friction fails against coached victims.**
   - A large online randomised trial run for Open Banking (CeDEx 2023-08) found redesigned call-to-action buttons (cancel, "call the bank") **dramatically** cut scam success, while **warnings had weak effects**.
   - UK Ombudsman decisions repeatedly show coached victims **lying about payment purpose** and getting through the bank's questions.

> **Unsolved items 2, 5, 6, 8 and 9 have a common root: the system observes passively and acts by fixed mapping.** It never *asks* in an optimised way, so it never *learns* from what asking reveals. That is the gap DELPHI is built on.

---

<a name="6-india"></a>
## 6. India in late 2026: the regulatory earthquake nobody is pitching for

| Date | Development | Implication |
|---|---|---|
| 25 Sep 2025 (effective 1 Apr 2026) | RBI Authentication Directions: issuers must run **risk-based authentication that factors in behavioural analytics** | Behavioural signals are now a compliance requirement, not just nice to have |
| 9 Apr 2026 | RBI discussion paper: **1-hour delay** on APP payments ≥₹10k, a "kill switch", a ₹25 lakh annual credit ceiling, a "trusted person" for customers 70+ | Friction is becoming mandatory, so friction must become *smart* |
| 13 May 2026 | I4C–RBIH MoU: Suspect Registry data to feed MuleHunter | National mule intelligence becomes something banks can query |
| 24 Jun 2026 (effective 1 Jan 2027) | Customer-liability amendment: covers *fraudulent* (not just unauthorised) transactions; **burden of proof on the bank**; 85% compensation up to ₹25k | Banks pay when they cannot *prove* due diligence |
| 11 Sep 2026 (verified) | **Draft mule SOP**: transactions ≥₹1,000 flagged by monitoring (AI/ML allowed) → **immediate debit hold up to 60 days**; 20 days for the customer to respond, 10 for the bank to decide; STR to FIU-IND; **no compensation for wrongful holds** | A massive wave of holds on mostly genuine customers is coming |
| 4 Aug 2026 | Supreme Court order: RBI to issue an SOP on temporary debit holds for mule-linked accounts; an inter-departmental committee to examine **shared liability and victim compensation** | Origin of the 11 Sep mule SOP |
| 18 Sep 2026 | DPIP (Digital Payments Intelligence Platform) piloting (7 banks per Rajya Sabha, 28 Jul; "8" per Deputy Governor, 18 Sep) via IDPIC. IDPIC is a Section 8 company incorporated 16 Oct 2025, owned 50:50 by SBI and BoB, ₹500 cr authorised capital, targeting operation by Dec 2026 | A new national risk signal to consume, not compete with |
| **29 Oct 2026 (verified; proposals, not final)** | **Supreme Court bench proposes**: stall **unusual** transactions until genuineness is verified (in writing; one report says physically); **alert the local police**; banks compensate **at least the amount lost** where due diligence failed ("strict liability"). The Attorney General opposed punishing banks. RBI to notify by 30 Nov; in force by 31 Dec 2026 | **Behavioural deviation is about to trigger a mandated hold, and the bank is liable both ways.** |

**The opportunity no one is pitching for.** Every bank in India is about to need a way to:
- resolve thousands of behaviour-triggered holds **in minutes, not days**;
- with **minimal harm to genuine customers**;
- and an **auditable evidence trail proving due diligence**.

Scoring vendors give a score. Nobody gives a *resolution procedure*. DELPHI is that procedure.

**National infrastructure DELPHI plugs into (as probes, not competitors):**
- **DoT FRI**: mobile-number risk; ₹5,043 cr protected by Aug 2026.
- **I4C Suspect Registry**: 32 lakh mule accounts; ₹25,698 cr declined.
- **RBIH MuleHunter.AI**, **NPCI fraud monitoring**, **DPIP** (when live), **CFCFRMS** (for the Chase loop).

**Global tailwinds** (the same direction everywhere: more liability on banks, receiving banks pulled in):
- UK mandatory APP reimbursement (Oct 2024): split 50/50 between sending and receiving banks; 88% of value reimbursed; ~£73m a year less APP fraud.
- Singapore Shared Responsibility Framework (Dec 2024).
- EU Verification of Payee (Oct 2025).
- Australia Scams Prevention Framework (2025) *[unverified details]*.

---

<a name="7-novelty"></a>
## 7. Novelty check

**Verdicts:** *Crowded* (don't lead with it), *Partial* (prior art exists, but our specific form is open), *Open* (no close prior art found).

| Concept | Closest prior art found | Verdict |
|---|---|---|
| Adversarially mutate confirmed fraud to find blind spots ("evasion distance") — **ARGUS's core** | **US patent 12597034** (evolution-based black-box attack, then retrain); **US 12511659** (GAN generates laundering patterns that evade AML rules); FRAUD-RLA (2025, RL attacker); TU Delft subgraph perturbation thesis; Neovera "Fraud Red Team" (manual service, 25+ of the top 100 banks) | **Partial — and patented.** ARGUS's headline has real prior art; judges with domain depth may know it |
| Partial-trajectory / prefix detection — **ARGUS** | Early time-series classification (TMLR 2025 survey); predictive process monitoring; per-event foundation models (Stripe, nuFormer) | Partial |
| "What evidence would falsify this" explanations — **ARGUS** | Torres et al. 2026: supporting vs contradicting evidence, 0.98 citation validity | Partial (close) |
| LLM agents writing and backtesting rules | Unit21 Rule Recommendation Agent (shadow backtests), Sardine, Uber RADAR, SAGE | **Crowded** |
| **Choosing interventions to discriminate competing fraud hypotheses (value of information)** | Patents and products:<br>• **EMC US9639689** (2013): infers attack type and picks a matching challenge; threshold-based, no posterior.<br>• **EMC US9122866** (2013): cost-effectiveness challenge choice; notes SMS is pointless if the device is compromised.<br>• **BioCatch US9558339** (2010): picks the UI "interference" that best discriminates owner vs attacker.<br>• **Callsign US11531735** (2022): challenge when the score is in the uncertain band.<br>• **Airbnb "Targeted Friction"**: friction cost model of good-user dropout vs chargebacks, A/B tested.<br>Research: RLAuth; active-diagnosis entropy test selection (Zheng & Rish). | **Ingredients exist; the combination is open.** No system found that picks probes by expected value of information over a *multi-way* fraud-type posterior (incl. coerced victim, mule, rogue agent, unexplained), net of cost |
| Behavioural scoring of scam "spell-breaker" questions | **Callsign US11531735, claims 2–3 & 6–9**: priming content to trigger coerced behaviour, scored on timing, cadence and jitter, plus remote-access detection. **High overlap; in force until ~2042.** BioCatch US10970394 (passive vishing/duress detection); Revolut questionnaire flow (2024) | **Not novel on its own; freedom-to-operate risk.** Use only as one probe *inside* the value-of-information engine; never pitch it as the invention |
| Recovery prioritisation by amount × success | **JPMorgan US7480631** (2004) claims "prioritizing… fraud recovery… based on fraud amount and estimated success rate"; Vocalink US10965574 (payment-chain tracing) | Abstract idea is prior art. **Still open:** per-hop, real-time calculation on the payment graph (traced balance × P(freeze lands before the next hop) × P(mule)) |
| Investigator next-step recommendation | Micro Focus US11544374 (2018): imitates senior analysts' next queries (security); TU Delft/ING thesis on POMCP planning for AML investigations (2020); NICE/Hawk "next best action" (LLM or policy) | Crowded as a concept; value of information over typed hypotheses is not found in vendor material |
| **Learning from investigator reasoning traces** | Security operations only (ContextBuddy, AACT imitation learning); patent 11334878 (weights analyst labels by seniority); Celonis process-mining consulting | **Open** for bank fraud |
| **Golden-hour forward-flow prediction, freezes ranked by recoverable value** | Pay.UK/Vocalink MITS (dispersion trees, no forecasting); I4C CFCFRMS (manual); Kumo (7-day withdrawal prediction) | **Partial → open** for "expected recoverable ₹ under a time limit" |
| **Probes as label generators for held/blocked decisions** | Stripe random holdback (Manapat, PyData 2015; lets fraud through, so costs money); Mastercard Causal Label Recovery (2026, offline, no probes); SMS "was this you?" confirmation patents (not framed as fixing label bias) | **Cleanest technical novelty found** — getting propensity-logged labels via *customer-protective* probes with randomised probe *choice*, rather than letting fraud through |
| Fraud breach-and-attack simulation mapped to MITRE F3 | F3 itself (Apr 2026) lists emulation and data sources as future work; AttackIQ/SafeBreach are cyber only | Open (but overlaps ARGUS) |
| Mule epidemiology (R per cluster, ring vaccination, tracing-delay KPI) | SIR models for fund-raising fraud; MITS trees | Framing open; underlying technique (label propagation) crowded |
| Know-Your-Agent behavioural baselines (agentic payments) | AP2 mandates, Visa Trusted Agent Protocol, Mastercard Agent Pay (identity only); BioCatch DeviceIQ (classifies sessions) | Open, but early market |
| Bank-owned honey-payees / decoys | Apate (telco honeypot phone numbers), Thinkst card canaries, CYBERA mule intelligence | Open but **legally risky** (KYC fiction, entrapment) |

> **Takeaway.** ARGUS's centrepiece (adversarial mutation) is the most crowded and patented of its components. The *largely open* zone is **active, hypothesis-driven resolution**: the action-selection layer that sits *after* detection. Nobody has staked a claim there, and it is exactly where India's new regulations create urgent demand.

---

<a name="8-portfolio"></a>
## 8. Idea portfolio — 8 candidates, scored (1–5)

| # | Idea | Novelty | India / regulatory pull | Judge "wow" | 4-week PoC feasibility | FDE fit | **Total** |
|---|---|---|---|---|---|---|---|
| **1** | **DELPHI — hypothesis-driven, value-of-information resolution (Guardian + Navigator + Chase)** | 5 | 5 | 5 | 4 | 5 | **24** |
| 2 | ARGUS (ChatGPT) — adversarial mutation + trajectory risk | 3 | 3 | 5 | 3 | 4 | 18 |
| 3 | **F3 Purple Team** — continuous fraud-control attack simulation, detection-as-code, F3 coverage heat-map | 4 | 3 | 4 | 4 | 5 | 20 |
| 4 | **Golden-Hour Chase** (standalone) — forward money-flow forecasting + targeted lien on only the traced amount | 4 | 5 | 4 | 4 | 3 | 20 |
| 5 | **Investigator Apprenticeship** — imitation learning on investigator clickstreams + process mining | 4 | 3 | 3 | 3 | 5 | 18 |
| 6 | **Kill-chain Campaign Fusion** — merge alerts into campaigns by F3 stage progression; one case per campaign | 3 | 3 | 4 | 4 | 4 | 18 |
| 7 | **Mule Epidemiology** — R per mule cluster, "ring vaccination" friction on graph neighbours, tracing delay as KPI | 4 | 4 | 4 | 4 | 3 | 19 |
| 8 | **Know-Your-Agent** — behavioural baselines for AI agents paying on users' behalf; mandate drift | 4 | 2 | 4 | 3 | 3 | 16 |

**Recommendation:** lead with **#1 DELPHI**. It *absorbs* the strongest parts of #4 (Chase loop), #5 (Navigator learns from traces) and #6 (cases merge into campaigns) under one principle, so the pitch is a single idea and not a grab-bag. #3 or ARGUS can appear as a roadmap item (§15).

---

<a name="9-viveka"></a>
## 9. Flagship: **DELPHI**
### *Hypothesis-driven fraud intelligence that asks the right question*

### 9.1 Problem statement (elevator pitch)
Fraud systems tell banks *how risky* a payment is, but not *what to do next to find out the truth*. They respond with the same fixed friction every time. As a result:
- coerced scam victims pass OTPs;
- genuine customers get frozen for days;
- investigators click through dozens of screens;
- held transactions never produce the labels that would make models better.

India is moving to *mandate* holds on unusual transactions and to make banks liable for due-diligence failures: the Supreme Court bench's proposals of 29 Oct 2026, the RBI mule SOP and the January 2027 liability rules. At the same time, High Courts demand that each freeze be specific and reasoned. Banks need a system that **resolves uncertainty fast, fairly and auditably**.

### 9.2 Core principle — value of information (VoI)

At any moment DELPHI holds a **belief** over competing hypotheses for a payment, account or case:

```
H = { Genuine, Account-Takeover, Coerced-APP(digital-arrest | investment | impersonation),
      Mule-inbound, First-party, Rogue-AI-agent, ∅ Unexplained }
```

It has a catalogue of **actions** (probes, evidence fetches, freezes). Each action has:
- a **cost** (customer friction, delay, ₹ of money at risk, investigator minutes);
- a **likelihood table** `P(outcome | hypothesis)`.

It picks the action that most reduces the expected cost of a wrong decision, minus what the action itself costs:

```
Bayes risk:        R(p) = min_d  Σ_h p(h) · Loss(d, h)          d ∈ {release, hold, block, escalate}
Value of action:   VoI(a) = R(p) − E_o[ R(p(· | o, a)) ]
Choose:            a* = argmax_a [ VoI(a) − Cost(a) ]
Stop when:         max_a [ VoI(a) − Cost(a) ] ≤ 0   → take best decision d, with full evidence trail
```

This is textbook Bayesian decision theory (Bayesian experimental design / optimal stopping), so it is defensible in front of technical judges. **Applying it to choose fraud interventions across competing fraud hypotheses is the new part.**

`Loss(d, h)` is where business reality enters:
- Releasing coerced-APP loses about `amount × (1 − recovery rate)` plus liability under the new rules.
- Holding a genuine customer costs about `harm + complaint + churn`, scaled by amount and the customer's vulnerability.

### 9.3 Worked example — why "high score → OTP" fails, and how DELPHI decides

A 68-year-old customer initiates ₹4,80,000 to a new payee at 11:40 pm. The detection layer gives a calibrated prior: **Genuine 0.55 · Coerced-APP 0.35 · ATO 0.10**. *(The likelihoods below are illustrative; in deployment they are elicited from investigators and then learned.)*

| Probe | P(outcome \| Genuine) | P(… \| APP) | P(… \| ATO) | Posterior after probe (G / APP / ATO) |
|---|---|---|---|---|
| **OTP / biometric step-up** → "passed" | 0.98 | 0.97 | 0.30 | **0.59 / 0.37 / 0.03** — barely moves G vs APP. *Useless against coercion.* |
| **Call-status check** → "on a live call with an unknown number" | 0.08 | 0.85 | 0.10 | **0.13 / 0.85 / 0.03** |
| …then **digital-arrest spell-breaker question** → ">8 s hesitation + switched to WhatsApp" | 0.05 | 0.60 | 0.20 | **0.01 / 0.98 / 0.01** → hold + trusted-person callback + police alert |

**The key point for the slide:** DELPHI knows *before running it* that OTP has near-zero information value when the competing hypotheses are Genuine vs Coerced. Industry's default response is the least informative one available.

The mirror case matters just as much. A genuine customer paying a hospital abroad from a new phone gets:
- a passive beneficiary lookup (clean, a known hospital merchant);
- a biometric step-up, which *is* informative there because the competing hypothesis is ATO.

Result: **released in about 20 seconds instead of a 1-hour or 60-day hold.**

### 9.4 The probe catalogue (payment-time — the Guardian loop)

| Probe | Friction | Most discriminates | Data source |
|---|---|---|---|
| Beneficiary intelligence lookup | 0 | Mule vs Genuine | I4C Suspect Registry, MuleHunter, DPIP, internal graph |
| Counterparty phone risk | 0 | APP vs Genuine | DoT FRI API |
| Device posture (screen-share, remote-access app, accessibility abuse) | 0 | APP / ATO vs Genuine | Mobile SDK, Android in-call protection signals |
| Call-status (on a live call? unknown number?) | 0 (consented) | **APP vs Genuine** | On-device / telco signal (cf. CBA–Telstra Scam Indicator) |
| Session-behaviour replay (hesitation, copy-paste of account number, typing cadence) | 0 | ATO, APP | Behavioural SDK |
| Structured purpose question + **response behaviour** | Low | APP subtype | In-app |
| **Scam-specific spell-breaker question** (in the customer's language: Hindi, Telugu, Tamil…) | Low–Med | Coerced vs Genuine | LLM-phrased, template-constrained |
| Biometric / OTP step-up | Low | **ATO vs Genuine** (not APP) | Existing |
| Receiving-bank pre-credit check (like UK EDEx) | 0 | Mule | Bilateral API / DPIP |
| Cooling-off delay, watching for **persistence** (retries via other channels, raising limits) | Med | APP (scammers push urgency) | Core / channel logs |
| Trusted-person notification (RBI's 70+ proposal) / bank callback from a verified number | High | Coerced | Contact centre |

**Content vs behaviour.** Coached victims lie about payment purpose (Ombudsman evidence), so DELPHI scores **how** they answer, not just **what** they answer: hesitation, switching apps to consult the scammer, implausibly fast "correct" answers. It also rotates question variants so scammers can't script for them. The CeDEx randomised trial shows call-to-action redesigns beat warnings, so probes double as *interventions*. Cash App's numbers support this: scam warnings appear on ~1.5% of P2P payments, and ~50% of warned payments are abandoned.

> ⚠️ **Prior-art note.** Behavioural scoring of coercion-priming challenges is claimed in **Callsign US11531735** (claims 2–3, 6–9; in force to ~2042). In the pitch, the spell-breaker is *one probe among many*. DELPHI's contribution is **choosing** which probe to run, through per-hypothesis likelihoods and a posterior, and feeding the result into a multi-way decision. For a real product, get a freedom-to-operate review; for the ideathon, just cite it, which shows research maturity.

### 9.5 The Navigator loop (investigation time)

For each case, the investigator console shows:
- **Hypothesis bar**: Mule 0.61 · Genuine-merchant 0.27 · Unexplained 0.12.
- **"Next best evidence"**, ranked by VoI, with the expected time to fetch. For example: *"Check whether the beneficiary's device appears on other accounts — expected to settle 58% of remaining uncertainty (≈20 s)."*
- **A one-click agent fetch.** An LLM agent retrieves the evidence and writes a **cited** finding, and the posterior updates live.
- **"What would change my mind"**: the single most discriminating piece of missing evidence. This is better than generic "contradicting evidence" lists (Torres 2026).
- **An auto-assembled due-diligence pack**: every probe, every piece of evidence, every posterior change, timestamped. This is the audit trail that the proposed due-diligence liability standard, and the High Courts' "recorded reasons" requirement, will demand. It also pre-fills the STR and NCRP/CFCFRMS forms.

**Learning from reasoning, not just verdicts.** DELPHI logs *which* evidence each investigator opened, in what order, and when they became confident. This lets it:
- update the likelihood tables (Dirichlet/Bayesian counts);
- learn which evidence the best investigators rely on (imitation learning — open in fraud research; precedent in security operations with ContextBuddy and AACT);
- find where cases stall (process mining).

**Cases, not alerts.** Cases that share a leading hypothesis *and* infrastructure (device, IP, beneficiary cluster) merge into **campaigns**. Each campaign is tagged by MITRE F3 stage (Initial Access → Positioning → Execution → Monetization) and ranked by expected loss and victim vulnerability.

### 9.6 The Chase loop (after money moves) — *the Jatayu loop*

When a case resolves to APP or ATO *after* funds have left, DELPHI switches its action space from "probe" to "freeze":
- **Forward-flow forecast**: on the temporal transaction graph, predict where the money goes next and how fast (temporal link prediction).
- Rank candidate holds by **expected recoverable ₹** = P(funds reach account by time t) × expected balance at freeze time × P(the freeze is accurate and lawful).
- **Lien only the traced amount, not the whole account, and record the reasons.** This is now effectively what courts require: Rajasthan HC (Aug 2026) made the disputed-amount lien the default with recorded reasons, and Delhi HC (*Malabar Gold*, Jan 2026) rejected blanket debit freezes. DELPHI's evidence trail *is* the recorded reasons.
- *Prior art:* JPMorgan's 2004 patent (US7480631) already claimed prioritising recovery by amount × success rate. DELPHI's new piece is the **per-hop, real-time** version on the payment graph: traced balance × P(the freeze lands before the next hop) × P(the account is a mule).
- Auto-draft CFCFRMS tickets for downstream banks in priority order.
- KPIs: **golden-hour recovery ratio** and **tracing delay**. Epidemic models show tracing delay decides whether spread is contained, and it applies to mule networks too.
- **Inbound side.** When the bank is the *receiving* bank, the same engine runs Mule-vs-Genuine on incoming credits. Under the UK's 50/50 reimbursement split, and India's mule SOP, receiving-side detection is now a cost line.

### 9.7 How DELPHI finds **previously unknown** fraud (the use case explicitly asks for this)

- **The ∅ "Unexplained" hypothesis.** When anomaly detectors fire but the evidence fits *no* known hypothesis well (low likelihood under every typology), the case is routed to the **Typology Scout**.
- The Scout clusters unexplained cases by behaviour, graph and F3 stage, and proposes a **candidate new typology** with its distinguishing evidence.
- An FDE engineer and investigators then:
  - name it;
  - define its probes and likelihoods (*detection-as-code*, versioned);
  - backtest it on history;
  - **shadow-run** it;
  - promote it.
- The hypothesis set grows the way the bank's real fraud landscape grows. That is "continuously evolve the solution", made literal.

### 9.8 Solving the label problem, for free

| Today | DELPHI |
|---|---|
| Blocked or held items never reveal ground truth, so models learn only from what they let through (selective labels) | Probes resolve held items to *confirmed* outcomes: callback confirmed, the customer admitted being coached, the beneficiary was a confirmed mule. **Held decisions get labels.** |
| Stripe's fix lets a little fraud through on purpose (costly) | A small ε-randomisation among *probe choices* (not between block and allow), with logged probabilities, gives **unbiased evaluation of the probe policy** via inverse-propensity weighting, at **no fraud cost** |
| Labels = disposition only | Labels = disposition + evidence path + probe outcomes, which also trains the likelihood tables |

---

<a name="10-architecture"></a>
## 10. Architecture, agents and the FDE loop

```
┌──────────────────────────────── L0  DATA FABRIC (FDE connects in weeks) ───────────────────────────────┐
│ Core banking / UPI switch · digital-banking sessions + device SDK · beneficiary registry · CRM/complaints │
│ case management · external: I4C Suspect Registry · MuleHunter · DoT FRI · NPCI · DPIP · CFCFRMS · telco   │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                     ▼
┌──────────────── L1  SENSING (state of the art — not our USP) ───────────────┐
│ Behavioural twin (10-min / day / month / lifetime baselines)                 │
│ Temporal entity graph (customer–account–device–IP–beneficiary–merchant)       │
│ F3 stage tagger (new device = Initial Access; add payee/raise limit =        │
│   Positioning; transfer = Execution; fan-out/cash-out = Monetization)        │
│ Detectors: GBM + isolation forest + graph features/GNN → calibrated priors   │
└──────────────────────────────────────────────────────────────────────────────┘
                     ▼
┌──────────────── L2  HYPOTHESIS ENGINE (core) ───────────────────────────────┐
│ Typology library: each hypothesis = F3 chain + probe likelihood tables       │
│ Bayesian posterior update · ∅-Unexplained open-world detector                │
└──────────────────────────────────────────────────────────────────────────────┘
                     ▼
┌──────────────── L3  VALUE-OF-INFORMATION PLANNER (core) ────────────────────┐
│ Action spaces: probes (Guardian) · evidence (Navigator) · freezes (Chase)     │
│ Cost model (friction ₹, delay, money-at-risk, investigator minutes)          │
│ Guardrails: max friction per customer, vulnerable-customer policy, RBI        │
│ timelines (20/10/60 days), fairness caps                                      │
└──────────────────────────────────────────────────────────────────────────────┘
                     ▼
┌──────────────── L4  AGENTS ─────────────────────────────────────────────────┐
│ Guardian Agent (live payments) · Navigator Copilot (cited evidence, "what     │
│ would change my mind") · Chase Agent (golden hour) · Typology Scout (unknown  │
│ patterns) · Compliance Agent (due-diligence pack, STR/NCRP drafts)            │
│ LLMs phrase, fetch and summarise; they NEVER decide holds — the Bayesian       │
│ engine does (prevents hallucinated decisions; FREE-AI aligned)                │
└──────────────────────────────────────────────────────────────────────────────┘
                     ▼
┌──────────────── L5  FDE LEARNING LOOP ──────────────────────────────────────┐
│ Probe outcomes + investigator traces → likelihood updates                    │
│ ε-exploration + inverse-propensity weighting → unbiased policy evaluation     │
│ New typologies as versioned code → backtest → shadow → approve → live         │
│ Weekly FDE × investigator typology review                                     │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Why this is *Forward Deployed Engineering* and not a product drop-in:**
1. **Week 1 — shadow the investigators.** Map their real hypotheses and evidence paths, and *elicit* the likelihood tables ("of 100 genuine customers, how many would hesitate here?"). Investigator expertise becomes the model's prior on day one, which solves cold start without months of labels.
2. **Connect** only the data that the highest-VoI probes need, because VoI tells you which integrations matter most.
3. **Engineer** new probes and typologies as code, alongside investigators.
4. **Deploy** in shadow first, then passive probes only (zero friction), then active probes.
5. **Evolve**: investigator traces and probe outcomes retrain the system weekly.

There is outside evidence for this model: Socure was hiring "Forward Deployed Engineers – RiskOS Agents" in Aug 2026 to turn bank SOPs into agent behaviour, and Grab's analyst-written SOP agent cut ATO handling from 22 to 3 minutes.

---

<a name="11-metrics"></a>
## 11. Metrics nobody else will show

| Metric | What it proves |
|---|---|
| **Hold Resolution Time** (median time from hold to confident decision) | Compliance with the Supreme Court / RBI regime without days-long freezes |
| **Genuine-customer hold rate** and **friction ₹ per fraud ₹ prevented** | Fairness; customer experience |
| **Information yield per probe** (bits, or posterior shift) | That VoI choice beats fixed friction |
| **Coercion catch rate** (APP recall where 2FA passed) | Solves the case 2FA can't |
| **Label coverage** (% of held/blocked decisions with a verified outcome) | Fixes the selective-labels problem |
| **Golden-hour recovery ratio** (₹ frozen ÷ ₹ at risk) and **tracing delay** | The Chase loop works |
| **Uncertainty reduced per investigator minute** | Investigator productivity, not just "fewer alerts" |
| **Unknown-pattern time-to-typology** (first ∅ case → approved typology) | Continuous evolution — the FDE outcome |
| **Due-diligence completeness** (% of decisions with a full evidence trail) | Liability defence |

---

<a name="12-poc"></a>
## 12. Stage 2 PoC plan (4 weeks) + demo script

**Data:**
- **Synthetic scenario generator** (core): customers, devices, sessions, beneficiaries, payments, plus *probe-response distributions* per scenario:
  - genuine-unusual (wedding, hospital abroad, new phone);
  - ATO;
  - digital-arrest APP;
  - investment scam;
  - inbound mule;
  - one hidden "unknown" typology for the Scout to discover live.
- **BAF (Feedzai, NeurIPS 2022)**: device and session features, to train and credibly benchmark the sensing layer.
- **PaySim**: takeover → transfer → cash-out chains.
- **AMLworld HI-Small (IBM)**: fully labelled multigraph for the Chase loop.

**Stack:**
- Python, pandas, LightGBM, scikit-learn (isolation forest), NetworkX (or PyG for a GNN stretch goal).
- NumPy Bayesian engine and VoI planner (a few hundred lines; greedy one-step lookahead).
- FastAPI.
- Streamlit or React investigator console, plus a mock mobile banking screen for probes.
- An LLM for cited narratives and multilingual probe phrasing.
- SQLite/Postgres for the audit trail.
- Optional Neo4j for graph visuals.

| Week | Build |
|---|---|
| 1 | Scenario generator + sensing layer (GBM + graph features) → calibrated multiclass prior; typology library v1 (5 hypotheses + ∅) with elicited likelihoods |
| 2 | Bayesian engine + VoI planner + probe simulator; baselines: (a) static rules, (b) score → fixed OTP/hold |
| 3 | Investigator console (hypothesis bar, next-best-evidence, cited LLM findings, due-diligence pack); Chase on AMLworld subgraph |
| 4 | Typology Scout (cluster ∅ cases); learning loop (likelihood updates, ε-exploration log); metrics dashboard; demo polish |

**Demo script (about 7 minutes, three stories + one reveal):**
1. **"Kamala-amma under digital arrest."** OTP would pass (show the baseline approving the payment). DELPHI picks call-status → spell-breaker in Telugu → hesitation and an app switch → posterior APP 0.98 → hold + trusted-person call. The due-diligence pack is generated.
2. **"Arjun paying a hospital in Dubai from a new phone."** The static rule holds it (and would hold for 60 days under the SOP). DELPHI: beneficiary lookup → biometric step-up → **released in 20 s.**
3. **"The money already left."** Chase mode: forward-flow forecast on the graph → 3 targeted liens ranked by recoverable ₹ → CFCFRMS tickets drafted → ₹X of ₹Y recovered inside the golden hour.
4. **The reveal.** Several ∅-Unexplained cases cluster in the Scout → it proposes "Typology T-07: refund-scam via fake UPI collect request" → the investigator approves → it is backtested, shadow-run and live. *The system learned a new fraud pattern in front of the judges.*
5. **The results table**: Static rules vs Score + fixed friction vs **DELPHI** on fraud caught, genuine-hold rate, median resolution time, friction cost, label coverage.

---

<a name="13-slide"></a>
## 13. Draft content for the 1-slide template

**Title:** *DELPHI — Dynamic Evidence-Led Probing of fraud Hypotheses & Interventions*
**Tagline:** *"Don't just score fraud. Ask the question that reveals it."*

**① Business need / opportunity**
- India's cybercrime portal (NCRP) recorded **24.02 lakh** financial-fraud complaints and **₹22,495 cr** lost in 2025 (Rajya Sabha, Feb 2026). **Less than 1%** of reported losses is ever refunded.
- Today's fraud systems produce a risk *score* and apply fixed friction. But coerced victims pass OTP, genuine customers get frozen, and held transactions never yield labels.
- The **Supreme Court bench (29 Oct 2026)** proposed that banks stall unusual transactions until verified and compensate victims when due diligence fails; RBI rules are due by **31 Dec 2026**.
- High Courts are simultaneously striking down blanket freezes.
- Banks need to **resolve holds in minutes, fairly and with recorded reasons.**

**② Target segment / market**
- Indian banks: PSBs, private banks, SFBs, co-operative banks.
- PSPs and UPI apps.
- Then UK receiving banks (50/50 APP liability), Singapore, Australia, EU.
- Fraud detection & prevention market: **global $32B (2025) → $65.7B (2030), 15.5% CAGR** (MarketsandMarkets); **India $1.69B (2025) → $8.86B (2034), 20.2% CAGR** (IMARC).

**③ Solution details + USP**
- Competing-hypothesis engine (Genuine / ATO / Coerced-APP / Mule / Unknown) that picks the **next probe, evidence or freeze by value of information**.
- **Guardian** (smart friction), **Navigator** (next-best-evidence investigator copilot), **Chase** (golden-hour recovery of only the traced amount).
- USPs:
  - ① Knows *which* friction is informative (OTP is useless against coercion).
  - ② Turns probes into labels, fixing selective-label bias.
  - ③ Learns from investigators' reasoning, not just verdicts.
  - ④ An open-world "Unexplained" hypothesis discovers new typologies.
  - ⑤ An auditable due-diligence pack for every decision.
- LLMs explain and fetch; Bayesian decisions stay deterministic and auditable.

**④ Future readiness (roadmap)**
1. FDE discovery: elicit investigator likelihoods; map typologies to MITRE F3.
2. MVP in shadow mode.
3. Pilot with zero-friction passive probes.
4. Scale: integrate DPIP/MuleHunter/FRI/CFCFRMS APIs; share likelihood tables across banks by federated learning; add agentic-payment (Rogue-AI-agent) hypotheses.
5. Launch as a SaaS/on-prem module.

**Replicable** wherever a decision can request more evidence at a cost:
- insurance claims (which document to request);
- telecom SIM-swap;
- e-commerce returns abuse;
- healthcare billing;
- government benefit (DBT) leakage;
- loan servicing (Use Case 1).

*Visual suggestion:* a centre graphic showing the hypothesis bar shifting across 3 probes (the §9.3 table), with the Lakshman Rekha / golden deer / Jatayu motif as small icons for Guardian / Navigator / Chase.

---

<a name="14-qa"></a>
## 14. Judge Q&A prep

**"Won't extra questions annoy customers?"** Probes only run in the *ambiguous band*, and zero-friction passive probes always go first. Active probes run only when VoI exceeds their cost. Compared with the mandated alternative (blanket holds of up to 60 days), DELPHI *reduces* friction for genuine customers. We measure friction cost explicitly.

**"Scammers will coach victims to answer correctly."** That is why DELPHI scores *response behaviour*, not just content: hesitation, switching to WhatsApp, implausibly fast "right" answers. Question variants rotate, and coaching itself leaves a signature. Adaptation is tracked through falling information yield per probe, which triggers FDE review.

**"Where do the probabilities come from on day 1?"** They are elicited from investigators in FDE workshops (structured expert elicitation), then updated with Bayesian counts as outcomes arrive. This avoids the months-of-labels cold start.

**"How is this different from BioCatch Scams360 / Callsign / Google in-call protection?"** Those generate a *signal* or apply an intervention triggered by a score threshold. Callsign's patent challenges the user when the score is uncertain; EMC's patents pick a challenge by attack type. DELPHI *chooses among* interventions, including those very signals as probes, by expected value of information over a multi-way hypothesis posterior. It learns which probes work, and it extends the same engine to investigation and recovery. We consume their signals; we don't compete with them.

**"Isn't this just Airbnb's targeted friction?"** Airbnb chooses *whether* to apply one friction by weighing chargebacks prevented against good-user dropout. That is a two-way, cost-aware decision. DELPHI chooses *which of many* probes to run, in sequence, to separate *several* fraud types (including coercion, where friction that verifies identity tells you nothing).

**"Is this patented already?"** We opened the closest patents: EMC '689 and '866, BioCatch '339 and '394, Callsign '735, JPMorgan '631, Vocalink '574 and Micro Focus '374. Each covers an ingredient; none covers a shared value-of-information posterior across payment-time probes, investigator evidence and post-loss freezes, or probe-derived labels for held transactions. (Wording from §17.1.)

**"Isn't an LLM making fraud decisions risky?"** It doesn't make them. Decisions come from a transparent Bayesian engine with explicit loss functions. LLMs only phrase questions, fetch evidence and write *cited* narratives. This aligns with RBI FREE-AI principles and the Governor's emphasis on human oversight.

**"Privacy — call status, device data?"**
- Consent-based under the DPDP Act 2023.
- On-device signals where possible.
- Metadata only, never call content.
- Data minimisation: VoI tells you which data is worth collecting at all.

**"Fairness for elderly or less digital customers?"** There is a vulnerable-customer policy layer: different loss weights, and a trusted-person route (in line with the RBI's 70+ proposal). Fairness caps limit friction per customer segment, and hold rates are reported by segment.

**"Scalability / latency?"** Passive probes are API lookups (milliseconds). Bayesian updates are arithmetic over about 10 hypotheses. Heavy models stay in the sensing layer, which already runs in production at peers.

**"Does it work against unknown fraud?"** Yes, through the ∅-Unexplained hypothesis → Typology Scout → FDE typology pipeline (shown live in the demo).

**"Why not just use ARGUS-style red-teaming?"** Its core has prior art (two US patents, FRAUD-RLA). And red-teaming finds *where* detectors fail, but not *what to do* with a risky payment in front of you. They're complementary (§15).

---

<a name="15-argus"></a>
## 15. DELPHI vs ARGUS — and how to merge them

| | ARGUS (ChatGPT report) | DELPHI (this report) |
|---|---|---|
| Core question | "How would fraud mutate to evade us?" | "What action would reveal the truth fastest at least harm?" |
| Layer | Detection (finding blind spots) | Decision and resolution (after detection) |
| Handles coerced victims (2FA passes)? | Weakly (structure looks normal) | **Directly** (coercion probes) |
| Addresses the Supreme Court / RBI hold regime? | No | **Central** |
| Prior-art risk | High (patents 12597034 and 12511659, FRAUD-RLA) | Low in this form |
| Mutation-realism problem | Yes: synthetic variants may be unrealistic and inflate false positives | N/A |
| PoC convincingness | "Blind spot discovered" is impressive but synthetic-on-synthetic | Side-by-side baseline vs DELPHI on resolution time, false holds, catch rate |
| Investigator workflow | Hypothesis explanations | Next-best-evidence + reasoning-trace learning + due-diligence pack |
| Recovery after loss | None | Chase loop |

**Merge option — if the team loves both:** use ARGUS's mutation engine as DELPHI's **robustness tester**, in a roadmap stage or an optional module. Each night it mutates confirmed typologies and checks whether DELPHI's *probe set* still separates the hypotheses (in other words, whether the bank can still resolve the mutated attack). The pitch line becomes:
> *"ARGUS-style red-teaming tells you where you're blind. DELPHI tells you what to ask when you are."*

Keep DELPHI as the headline. One crisp idea beats two half-ideas on a 1-slide format.

---

<a name="16-sources"></a>
## 16. Sources
*(Items I personally re-verified on 30 Oct 2026 are marked ✔. Vendor metrics are self-reported.)*

**Regulation & India**
- ✔ Supreme Court on digital-arrest compensation and holds (29 Oct 2026): https://www.medianama.com/2026/10/223-sc-rbi-banks-compensate-digital-arrest-victims/
- ✔ RBI draft money-mule SOP (11 Sep 2026): https://www.corplawupdates.in/updates/rbi-draft-kyc-money-mule-sop-2026
- RBI Authentication Directions: https://website.rbi.org.in/as/web/rbi/-/press-releases/rbi-issues-directions-on-framework-on-authentication-mechanisms-for-digital-payment-transactions
- RBI customer-liability amendment: https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=63011 · https://inc42.com/buzz/rbis-digital-fraud-compensation-rules-victims-to-get-up-to-%E2%82%B925000/
- RBI discussion paper (1-hour delay): https://www.medianama.com/2026/04/223-rbi-1-hour-delay-digital-payment-fraud-safeguards/ · https://telanganatoday.com/rbi-proposes-delay-for-digital-transfers-above-rs-10000-to-curb-rising-frauds
- NCRP complaint series: https://www.scanmyshadow.com/blog/2026/10/india-cyber-fraud-2021-2025-ncrp-complaints-losses.html
- CFCFRMS figures (lien vs refund — verify against PIB): https://www.sarkaritel.com/national-cyber-crime-reporting-portal-saves-11158-crore/ · https://www.impriindia.com/insights/policy-update/citizen-financial-cyber-fraud-reporting-and-management-system-cfcfrms-strengthening-indias-response-to-digital-financial-fraud/
- Suspect Registry scale: https://the-ken.com/story/flagged-3m-mule-accounts-declined-rs-25k-cr-in-fraud-transactions-then-who-knows/
- DoT FRI: https://www.gasa.org/post/how-indias-fraud-risk-indicator-strengthens-real-time-scam-detection
- RBI Annual Report FY26 fraud data: https://www.outlookbusiness.com/news/financial-institutions-report-over-10000-cases-of-fraud-involving-48000-cr-in-fy26-rbi-data
- UPI fraud: https://yourstory.com/2025/12/upi-frauds-peak-in-fy24-show-signs-of-decline-parliament-data
- MuleHunter / DPIP: https://www.ibtimes.co.in/rbi-speeds-idpic-rollout-combat-digital-payment-frauds-pilot-running-8-banks-906120 · https://www.policyedge.in/p/i4c-and-rbih-partner-to-use-ai-for-detecting-mule-accounts-and-financial-fraud · https://www.angelone.in/news/economy/rbi-to-introduce-digital-payment-intelligence-platform-to-combat-banking-fraud
- Insider and mule economy: https://cyberpeace.org/resources/blogs/inside-indias-cyber-fraud-economy-bank-insiders-crypto-and-mule-accounts
- Digital arrest stats: https://inc42.com/buzz/indians-lost-inr-1935-cr-to-digital-arrest-scams-in-2024-govt
- UK PSR reimbursement: https://www.psr.org.uk/information-for-consumers/app-scams-reimbursement-dashboard/ · https://www.psr.org.uk/news-and-updates/latest-news/news/payment-fraud-falls-by-73m-following-psr-reimbursement-scheme/
- Singapore SRF: https://www.mas.gov.sg/regulation/guidelines/guidelines-on-shared-responsibility-framework

**Engineering posts & papers**
- Stripe Radar: https://stripe.dev/blog/how-we-built-it-stripe-radar · counterfactual evaluation: https://www.infoq.com/presentations/stripe-ml-models-fraud · ML flywheel: https://stripe.com/blog/the-ml-flywheel-how-we-continually-improve-our-models-to-reduce-card-testing · foundation model: https://techcrunch.com/2025/05/07/stripe-unveils-ai-foundation-model-for-payments-reveals-deeper-partnership-with-nvidia
- Uber RADAR: https://www.uber.com/en-GB/blog/project-radar-intelligent-early-fraud-detection/ · graph learning: https://www.uber.com/blog/fraud-detection/
- Grab: https://engineering.grab.com/graph-for-fraud-detection · https://engineering.grab.com/streamlining-riskops-with-sop · Spade: https://arxiv.org/abs/2211.06977
- Nubank nuFormer: https://arxiv.org/html/2507.23267v1
- Monzo: https://monzo.com/blog/build-a-reactive-fraud-prevention-platform · https://monzo.com/blog/representation-learning-for-enhanced-fraud-detection
- Capital One ATLAS: https://arxiv.org/abs/2509.20339
- Meta DEC: https://www.usenix.org/conference/usenixsecurity21/presentation/xu-teng · LinkedIn: https://www.linkedin.com/blog/engineering/trust-and-safety/automated-fake-account-detection-at-linkedin
- PayPal ATO graph: https://developer.paypal.com/community/blog/graph-usage-in-combating-ato-fraud-risk/
- Revolut PRAGMA: https://arxiv.org/abs/2604.08649

**Vendors & market**
- Visa A2A Protect: https://corporate.visa.com/en/sites/visa-perspectives/newsroom/enhanced-a2a-protect.html · Visa–BioCatch: https://investor.visa.com/news/news-details/2026/Visa-to-Acquire-BioCatch/default.aspx
- Feedzai Farol: https://a-teaminsight.com/briefs/feedzai-adds-farol-agent-for-fraud-rules-investigations-and-sar-drafting/
- NICE Actimize agentic: https://www.niceactimize.com/press-releases/nice-actimize-x-sight-actone-platform-redefines-financial-crime-investigations-with-agentic-ai-476
- SAS Forrester Wave: https://www.sas.com/en_us/news/press-releases/2026/august/forrester-financial-crime-solutions.html
- Sardine agents: https://www.sardine.ai/blog/agentic-frontier-ai-fraud-detection · Unit21: https://www.unit21.ai/
- FIS + Anthropic: https://www.fisglobal.com/en/about-us/media-room/press-release/2026/fis-brings-agentic-ai-to-banking-with-anthropic-starting-with-financial-crimes
- Socure FDE role: https://jobs.accel.com/companies/socure/jobs/90327567-forward-depolyed-engineer-riskos-agents
- Mastercard Consumer Fraud Risk: https://newsroom.mastercard.com/news/press/2024/september/mastercard-expands-first-of-its-kind-ai-technology-to-help-banks-protect-more-consumers-from-scams-in-real-time
- Market size: https://www.imarcgroup.com/india-fraud-detection-prevention-market · https://www.imarcgroup.com/global-fraud-detection-prevention-market
- UK Finance 2026: https://www.fintechfutures.com/financial-crime-fraud-prevention/uk-payment-fraud-losses-top-1-28bn-in-2025-new-uk-finance-report-finds
- LexisNexis fraud multiplier: https://risk.lexisnexis.com/about-us/press-room/press-release/20250910-fraud-multiplier

**Research, novelty and cross-domain**
- ✔ MITRE F3: https://ctid.mitre.org/blog/2026/04/09/fraud-fighters-unite-with-mitre-f3/
- ✔ Apate.ai: https://techcrunch.com/2026/10/14/apate-sends-ai-agents-undercover-to-get-scammed-so-they-can-gather-fraud-intelligence/
- Prior-art patents on adversarial fraud generation: https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/12597034 · https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/12511659
- FRAUD-RLA: https://arxiv.org/abs/2502.02290 · Cost-aware adversarial robustness (NDSS): https://www.ndss-symposium.org/ndss-paper/adversarial-robustness-for-tabular-data-through-cost-and-utility-awareness/
- Selective labels: https://www.cs.cornell.edu/home/kleinber/kdd17-selective.pdf · Mastercard Causal Label Recovery: https://arxiv.org/abs/2605.29272 · Dal Pozzolo: https://boracchi.faculty.polimi.it/docs/2017_FraudsTNNLS.pdf · Carcillo active learning: https://arxiv.org/abs/1804.07481
- Explainable AML triage (supporting vs contradicting evidence): https://arxiv.org/abs/2604.19755 · Co-Investigator AI: https://arxiv.org/abs/2509.08380 · FraudBench: https://arxiv.org/abs/2608.18136
- Analyst XAI field study: https://arxiv.org/abs/2101.08758 · FiFAR: https://arxiv.org/abs/2312.13218
- RLAuth: https://publications.polymtl.ca/54142/ · EMC challenge-selection patent: https://patents.justia.com/patent/9122866 · Risk-cost model: https://arxiv.org/abs/2605.02979
- CeDEx scam-warning RCT: https://www.nottingham.ac.uk/cedex/documents/papers/cedex-discussion-paper-2023-08.pdf
- Android in-call protection: https://blog.google/security/android-expands-pilot-in-call-scam-protection-financial-apps/
- CBA–Telstra Scam Indicator: https://www.itnews.com.au/news/cba-and-telstra-expand-scam-indicator-tool-to-landlines-611146
- Pay.UK MITS: https://www.finextra.com/newsarticle/33094/payuk-rolls-out-new-mule-busting-technology
- Datasets: BAF https://github.com/feedzai/bank-account-fraud · AMLworld https://arxiv.org/abs/2306.16424 · Elliptic++ https://github.com/git-disl/EllipticPlusPlus · Multi-GNN https://arxiv.org/abs/2306.11586 · FraudGT https://jshun.csail.mit.edu/FraudGT.pdf
- Agentic commerce "data chasm": https://hstalks.com/article/11821/download/ · Google AP2: https://www.pymnts.com/artificial-intelligence-2/2025/google-unveils-a-payment-protocol-for-ai-driven-commerce/

---

<a name="17-round2"></a>
## 17. Round 2 — gap-fill research (30 Oct 2026)

Three more research tracks, about 180 searches and page fetches in total:
1. Prior art across patents and papers, opening the closest patents on Google Patents.
2. Verification of the India numbers against primary sources (Parliament answers, RBI, MHA, DoT).
3. The remaining global regulation, vendors and engineering blogs.

pib.gov.in returned HTTP 403, so some Parliament figures are cited through outlets that name the question number, date and minister.

### 17.1 Prior-art and patent check on DELPHI

| Sub-claim | Closest items found (opened) | Overlap |
|---|---|---|
| A. Choosing challenge / friction by information | EMC **US9639689** (2013, challenge matched to inferred attack type); EMC **US9122866** (2013, cost-effectiveness; "SMS pointless if device compromised"); BioCatch **US9558339** (2010, picks the interference that best discriminates owner vs attacker); EMC US9667611; Visa US20100114776 "challenge optimizer" | Medium–High on ingredients; **none uses a value-of-information calculation over a multi-way fraud posterior** |
| B. Multi-hypothesis posterior + step-by-step evidence | Zheng & Rish, active diagnosis by entropy (method, not fraud); active feature acquisition (IJCAI 2026 survey); Bayesian Analysis of Competing Hypotheses (intelligence analysis, 2021); Callsign's uncertain-band probing | **The maths is textbook; applying it to {genuine, ATO, coerced, mule, first-party, rogue agent, unexplained} appears new** |
| C. Investigator next-best evidence | JPMorgan **US7480631** (2004, fraud-type-conditioned sequential questioning, rule-based); Micro Focus **US11544374** (2018, imitates analysts, security); TU Delft/ING POMCP for AML (2020 thesis) | Concept crowded; value of information over typed hypotheses minus fetch cost not found |
| D. Probe outcomes as labels for held transactions | Stripe counterfactual evaluation (2015, lets fraud through); Mastercard Causal Label Recovery (2026, offline) | **Cleanest novelty** |
| E. Freeze priority by expected recovery | JPMorgan US7480631 (2004) claims recovery priority by amount × success; Vocalink **US10965574** (2018, payment-chain tracing to stop money leaving) | Abstract idea is prior art; per-hop real-time graph version open |
| F. Scam questions + behavioural scoring | **Callsign US11531735** (2022, in force ~2042): coercion-priming challenge + timing/cadence/jitter + remote-access detection — **HIGH**; BioCatch US10970394 (passive vishing/duress); Revolut questionnaire (2024) | Not novel alone; freedom-to-operate risk |

**Safest novelty wording for the pitch (use verbatim):**
> "To our knowledge, no published patent or product unifies payment-time customer probes, investigator evidence requests and post-loss freeze prioritisation under a single Bayesian posterior over competing fraud typologies. These include coerced victim, mule, AI agent and an open 'unexplained' class. DELPHI selects each next action by expected value of information net of cost. Existing systems choose challenges by risk score or threat type (e.g. EMC), score coerced behaviour after a challenge (e.g. Callsign), or recommend steps by imitating analysts. DELPHI additionally treats probe outcomes as propensity-logged labels, reducing selective-label bias without releasing suspected fraud."

**Never say:** "first to use behavioural responses to scam questions" (Callsign, Revolut), "first to prioritise recovery by expected value" (JPMorgan 2004), "first to choose authentication by threat type" (EMC), or "a novel inverse-propensity correction" (Stripe).

**Additional prior art from engineering blogs:**
- **Airbnb, "Fighting Financial Fraud with Targeted Friction":** applies a friction instead of a block when a model is unsure. Its cost model is chargebacks prevented vs good-user dropout, measured with 95/5 A/B tests. This is a *two-way, single-friction* version of DELPHI's idea, so cite it as inspiration.
- **Coinbase (2026):** posts on a "dynamic control policy" replacing static rules and on agentic retraining with human review gates *(dates from search snippets)*.

### 17.2 India facts — verification table

| Claim (used earlier) | Verdict | Use this |
|---|---|---|
| NCRP 2025: 24.03 lakh / ₹22,495 cr | ✅ Confirmed (Rajya Sabha, 11 Feb 2026: **24.02 lakh**) | ₹22,495 cr. The "21.8 lakh / ₹19,813 cr" figure is an earlier, narrower I4C cut; the RBI paper says 28 lakh / ₹22,931 cr for "digital payment fraud". Name the source you quote |
| CFCFRMS ₹11,158 cr saved, 32.80 lakh complaints (30 Jun 2026) | ✅ Confirmed (Lok Sabha written reply, Aug 2026; same reply: 3,718 apps, 15.75 lakh SIMs, 5.77 lakh IMEIs blocked) | As stated |
| ₹56,087 cr / ₹9,079 cr lien / ₹206 cr refunded | ⚠️ Unverifiable (no primary source) | Use the MHA review instead: ₹64,447 cr → ₹10,718 cr lien → ₹323 cr refunded (2021 to May 2026) |
| RBI discussion paper, 9 Apr 2026 | ✅ Confirmed (press release 2026-27/57): 1-hour lag on APP payments ≥₹10k (cancellable, whitelisting allowed); trusted person for **70+ and persons with disabilities** on APP >₹50k; ₹25 lakh credit cap with "shadow credit"; kill switch. **>₹10k = 45% of fraud volume, 98.5% of value; >₹50k = ~92% of value** | As stated; not yet final |
| Suspect Registry: 32.08 lakh mule accounts, ₹25,698 cr declined | ✅ Confirmed (they are *Layer-1* mule accounts; plus 30.48 lakh suspect identifiers) | Don't add this to the CFCFRMS "saved" figure |
| DoT FRI ₹5,043.73 cr protected by Aug 2026 | ✅ Confirmed (DoT, 8 Sep 2026) | As stated |
| MuleHunter 26 banks (Mar) / 29 banks (Jul 2026) | ✅ Confirmed (Lok Sabha Q3867, Rajya Sabha Q1032). An RBI Deputy Governor reportedly cited **95% mule-detection accuracy at Canara Bank** | As stated |
| RBI FREE-AI: 7 sutras, 26 recommendations (13 Aug 2025) | ✅ Confirmed. Sutras include *People First* (human oversight and override) and *Understandable by Design*. **Human oversight required for medium/high-risk autonomous AI**; AI inventory, audit, incident reporting | Use this to justify "the LLM never decides holds" |
| Customer liability amendment | ✅ Confirmed, with detail: **85% of net loss or ₹25,000, whichever is lower**; only frauds ≤₹50,000; **once per lifetime**; effective **1 Jan 2027** | As stated |
| bank.in deadline | ✅ 31 Oct 2025 (RBI/2025-26/28) | — |
| Supreme Court, 29 Oct 2026 | ⚠️ **Oral proposals, not a final order**. "Stalled until genuineness is verified in writing"; alert the local police; at least full compensation where due diligence failed; Attorney General opposed punishing banks | Say "the Supreme Court bench proposed" |

**New India facts worth using:**
- **Digital-arrest complaints** (I4C status report to the Supreme Court): 1,23,672 (2024) → 58,249 (2025) → 16,377 (to 30 Jun 2026). 2024 losses: ₹1,935.5 cr.
- **Supreme Court 4 Aug 2026 order:** RBI SOP on debit holds; e-Zero FIR (live in 19 states); a committee on **shared liability and victim compensation**; MeitY/DoT to study call-duration limits.
- **IDPIC** (runs DPIP): incorporated 16 Oct 2025; SBI and BoB 50:50; ₹500 cr authorised capital; targeting operation by Dec 2026.
- **NPCI:** federated-AI fraud model piloted with ~4 banks (Apr 2025), with data staying behind each bank's firewall. "UPI HELP" assistant (Oct 2025) handles disputes, not fraud detection.
- **Google Pay India:** "prevented ₹13,000 cr" in 2024 (self-reported). Screen-share scam alert pilot with GPay, Paytm and Navi (Nov 2025).
- **PhonePe Protect** (Nov 2025): auto-declines payments to "Very High" FRI numbers and warns on "Medium". **This is a single-signal decision; DELPHI would treat FRI as one probe among many.**
- **Private banks (HDFC, ICICI, Axis, Kotak) publish no primary AI-fraud results.** Treat any blog claim as unverified, and note this as a transparency gap DELPHI's metrics address.
- **Wrongful freezes** are a judicial pattern: Delhi HC *Malabar Gold* (Jan 2026); Rajasthan HC 105 petitions (Aug 2026); Kerala, Madras, Allahabad and MP High Courts ruled similarly; Cyberabad "Defreeze Mela". Strong evidence for DELPHI's reasoned, amount-specific holds.

### 17.3 Global regulation — verified details

| Jurisdiction | What's now in place | Relevance to DELPHI |
|---|---|---|
| **EU PSD3/PSR** | Deal 27 Nov 2025; Coreper 22 Apr 2026; ECON 5 May 2026; Official Journal publication not confirmed as of Oct 2026.<br>Provisions: name/IBAN check on **all** credit transfers (refuse on mismatch); PSP liability where name check or fraud controls fail; **mandatory fraud-data sharing between PSPs**; full refund for bank-impersonation (spoofing) fraud; platforms liable to PSPs if they ignore notified scam content.<br>Name-check liability applies ~24 months after entry into force | Liability for "inadequate fraud controls" means banks need provable, evidence-based decisions |
| **Australia SPF Act 2025** | Six principles: govern, prevent, detect, report, disrupt, respond.<br>Tier-1 penalties: greater of ~A$50m, 3× benefit, or 30% of turnover.<br>**Safe harbour for disruptive action for up to 28 days while investigating**, if proportionate and reversed promptly.<br>Banks, telcos and digital platforms designated May 2026; draft bank code covers confirmation of payee, warnings, payment recall, blocking.<br>Full effect **31 Mar 2027** | The 28-day window rewards **fast, proportionate resolution**, which is exactly DELPHI's job |
| **Singapore** | Shared Responsibility Framework (Dec 2024): 12-hour cooling-off after new device or token activation; 24/7 kill switch.<br>**Protection from Scams Act** (in force 1 Jul 2025): police can order banks to restrict a likely victim's account (12 orders by Feb 2026).<br>**Scams (Countermeasures) Act 2026**: passed 9 Sep 2026, not yet in force | Shows regulators intervening on the *victim's* account, i.e. the coerced-victim hypothesis |
| **UK** | Mandatory APP reimbursement year-1 evaluation (1 Jul 2026): in-scope losses down ~21% (~£73m a year); reimbursed value up from 54% to 65%. Q1 2026: £72.6m reimbursed (record quarter); 88% (£316m) cumulative. PSR consultation Dec 2026; PSR functions moving to the FCA | 50/50 sending and receiving liability makes mule-side resolution (the Jatayu loop) worth money |
| **US** | No federal APP reimbursement; Regulation E covers only *unauthorised* transfers. TRAPS Act (task force) still in committee. NY Attorney General v. Zelle/EWS survived dismissal (Jul 2026); CFPB dropped its Zelle suit (Mar 2025) | Liability is moving through the courts, not statute |

### 17.4 Remaining vendors

| Vendor | Approach / claim (self-reported) | Note |
|---|---|---|
| **Visa + BioCatch** | ✅ Confirmed: Visa press release 3 Aug 2026, **$2.4bn cash**; BioCatch serves 350+ banks in 21 countries, 760m users, 19bn sessions a month; closing ~fiscal Q2 2027 | The behavioural-signal layer is consolidating into card networks, so the open space is the **decision layer above signals** |
| **Featurespace Scam Detect** (now Visa A2A Protect) | NatWest: **+135% scam detection, −75% scam false positives**; Pay.UK ~£112m a year extra detected | Strong signals, still score-and-threshold |
| **BioCatch Scams360** (Jul 2025) | Up to 3,000 signals per session; +50% detection of non-impersonation scams; one US bank stopped $100m of impersonation-scam payments | Digital sessions only; blind to phone-led coaching outside the app |
| **Mastercard Consumer Fraud Risk** | 9+ UK banks; TSB **+20% APP detection**. Recorded Future powers Mastercard Threat Intelligence (~$120m fraud flagged in market test) | Network score |
| **Visa VAAI Score** | Card-enumeration scoring, 85% lower false positives, ~20 ms | Card testing only |
| **Callsign** | Still operating; "Dynamic Interventions" in its orchestration layer; **no public metrics** | Closest product competitor (and patent holder) |
| **Arkose Labs Titan** (Jan 2026) | Adaptive challenges to make attacks too costly ("disruptive economics") | Bot and account abuse, not payment-scam decisioning |
| **Lynx** (Spain) | Daily-retrained models; 70–80% fraud detected at 0.2% impact on genuine customers; 50 ms | Card-origin |
| **Resistant AI** | 80+ models layered on existing transaction monitoring; $25m Series B (Oct 2025) | Overlay, not real-time interdiction |
| **Signzy MuleShield** (India) | Pre-onboarding mule score from 200+ parameters; claims +72% detection and 400k+ mules found | Onboarding-time only |
| **Effectiv** | Acquired by Socure (Oct 2024, $136m), now Socure's RiskOS engine | — |
| Napier AI, IDfy, FinSec AI | AML monitoring (Napier); nothing found for IDfy or FinSec AI | — |

### 17.5 Remaining engineering posts and research

- **DoorDash rules engine (Mar 2026):** rules run at checkpoints; a DAG of ~15k reusable facts; shadow mode, replay backtests and cohort experiments; ~10k RPS. Incident mitigation time down 80%+; rule deploys from days to minutes. *DELPHI's "probes as versioned code" follows this pattern.*
- **Visa Research:** TransactionGPT (arXiv 2511.08939, +22% over a production model), TREASURE (2511.19693). Visa says transformers helped block $40bn of attempted fraud in 2024.
- **Cash App:** scam warnings on ~1.5% of P2P payments, ~50% of which are abandoned; $2bn+ losses prevented since 2020.
- **Robinhood (2024):** shared-attribute fraud graph whose outputs feed models from application through money movement.
- **Tencent/Weixin Pay:** billion-scale graph learning for credit risk (arXiv 2608.02168, Aug 2026).
- **Amazon/AWS:** GNN fraud on DGL + Neptune; GraphStorm v0.5 real-time inference.
- **Wise, Instacart:** job postings only (Wise: p99 under 1 s fraud checks on Flink/Kafka).
- **Concept drift:** no peer-reviewed fraud-specific drift survey for 2024–26 exists; the best is a general review (*Information* 15(12):786, 2024).
- **APP-scam academic gap confirmed:** no peer-reviewed APP detection paper on real bank-transfer data was found. Existing work is socio-legal (*J. Economic Criminology* 2025; *J. Financial Regulation* 2024) or not peer-reviewed (Nationwide abstract 2025; SCRIPTMIND, EACL 2026 industry track). **The PoC's synthetic APP scenario generator plus probe-response model would itself be a research contribution.**

**Still not covered after two rounds:** Amazon Fraud Detector internals, IDfy and FinSec AI products, and the full text of the FREE-AI recommendations (read the RBI PDF before quoting specific numbered recommendations).

<a name="18-naming"></a>
## 18. Naming — why DELPHI, and alternatives considered

**Recommended: DELPHI — *Dynamic Evidence-Led Probing of fraud Hypotheses & Interventions***

| Letter | Word | What it signals to judges |
|---|---|---|
| **D** | Dynamic | Adapts in real time; "continuously evolve" from the use case |
| **E** | Evidence-Led | Every decision backed by recorded evidence (due diligence, court-proof) |
| **L** | (Evidence-)Led | — |
| **P** | Probing | The core novelty: asking, not just scoring |
| **H** | Hypotheses | Competing fraud explanations, including the unknown |
| **I** | Interventions | Covers payment probes, investigator evidence and Jatayu-loop freezes |

**Why it beats ARGUS as a name:** ARGUS (the hundred-eyed giant) means *watching*, which is the passive paradigm every vendor already sells. **DELPHI means consulting the oracle before acting**, which is the active paradigm we're proposing. Three meanings in one word:
1. The Oracle of Delphi: truth revealed by asking.
2. "Know thyself", inscribed at Delphi: behavioural baselines.
3. **The Delphi method** (RAND, 1950s): structured expert elicitation, used literally in the FDE workshops that build DELPHI's likelihood tables.

That third meaning is useful when Virtusa judges ask how the solution embeds with investigators.

**Pitch line:** *"ARGUS-style systems watch with a hundred eyes. DELPHI asks one question — the right one."*

**Module names inside DELPHI:**
- **Guardian loop**: payment-time probes.
- **Navigator loop**: investigator next-best-evidence.
- **Jatayu loop**: golden-hour chase and targeted lien. This is the nod to Virtusa's programme name and the Ramayana story.
- **Typology Scout**: discovers unknown patterns.

**Alternatives checked (30 Oct 2026):**

| Name | Expansion | Why not chosen |
|---|---|---|
| SOLOMON | *Sequential Optimal Low-friction Observation for Multi-hypothesis, Open-world, Network-aware fraud intelligence* | Best metaphor: the Judgment of Solomon is a discriminating probe (kept as the 10-second explainer). But **a Kazakh startup named "Solomon" announced an AI fraud/AML investigation platform for banks on 28 Oct 2026** |
| SPHINX | *Sequential Probing of Hypotheses to INterrogate & eXpose fraud* | The riddle-asking guardian fits well, but **Sphinx (sphinxhq.com) is a funded fraud and compliance-agent startup** with document-fraud detection for banks (2026) |
| THEMIS | *Targeted Hypothesis Evaluation with Minimal-Intrusion Safeguards* | **Themis is a UK financial-crime platform** (raised £7.25m) |
| VERITAS | *Value-of-information Evidence & Risk Inference for Targeted Action & Safeguards* | **Name of MAS Singapore's responsible-AI framework** for financial services (also Veritas Technologies) |
| PENELOPE | (Penelope's bed test: a question only the true Odysseus could answer) | Lovely identity-probe story, but long and less known; also the name of an internal scam filter at a classifieds platform |
| ORACLE | — | Oracle Corp |
| VIVEKA | Sanskrit "discernment" (round-1 name) | Strong for Indian judges, but the user asked for an English acronym |

*If the team prefers a story-first name over a clean one, SOLOMON is the runner-up. Mention the Judgment of Solomon in the pitch either way.*

---
*Before submission: re-check every number on the slide against the primary source listed in §17.2. Say "Supreme Court bench proposed" (not "ordered") for 29 Oct 2026 unless a final order is issued before you submit. Cite the Callsign and EMC patents proactively in the novelty slide; it signals research maturity.*

**Round-2 sources (selected):**
- Patents: https://patents.google.com/patent/US11531735 · https://patents.google.com/patent/US9639689 · https://patents.google.com/patent/US9122866 · https://patents.google.com/patent/US9558339 · https://patents.google.com/patent/US10970394B2/en · https://patents.google.com/patent/US7480631B1/en · https://patents.google.com/patent/US10965574 · https://patents.google.com/patent/US11544374
- Active diagnosis: https://arxiv.org/abs/1207.1418 · Stripe counterfactual evaluation (2015): https://pydata.org/seattle2015/schedule/presentation/17/
- India: https://www.freepressjournal.in/india/centre-blocks-3718-fraudulent-apps-saves-11158-crore-from-cyber-fraud · https://anantamias.com/current-affairs/cyber-fraud-complaints-12-7-lakh-six-months/ · https://taxguru.in/rbi/rbi-seeks-public-comments-digital-payment-fraud-safeguards-paper.html · https://newsonair.gov.in/dot-prevents-suspected-cyber-fraud-losses-of-more-than-5000-cr-through-fri/ · https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=61018 · https://theprint.in/judiciary/e-zero-fir-mechanism-now-operational · https://lawchakra.in/supreme-court/digital-arrest-victims-banks/ · https://nsearchives.nseindia.com/corporate/SBIN_10122025155805_BSE_NSE_IDPIC_10122025.pdf · https://techcrunch.com/2025/11/20/google-steps-up-ai-scam-protection-in-india-but-gaps-remain
- Global: https://www.pymnts.com/cpi-posts/eu-strikes-landmark-deal-to-modernize-payment-services-crack-down-on-fraud/ · https://www.klgates.com/Australias-Proposed-Scams-Prevention-Framework-2-11-2025 · https://corrs.com.au/insights/developing-the-scams-prevention-framework-treasury-consults-on-draft-rules-and-codes · https://www.mha.gov.sg/media-room/newsroom/commencement-of-the-protection-from-scams-act/ · https://www.regulationtomorrow.com/2026/07/payment-fraud-falls-by-73m-following-psr-reimbursement-scheme/ · https://www.insurancejournal.com/news/east/2026/07/24/878853.htm
- Vendors and blogs: https://usa.visa.com/about-visa/newsroom/press-releases.releaseId.22626.html · https://ffnews.com/newsarticle/natwest-improves-scam-detection-rate-by-135-using-featurespaces-technology · https://www.biometricupdate.com/202507/biocatch-launches-behavioral-analytics-tool-to-help-stop-app-fraud · https://www.fintechfutures.com/ai-in-fintech/nine-uk-banks-tap-mastercard-for-new-ai-powered-fraud-risk-solution · https://usa.visa.com/about-visa/newsroom/press-releases.releaseId.20661.html · https://medium.com/airbnb-engineering/fighting-financial-fraud-with-targeted-friction-82d950d8900e · https://cash.app/press/how-ai-driven-scam-warnings-prevent-fraud-cash-app · https://arxiv.org/html/2511.08939v1 · https://robinhood.com/us/en/newsroom/preventing-fraud-at-robinhood-using-graph-intelligence/
- Name checks: https://sphinxhq.com/products/doc-fraud · https://www.wearethemis.com/uk/company/what-we-do · https://www.mas.gov.sg/schemes-and-initiatives/Veritas · https://digitalbusiness.kz/2026-10-28/kak-startap-solomon-razrabativaet-ii-platformu-dlya-bankov-i-finansovih-organizatsiy/
