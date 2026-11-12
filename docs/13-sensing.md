# 13 — Sensing layer

Research §10 (L1). Module `delphi.sensing`. This is the state of the art, **not the USP**: it
produces calibrated priors and scores that the engine consumes.

## 1. Outputs

| Output | Consumer | Shape |
|---|---|---|
| `p_model(h)` over model hypotheses {GENUINE, ATO, APP_DIGITAL_ARREST, APP_INVESTMENT, MULE} | Guardian prior | dict, sums to 1 |
| `q` anomaly percentile ∈ [0, 1] | Guardian prior (open mass) | float |
| `p_mule(account)` | BENEFICIARY_INTEL, ACCOUNT_REVIEW prior, Chase | float |
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

- LightGBM multiclass (`objective=multiclass`, 5 classes). Hyperparameters are fixed in
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
- Calibration: temperature scaling (a single T > 0 fitted on validation NLL), applied to the
  logits.
- Reported metrics (stored with the artifact), on the test split against observed labels:
  per-class one-vs-rest AUC, macro AUC, multiclass log loss, and expected calibration error
  (15 equal-width bins on max-probability).
- **Gate (enforced only on `eval` and `demo`; `test` is reported only):** ECE ≤ 0.05 and macro
  AUC ≥ 0.85.
  - If a retrain fails the gate, the artifact is stored as `FAILED_GATE` and the previous
    artifact stays active.
  - If **no** artifact exists yet (the first training), the artifact is activated anyway with
    `gate_passed = false`, and the Overview page shows a "sensing below quality gate" warning.
    The demo must always have a model.
- The evaluation's own metrics (doc 22) use `sim_truth`. The gate does not.

## 5. Anomaly detector

- scikit-learn `IsolationForest(n_estimators=200, contamination="auto", random_state=7)` on the
  payment features of training-window payments labelled GENUINE.
- `q` = the empirical percentile of the payment's anomaly score (−`score_samples`) within the
  training scores (stored as a sorted array of 10,001 quantiles in the artifact).

## 6. Mule scorer

- Account features: `inbound_distinct_payers_48h`, `inbound_count_7d`, `inbound_sum_7d_log`,
  `median_dwell_minutes_7d`, `outbound_to_inbound_ratio_7d`, `account_age_days`,
  `kyc_minimal`, `device_shared_accounts_30d`, `cash_out_share_7d`, `complaints_linked`
  (as of the time).
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
- The model is loaded at api/worker start and on an `artifact.activated` event. Inference
  p95 ≤ 20 ms per payment (NFR-03).
- **Retraining with a promoted typology:** if the ACTIVE KB has typologies that are not model
  classes and have ≥ 50 labels, the retrain adds them as classes. The KB does not change: class
  membership is read from the active artifact (doc 06 §11).
