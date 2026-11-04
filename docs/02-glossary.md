# 02 — Glossary (ubiquitous language)

Code, API fields, database columns and UI labels MUST use these terms. If a new concept is
needed, add it here first.

| Term | Code name | Definition |
|---|---|---|
| **Hypothesis** | `hypothesis_key` | One competing explanation of what is happening, e.g. `GENUINE`, `ATO`, `APP_DIGITAL_ARREST`. Every hypothesis except `GENUINE` and `UNEXPLAINED` is defined by a typology |
| **Typology** | `typology` | A named, versioned fraud pattern in the KB. It defines a hypothesis: description, F3 stages, applicable contexts, likelihood rows, loss parameters |
| **Unexplained (∅)** | `UNEXPLAINED` | The open-world hypothesis. Its likelihood is uniform over every outcome space, so it gains mass when the evidence fits no known typology |
| **Context** | `context` | What is being decided about: `OUTBOUND_PAYMENT` (a customer's payment) or `ACCOUNT_REVIEW` (whether an account is a mule) |
| **Decision profile** | `decision_profile` | The combination of a context and a loop stage that fixes the decision set and loss table: `GUARDIAN_PAYMENT`, `CASE_PAYMENT_HOLD`, `CASE_ACCOUNT_REVIEW` |
| **Decision** | `decision` | A terminal action in a decision profile, e.g. `RELEASE`, `PROTECTIVE_HOLD`, `CONFIRM_MULE` |
| **Belief** | `belief` | A probability distribution over the applicable hypotheses. "Prior" is the belief before any observation; "posterior" is the belief after observations |
| **Prior** | `prior` | The initial belief, built by the sensing layer from model output plus the open mass |
| **Open mass** | `open_mass` (u) | The prior probability reserved for `UNEXPLAINED` and for KB-only typologies. It rises with the anomaly percentile |
| **Observation source** | `source_key` | Anything that yields a categorical outcome the engine can learn from: a **probe** or an **evidence item** |
| **Probe** | `probe` | An observation source used in the Guardian loop at payment time. It is either **passive** (no customer interaction) or **active** (the customer sees something) |
| **Evidence item** | `evidence` | An observation source used by investigators in the Navigator loop |
| **Outcome** | `outcome` | One categorical result of an observation source, e.g. `ON_CALL_UNKNOWN_NUMBER` |
| **Outcome classifier** | `classify_<source>()` | Deterministic code that turns raw signals (timings, answers, records) into an outcome. It is never an LLM |
| **Likelihood table** | `likelihoods` | For one source: P(outcome \| hypothesis), stored as Dirichlet parameters per hypothesis row |
| **Dirichlet row** | `alpha` | Pseudo-counts for one (source, hypothesis) pair. The likelihood used is the posterior mean α / Σα |
| **Equivalent sample size (ESS)** | `ess` | Σα of a row: how many observations' worth of confidence the row carries |
| **Loss table** | `loss` | Loss(decision, hypothesis) = `base_inr + rate × amount at stake`, optionally scaled for vulnerable customers |
| **Bayes risk** | `bayes_risk` | The expected loss of the best decision under the current belief |
| **Value of information (VoI)** | `voi_myopic` | The expected reduction in Bayes risk from observing one source, before cost |
| **Net value** | `net_value` | R(p) − Q_k(p, a). It is the lookahead value of observing *a* next, net of all costs (doc 06 §6) |
| **Lookahead depth** | `lookahead_depth` (k) | How many future observations the planner considers when valuing the next one |
| **Cost** | `cost` | The ₹ cost of running a source: friction + delay (Guardian) or investigator time (Navigator) |
| **Friction tier** | `friction_tier` | `NONE`, `LOW`, `MEDIUM`, `HIGH`. How intrusive a probe is for the customer |
| **Guardrail** | `guardrail` | A hard rule that makes a source ineligible regardless of value (budgets, consent, fairness caps) |
| **Exclusive group** | `exclusive_group` | A set of sources of which at most one may run per session, e.g. `STEP_UP_AUTH` = {OTP, biometric} |
| **Fast path** | `fast_path` | Immediate `RELEASE` without probes when the sensing model is overwhelmingly confident the payment is genuine (p_model, not the prior; doc 08 §3), the amount is small and the payment is not anomalous |
| **Guardian session** | `guardian_session` | One payment-time decision process: sensing → passive probes → active probes → decision |
| **Probe run** | `probe_run` | One execution of a probe inside a session, with its candidates table, propensity and outcome |
| **Case** | `case` | A unit of investigation in Navigator. Its type is `PAYMENT_HOLD` or `ACCOUNT_REVIEW` |
| **Evidence record** | `evidence_record` | One fetched evidence item for a case: outcome, raw data, narrative, citations |
| **Campaign** | `campaign` | A group of cases that share a leading hypothesis and infrastructure (device, account, IP) |
| **F3 stage** | `f3_stage` | Kill-chain stage label: `INITIAL_ACCESS`, `POSITIONING`, `EXECUTION`, `MONETIZATION` |
| **Due-diligence pack (DD pack)** | `dd_pack` | The complete, timestamped evidence trail for a session or case |
| **Chase operation** | `chase_operation` | One golden-hour recovery effort that starts from one or more reported fraud payments |
| **Taint** | `taint_paise` | The part of an account's balance traced to the reported fraud, under proportional (haircut) tracing |
| **Freeze candidate** | `chase_candidate` | An account currently holding taint, scored by net expected recoverable value |
| **Lien** | `lien` | A debit hold on a **specific amount** (never the whole account), with recorded reasons |
| **CFCFRMS ticket** | `cfcfrms_ticket` | A simulated request to an external bank to place a lien |
| **Golden hour** | `golden_hour` | The window after a fraud report (default 60 minutes, configurable) in which recovery is measured |
| **Tracing delay** | `tracing_delay` | Time from the fraud report to the first lien being placed |
| **Knowledge base (KB)** | `kb` | The immutable, versioned bundle the engine reads: hypotheses, sources, likelihoods, losses, costs, guardrails, question bank |
| **KB version status** | `kb_status` | `CANDIDATE`, `SHADOW`, `ACTIVE`, `RETIRED`, `REJECTED` |
| **Shadow** | `SHADOW` | A KB version that is evaluated alongside the active one on live traffic but never drives decisions |
| **Typology draft** | `typology_draft` | An editable proposal for a new typology. It becomes part of a KB version on promotion |
| **Scout cluster** | `scout_cluster` | A cluster of unexplained cases found by the Typology Scout |
| **Label** | `label` | The ground-truth hypothesis for a payment or account as the bank *observes* it, with a source and a time |
| **Label source** | `label_source` | How a label became known: `CUSTOMER_COMPLAINT`, `NO_COMPLAINT_60D`, `OUTREACH_CONFIRMED`, `INVESTIGATOR_DISPOSITION`, `REGISTRY_LISTING` |
| **Selective labels** | — | Held or declined items never reveal their true label unless something resolves them |
| **Exploration** | `explore` | Choosing a near-optimal probe at random, with probability ε, to make off-policy evaluation possible |
| **Propensity** | `propensity` | The logged probability that the chosen probe was selected by the policy |
| **Off-policy evaluation (OPE)** | `ope` | Estimating how another probe policy would have performed, using logged propensities (IPS/SNIPS) |
| **Information yield** | `info_yield_bits` | KL(posterior after ‖ posterior before) in bits, for one observation |
| **Persona** | `persona` | A named, scripted synthetic customer used in demo stories (Kamala, Arjun, …) |
| **Scenario** | `scenario_type` | A simulated behaviour pattern (genuine routine, ATO, digital arrest, …) that generates payments and signals |
| **Response model** | `response_model` | The simulator's *true* distribution of signals per scenario. It deliberately differs from the KB's likelihood tables |
| **Time scale** | `time_scale` | Factor that compresses simulated durations in demo mode (default 1/30) |
| **Amount at stake** | `amount_paise` (A) | The ₹ value the loss table scales with: the payment amount for payments, the 7-day credits for accounts |
| **Vulnerable customer** | `is_vulnerable` | A customer aged ≥ 70 or flagged with a disability (research §6) |
| **Paise** | `*_paise` | Integer money unit. ₹1 = 100 paise |
