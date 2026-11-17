# 13 — Sensing layer

Research §10 (L1). Module `delphi.sensing`. This is the state of the art, **not the USP**: it
produces calibrated priors and scores that the engine consumes.

## 1. Outputs

| Output | Consumer | Shape |
|---|---|---|
| `p_model(h)` over the payment hypotheses {GENUINE, ATO, APP_DIGITAL_ARREST, APP_INVESTMENT} | Guardian prior (with the payer's `p_mule` for MULE, doc 06 §11) | dict, sums to 1 |
| `q` anomaly percentile ∈ [0, 1] | Guardian prior (open mass) | float |
| `p_mule(account)` | BENEFICIARY_INTEL (payee), Guardian prior (payer, doc 06 §11), ACCOUNT_REVIEW prior, Chase | float |
| top feature contributions (LightGBM `pred_contrib`) | DD pack, Guardian monitor | top 5 (feature, value, contribution) |

## 2. Single feature code path (no training–serving skew)

All features are computed by **one** function family operating on an in-memory
`CustomerHistory` snapshot:

```
build_payment_features(payment, history: CustomerHistory, as_of) -> dict[str, float]
build_account_features(account, history: AccountHistory, as_of) -> dict[str, float]
```

- Online path: a repository loads the snapshot from PostgreSQL with indexed queries (payments
  by payer and time, logins, devices, payees), then calls the function.
- Training and evaluation path: vectorised builders produce the same snapshots from dataframes.
- A **parity test** checks that, for 500 random payments, the online and offline features are
  equal within 1e-9 (FR-SNS-02).

`as_of` is strictly the payment's initiation time. Only data with timestamp < `as_of` may be
used (leakage guard; a unit test injects a future event and asserts it is ignored).

In code (#87): `delphi.sensing.features.build_payment_features` is the one function;
`delphi.sensing.history.load_payment_snapshot` is the online repository (the existing indexes on
payments by payer and time, logins and CRM events by customer and time, payees, devices and
accounts by customer cover its queries); `delphi.sensing.offline.OfflineHistories` builds every
customer's snapshot from dataframes. Both read the `bank` tables through `delphi.sim.api`, never
`sim_truth`.

## 3. Payment features (v1)

Behavioural baseline windows ("behavioural twin"): 10 min, 24 h, 30 days, lifetime.

| Feature | Definition | prior model | `scout` |
|---|---|---|---|
| `amount_log10` | log10(amount ₹) | ✔ | ✔ |
| `amount_z_30d` | (amount − mean_30d) / max(std_30d, ₹100) | ✔ | ✔ |
| `amount_ratio_max_lifetime` | amount / max lifetime payment | ✔ | ✔ |
| `hour_sin`, `hour_cos` | local-time hour of day encoding | ✔ | ✔ |
| `hour_deviation` | circular distance from the customer's median payment hour (hours) | ✔ | ✔ |
| `is_new_payee` | payee added < 24 h ago, **or** no earlier payment to it **and** added within the 30-day baseline window | ✔ | ✔ |
| `payee_age_days` | days since the payee was added (capped at 365) | ✔ | ✔ |
| `payee_account_age_days` | days since the payee *account* was opened (capped at 3650) | ✔ | ✔ |
| `payee_p_mule` | mule-scorer output for the payee account (as of `as_of`) | ✘ (probe input) | ✔ |
| `device_age_days` | days since the device was first seen (capped at 365) | ✘ (probe input) | ✔ |
| `is_new_device` | device_age_days < 7 | ✘ (probe input) | ✔ |
| `count_10m`, `count_24h` | payments by this customer in the window | ✔ | ✔ |
| `sum_24h_ratio_30d` | 24 h sum / 30-day daily mean sum | ✔ | ✔ |
| `distinct_payees_24h` | distinct payees paid in the last 24 h | ✔ | ✔ |
| `new_payees_7d` | payees added in the last 7 days | ✔ | ✔ |
| `limit_change_24h` | 1 if a limit increase happened in the last 24 h | ✔ | ✔ |
| `login_new_city` | the last login city never seen before | ✔ | ✔ |
| `age_years` | customer age | ✔ | ✔ |
| `is_vulnerable` | age ≥ 70 or disability flag | ✔ | ✔ |
| `channel_*` | one-hot of channel | ✔ | ✔ |
| `session_duration_s` | from session telemetry | ✔ | ✔ |
| `remote_access_active` | from telemetry | ✘ (probe input) | ✔ |

Where the table is silent (gap fills, #87):

- Windows count the customer's earlier payments from any of their accounts, whatever their
  status, never the payment being scored. Local time is IST (UTC+05:30, no daylight saving).
- With no earlier payment, `amount_z_30d` and `hour_deviation` are 0 and
  `amount_ratio_max_lifetime` is 1. The z-score uses the population std of the 30-day window.
- `hour_deviation` is the circular distance from the *circular* median local hour of the earlier
  payments: the hours are cut at the largest empty arc of the clock before taking the median.
  A customer who pays around midnight ([23.5, 23.7, 0.5, 0.6]) has a median near 0.1 h, not
  the linear median's 12 h, so a 23:40 payment deviates by about 0.4 h.
- `sum_24h_ratio_30d` divides the 24 h sum by the 30-day daily mean, floored at ₹100 like the
  z-score's std.
- A payee that was never saved is new; a payee or device first seen at or after `as_of` is not
  there yet.
- `device_age_days` counts only the customer's own devices (`bank.devices.customer_id`). A device
  recorded under another customer has never been this customer's, so it is new to them (age 0,
  `is_new_device` = 1), however long the bank has known it: a fraudster's old phone is new to
  the victim, which is the account-takeover case DEVICE_POSTURE is there to catch.
- `login_new_city`: the last login before the payment is from a city no earlier login used. A
  customer's first login is not a new city (TD-8).
- `limit_change_24h` stays 0: the bank records no limit changes yet (the CRM has SIM-swap and
  lost-device events only). `sensing/config.yaml` lists the event types that would count.
- `payee_p_mule` is 0.10, the §6 fallback, until the mule scorer (#91) writes `account_scores`.

**Double-count guard (no exceptions).** Any feature that is an input to a passive-probe
classifier is an *observation*, and is excluded from the prior model. Otherwise the engine
would count the same evidence twice, once in the prior and once in the likelihood update.

Excluded from the model (still computed, and used by the Scout and the DD pack):

- `is_new_device`, `device_age_days`, `remote_access_active` → DEVICE_POSTURE;
- `payee_p_mule` → BENEFICIARY_INTEL;
- call state → CALL_STATUS;
- paste, dwell and cadence → SESSION_BEHAVIOUR.

`payee_account_age_days` and `login_new_city` stay in the model. They correlate with probe
outcomes but are not classifier inputs. A unit test asserts that the model's `feature_list`
does not intersect `PROBE_INPUT_FEATURES`.

## 4. Prior model

- **MULE is not a payment-model class (#148).** Mule accounts are labelled through the
  registry, and their own outbound payments mature as GENUINE (doc 14 §7), so a payment model
  never sees a MULE row. With MULE as a class, `p_model(MULE)` would be about 0 for every
  payment, and because a model hypothesis gets no open mass (doc 06 §11), MULE would vanish
  from the Guardian prior. MULE enters from the payer account's mule score instead (doc 06
  §11, `with_payer_mule`).
- LightGBM multiclass (`objective=multiclass`, 4 classes). Hyperparameters are fixed in
  `sensing/config.yaml` (num_leaves 31, learning_rate 0.05, n_estimators 400 with early
  stopping on validation, class weights balanced by inverse frequency, seed 7).
- **Training labels: only labels the bank has observed as of the training cutoff** (doc 14
  §7), never `sim_truth`. Unlabelled payments are excluded. Observed labels include the
  simulator's label noise (unreported fraud later labelled GENUINE), as in reality.
- **Matured window.** GENUINE labels only arrive after `no_complaint_maturity_days` (profile
  parameter, doc 14 §2). Payments newer than that are labelled almost only when they are fraud,
  and would bias the classes. So training uses only payments initiated in the matured window
  M = [history start, cutoff − maturity]. Temporal split of M: first 2/3 train, next 1/6
  validation, last 1/6 test (whole days, rounded down; the remainder goes to train).
  - `demo`/`eval` (history 150 days, maturity 60): train days 1–60, validation 61–75, test 76–90.
  - `test` (history 21 days, maturity 7): train days 1–10, validation 11–12, test 13–14.
- Calibration (#148): the class weights make the booster learn posteriors under balanced
  class priors, which over-predicts every fraud class (about 1.5–1.8× on `eval` with
  temperature scaling alone). The calibrated logits are `(z + b) / T`, where `b` is a per-class
  **prior shift** and T > 0 a single temperature, **fitted together** on the validation split's
  NLL. `b` starts from the theoretical shift `b_c = log(π_c / π̃_c) = log(K_present · n_c / n)`.
  That shift alone overshoots (fraud under-predicted at about 0.6×), because early stopping
  halts the booster before it fully learns the balanced-class posteriors. The joint fit gives
  fraud classes within about 0.86–0.98× of their observed share on `eval`, with a lower log loss.
- Reported metrics (stored with the artifact), on the test split against observed labels:
  per-class one-vs-rest AUC, macro AUC, multiclass log loss, expected calibration error
  (15 equal-width bins on max-probability), and **per-class reliability** (mean predicted vs
  observed share, and a one-vs-rest ECE per class). The max-probability ECE is dominated by
  GENUINE and hides the fraud classes, so the per-class numbers are the ones to read.
- **Gate (enforced only on `eval` and `demo`; `test` is reported only):** ECE ≤ 0.05 and macro
  AUC ≥ 0.85.
  - If a retrain fails the gate, the artifact is stored as `FAILED_GATE` and the previous
    artifact stays active.
  - If **no** artifact exists yet (the first training), the artifact is activated anyway with
    `gate_passed = false`, and the Overview page shows a "sensing below quality gate" warning.
    The demo must always have a model.
- The evaluation's own metrics (doc 22) use `sim_truth`. The gate does not.

**Implementation** (`delphi.sensing.prior`, settings in `sensing/config.yaml` `prior:`). Like the
anomaly detector, the module trains and scores; choosing the rows from the database and storing
the artifact belong to `TRAIN_SENSING` (#88). Gaps the text above leaves open, filled as follows:

- **Observed labels:** `observed_labels` keeps the labels known by the cutoff, the doc 05 §8
  precedence choosing between several. Labels that are not model classes (`FRAUD_OTHER`, an
  unpromoted typology) are left out, like unlabelled payments.
- **Split:** the matured window is whole days from the history start; validation and test each
  get ⌊days/6⌋ and train the rest (`split_parts`).
- **Class weights:** `n / (K_present · n_c)` per training row, over the classes present in
  train. A class with no training rows (possible on `test`) gets no weight and no prior shift.
- **Early stopping:** on the validation split's unweighted multiclass log loss, patience 50
  rounds (`early_stopping_rounds`), keeping the best iteration. Training is deterministic
  (LightGBM `deterministic`, one thread, `seed`).
- **Temperature and prior shift** (`fit_calibration`): L-BFGS-B on the validation NLL over
  log T (bounded by `temperature_bounds`, [0.05, 20]) and `b` for the classes with validation
  rows; the first such class anchors `b` (only differences matter), and a class without
  validation rows keeps its theoretical shift. With no validation rows, T = 1, `b` is the
  theoretical shift, and there is no early stopping. Both are stored in the artifact.
- **Metrics:** a class with no positives (or no negatives) in the test split has AUC `null`,
  and macro AUC averages the classes that have one. An undefined macro AUC, or no test split,
  fails the gate where it is enforced.
- **Contributions:** LightGBM `pred_contrib` for one class, divided by T (so they add up, with
  the bias and `b / T`, to that class's calibrated logit). By default, for the most probable class; the top
  `top_contributions` by absolute value, ties in feature order.

## 5. Anomaly detector

- scikit-learn `IsolationForest(n_estimators=200, contamination="auto", random_state=7)` on the
  payment features of training-window payments labelled GENUINE.
- `q` = the empirical percentile of the payment's anomaly score (−`score_samples`) within the
  training scores (stored as a sorted array of 10,001 quantiles in the artifact).

## 6. Mule scorer

- Account features: `inbound_distinct_payers_48h`, `inbound_count_7d`, `inbound_sum_7d_log`,
  `median_dwell_minutes_7d`, `outbound_to_inbound_ratio_7d`, `account_age_days`,
  `kyc_minimal`, `device_shared_accounts_30d`, `cash_out_share_7d`, `complaints_linked`
  (as of the time). They come from `build_account_features` over an `AccountHistory`, through
  the same online and offline paths as the payment features (#87; migration 0009 indexes
  complaints by counterparty and devices by fingerprint). Where this is silent:
  - flows (inbound, outbound, dwell, cash-out share) count *executed* payments, by execution
    time; `inbound_sum_7d_log` is log10(1 + ₹ sum);
  - `median_dwell_minutes_7d`: the median time from each 7-day credit to the account's next
    debit; credits not yet followed by one are left out, and with none it is the whole window;
  - `outbound_to_inbound_ratio_7d` floors the inbound sum at ₹100; `cash_out_share_7d` is the
    share of the 7-day debit amount cashed out;
  - `device_shared_accounts_30d` counts other accounts that paid, in the last 30 days, from a
    device fingerprint this account paid from in those 30 days;
  - `complaints_linked` counts complaints that name the account as a counterparty.
- LightGBM binary (num_leaves 15, n_estimators 300, seed 7); isotonic calibration on validation.
- Labels: accounts labelled MULE (registry listing, investigator disposition, complaint) vs
  accounts with ≥ 30 days of activity and no fraud label.
- Gate: AUC ≥ 0.90 on test (`eval` profile).
- Scores are computed for **all** accounts, including external banks. Under A-06, network-level
  account data is visible, as national mule intelligence such as MuleHunter, DPIP or the I4C
  registry would provide. Scores are recomputed by `MULE_SWEEP` (doc 04 §5.4) and on demand,
  and cached in `account_scores(account_id, p_mule, computed_at, model_artifact_id)`.
- An account with no score yet (e.g. just opened) is scored on demand. If scoring fails, the
  fallback is p_mule = 0.10, logged as a WARNING.
- Training split: accounts are assigned to train, validation or test by the date of their
  first activity, using the same 2/3, 1/6, 1/6 proportions over the matured window as §4.
- **Implementation** (`delphi.sensing.mule`, `delphi.sensing.trainers.load_mule_data` and
  `train_mule_artifact`, #91). Where the bullets above are silent:
  - **Label events.** An account is a mule if, by the training cutoff, the bank had an ACCOUNT
    label with the MULE key (a registry listing; investigator dispositions from M5) or a
    complaint naming it as a counterparty. Its label time is the first such event.
  - **Each account's `as_of`.** A mule's features are taken 1 µs before its label time, so the
    model never learns "a complaint (or a listing) means mule": `complaints_linked` is 0 for
    every training mule (#142 review). A non-mule's features are taken at the end of the
    matured window (cutoff − maturity), when the bank had seen no fraud label for it for at
    least the maturity period.
  - **"≥ 30 days of activity"** is read as: opened at least `negative_min_age_days` (30) before
    its `as_of`. Both classes also need an executed payment before their `as_of`, and "first
    activity" is the first such payment's execution time. The 21-day `test` history cannot hold
    30 days of payments.
  - The learning rate (0.1) is not in the bullets above; it lives in `sensing/config.yaml`. The
    gate is enforced on `eval` only, as the bullet says; elsewhere it is reported.
  - With no validation split holding both classes, the scores are left uncalibrated (the report
    says `calibrated: false`).

## 7. Model artifacts and lifecycle

- `TRAIN_SENSING` job → `model_artifacts` rows: `{id, kind (PRIOR|ANOMALY|MULE), version,
  trained_at, training_cutoff, metrics, feature_list, blob (bytea, joblib), sha256, status
  (ACTIVE|RETIRED|FAILED_GATE)}`.
- The seed job trains once after dataset generation: `GENERATE_DATASET` queues
  `TRAIN_SENSING` on the dataset it wrote (key `TRAIN_SENSING:dataset-<id>`, so once). Retraining
  is manual (`POST /admin/sensing/train`; `409 JOB_ALREADY_RUNNING` while one runs).
- **Implementation** (#88): `delphi.sensing.artifacts` stores each model and applies the gate
  rules of §4: a passed gate makes it ACTIVE and retires the previous one; a failed gate stores
  it as FAILED_GATE and keeps the previous ACTIVE one; the first artifact of a kind is activated
  anyway, with `gate_passed = false`. Each activation publishes `artifact.activated`. The
  `TRAIN_SENSING` job (`delphi.sensing.jobs`) runs one trainer per kind, each in its own
  transaction, so a retried job skips the kinds it already stored. A trainer takes a
  `TrainingRequest` (a `delphi_app` connection and the dataset) and returns a `TrainedModel`
  (blob, metrics, feature list, training cutoff, gate result); it picks its own rows and cutoff.
  Kinds without a trainer yet are skipped and listed in the job result. Reading a blob checks
  its SHA-256.
- **The PRIOR trainer** (`delphi.sensing.trainers.train_prior_artifact`, #159) reads the
  payments and the labels the bank had observed (`delphi.labels`, delivered by
  `LABEL_DELIVERY`, doc 14 §8) through `delphi.sim.api`, never `sim_truth`, up to the job's
  **training cutoff**:
  - the seed's `TRAIN_SENSING` (params `cutoff: history_end`) cuts at the end of the dataset's
    history (`stats.history_end`), so the first models learn what the history knew;
  - a manual retrain (`POST /admin/sensing/train`) cuts at the time it runs, so it learns from
    every label delivered since: live complaints, S-4's collect-refund payments, and the labels
    the promoted-typology rule below counts.

  The matured window runs from the history's start to the cutoff minus the maturity. A kind
  with nothing to learn from (no labelled payment in that window) is recorded under
  `not_trained` with its reason, and the job goes on to the next kind: retrying could not help.
  On `test`, seed plus training take about 5 s.
- The model is loaded at api/worker start and on an `artifact.activated` event. Inference
  p95 ≤ 20 ms per payment (NFR-03).
- **Retraining with a promoted typology:** if the ACTIVE KB has typologies that are not model
  classes and have ≥ 50 labels, the retrain adds them as classes. The KB does not change: class
  membership is read from the active artifact (doc 06 §11).
