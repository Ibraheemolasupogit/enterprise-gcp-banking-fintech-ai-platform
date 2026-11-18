# M9A: fraud evaluation v2 (point-in-time features, rolling-origin validation)

Date 2026-11-18. Base `main` at `2bdba3e` (the M9A code was uncommitted at run time; the run manifest records the dirty tree and every source file's SHA-256). Synthetic data only. **No GCP, Terraform, IAM or deployment work.**

## What M9A adds

| Piece | Where | Notes |
| --- | --- | --- |
| Point-in-time behavioural features | `evaluation/behavioural.py` | 12 features, contract `m9a-features-1.0.0`. See [feature definitions](#feature-definitions) |
| Metrics | `evaluation/metrics_v2.py` | PR-AUC, ROC AUC, Brier, ECE (10 equal-width bins), precision/recall at alert capacity and at the frozen threshold, day-block bootstrap 95% CIs, and segments with minimum-support rules |
| Evaluation | `evaluation/evaluation_v2.py`, `configs/fraud_evaluation_v2.yaml` | Rolling-origin folds, logistic regression champion vs HistGradientBoosting challenger, validation-only selection and threshold, and a sealed test window |
| CLI | `financial-crime-ml evaluate-fraud-v2 --dataset-dir <v2 run> [--config] [--run-id]` | Writes to a new `outputs/runs/<id>/` only |
| Dataset config | `configs/synthetic_v2_m9a.yaml` | 20,000 transactions, 8% synthetic prevalence. The defaults and the M7B pilot config are unchanged |
| Tests | `tests/test_evaluation_v2.py` (18) | See [Tests](#tests) |

M1 (`evaluate-fraud`) and its artifacts are unchanged. M9A reuses:
- the M1 row-local features and their validation;
- `manifest.py` provenance and hashing;
- the v2 loader and validator;
- the M1 logistic-regression pipeline shape and run-ID rules.

There are no new dependencies (scikit-learn 1.8.0).

### Feature definitions

For a transaction at instant `t`, each behavioural feature uses only the **observations** (never labels) of transactions with event time **strictly earlier** than `t`. Rows sharing an instant never see each other, so the order of ties cannot change any value. Rows are ordered by `(transaction_timestamp, transaction_id)`. Windows are half-open, `[t − w, t)`.

| Group | Features |
| --- | --- |
| Activity counts | Account count over 10 minutes, 1 hour and 24 hours; customer count over 24 hours; same-account same-amount count over 24 hours; account history count over 30 days |
| Amount and timing | Account amount log-ratio to the 30-day mean (one account has one currency); log seconds since the account's previous transaction (capped at 30 days); `acct_has_prev` |
| Novelty | New beneficiary for the account; new device for the customer |
| Row-local | `is_round_amount`, using the currency exponent |

These are added to the M1 row-local features.

### Leakage prevention
- Features are built only from the 14 observation columns of `transactions.csv`.
- Labels come only from `outcomes.csv`, reading just `transaction_id` and `is_suspicious`. Episode and scenario columns are never read.
- Point-in-time features use only past rows. Test-window rows use earlier observations, as they would in production.

**Splits and folds:**
- The development and test windows are split chronologically, with test the latest 20%.
- Development is cut into 4 time blocks, which never split an instant. Fold k trains on blocks 0..k−1 and validates on block k, for k = 1..3.
- An embargo drops training rows from the hour before each validation start, so a synthetic burst cannot straddle a boundary.

**Selection and threshold**, using pooled out-of-fold validation predictions only:
- The pre-registered rule promotes the challenger only if its bootstrap PR-AUC CI lower bound exceeds the champion's PR-AUC **and** its ECE is no more than 0.02 worse.
- The threshold is the score of the last alert at a 5% alert rate on the fold-3 validation window.
- The deployed model is the fold-3 model. Nothing is refit after the threshold is frozen.

**Sealing:**
- `selection.json` (model, threshold, hashes) is written and hashed before any test label is released.
- `SealedTestWindow` holds the test labels and releases them once, only against that exact frozen record.
- Tests prove that `selection.json` existed at release, that a second release fails, and that no test row appears in the validation predictions.

## Evidence run `m9a-eval-1`

Dataset `m9a-data-1` (`generate-data-v2 --config configs/synthetic_v2_m9a.yaml`):
- validation passed with 0 issues;
- `transactions.csv` `53fce8a1…866f`;
- `outcomes.csv` `8b4d906e…df15`.

This was the **only** run against this dataset. A separate seed-7 dataset in a scratch directory was used for debugging.

| Artifact | SHA-256 |
| --- | --- |
| Feature contract | `ab2d04aa58224d7d9cb5a92ce72f6af12f645d165d22b2f8d582d41988727b35` |
| `selection.json` (frozen) | `846f40a79b9500a3d685578b0cdb0480ed6fd197e340029820409fdb6c91169f` (record digest) |
| `manifest.json` | `6871f8677aab702b9bd1edb2386b607e596af9f723ce2aba3bd08c351d588903` |
| `test_metrics.json` | `0aa9938b73f3b59a1ed084f87210e28c9e5c67cbe9783b1bd902a46b5e23273e` |
| `model_parameters.json` | `e39ed5b55080a8e079151fd2283722681cb6e406b8bf06b5d9e50f7fa23fa6f1` |

The manifest records `test_evaluations: 1`.

### Validation (rolling origin)

Validation PR-AUC by fold (each block 4,000 rows; positives 314 / 357 / 327):

| Fold (validation window) | Train rows | Logistic regression | HistGradientBoosting | M1 row-local reference |
| --- | --- | --- | --- | --- |
| 1 (Mar 14 – May 25) | 3,995 | 0.386 | 0.398 | 0.093 |
| 2 (May 25 – Aug 8) | 8,000 | 0.395 | 0.377 | 0.091 |
| 3 (Aug 8 – Oct 20) | 11,999 | 0.339 | 0.353 | 0.090 |
| **Pooled** (95% CI) | | **0.369** [0.332, 0.410] | 0.372 [0.332, 0.418] | 0.089 [0.079, 0.104] |
| Pooled Brier / ECE | | 0.0589 / 0.0182 | 0.0611 / 0.0273 | 0.0768 / 0.0209 |

**Decision:** the challenger's CI lower bound (0.332) is not above the champion's 0.369, so **logistic regression is kept**. It is the cloud-portable model. The point-in-time features lift pooled PR-AUC from the M1 row-local reference's 0.089 (about the prevalence) to 0.369. Frozen threshold: **0.4639**.

### Final test window (sealed; evaluated once)

4,000 rows, 300 positives (7.5%), 73 days. 95% CIs come from 1,000 day-block resamples, all usable.

| Metric | Value | 95% CI |
| --- | --- | --- |
| PR-AUC | **0.377** | [0.311, 0.453] |
| ROC AUC | 0.876 | [0.857, 0.898] |
| Brier | 0.0533 | [0.0462, 0.0603] |
| ECE | 0.0184 | [0.0135, 0.0247] |
| Precision / recall at 1% capacity | 0.425 / 0.057 | [0.275, 0.561] / [0.037, 0.074] |
| at 2% | 0.487 / 0.130 | [0.370, 0.588] / [0.105, 0.156] |
| at 5% | 0.415 / 0.277 | [0.342, 0.502] / [0.239, 0.330] |
| at 10% | 0.422 / 0.563 | [0.344, 0.496] / [0.498, 0.629] |
| At the frozen threshold | precision 0.415, recall 0.260, alert rate 4.70% (target 5%) | [0.335, 0.500] / [0.219, 0.302] |

The test results agree with validation: pooled validation PR-AUC was 0.369 and test is 0.377. The realised alert rate (4.7%) is close to the 5% design.

### Segments (test window; reported only with ≥200 rows, ≥20 positives and ≥20 negatives)

| Segment | Rows / positives | PR-AUC | Precision / recall at threshold | Alert rate |
| --- | --- | --- | --- | --- |
| EUR | 959 / 73 | 0.315 | 0.327 / 0.233 | 5.4% |
| GBP | 918 / 39 | 0.285 | 0.385 / 0.256 | 2.8% |
| JPY | 1,062 / 91 | 0.392 | 0.431 / 0.242 | 4.8% |
| USD | 1,061 / 97 | 0.493 | 0.492 / 0.299 | 5.6% |
| mobile | 2,011 / 152 | 0.373 | 0.404 / 0.263 | 4.9% |
| web | 1,780 / 131 | 0.389 | 0.435 / 0.282 | 4.8% |
| branch | 209 / 17 | insufficient support | | |
| cross-border | 3,331 / 232 | 0.356 | 0.384 / 0.241 | 4.4% |
| domestic | 669 / 68 | 0.458 | 0.524 / 0.324 | 6.3% |
| Exact corridors (36) | | All insufficient support (largest 178 rows); counts reported only | | |

Alert rates differ by currency (GBP 2.8% vs USD 5.6%) and by corridor type. These are point estimates on small segments, without CIs. They should be monitored, not tuned to.

## Cloud reproduction requirements (for M9C; nothing implemented)

The `feature_contract.json` field `cloud_reproduction`, and each feature's `FEATURE_SPECS` entry, fix the BigQuery definition:
- **Source:** `curated_transactions`. All 14 input columns exist there (tested).
- **Windows:** `RANGE BETWEEN w×10⁶ PRECEDING AND 1 PRECEDING` over `UNIX_MICROS(transaction_timestamp)`, partitioned as specified; the strictly-earlier semantics follow.
- **Means:** `CAST(SUM(amount_minor) AS FLOAT64) / COUNT(*)`.
- **M1 row-local translations:**
  - `LN(1 + CAST(amount AS FLOAT64))`;
  - UTC hour;
  - day of week `MOD(EXTRACT(DAYOFWEEK …) + 5, 7)`, so Monday is 0.
- **Categories:** categories unseen in training map to zero one-hot columns.
- **History scope:** cloud values equal offline values only when the cloud history is the same transaction set. Cold-start rows get empty-history defaults.
- **Parity:** INT64 features must match exactly, and FLOAT64 features within 1e-12.
- **Offline check:** a test reproduces every windowed feature in SQLite window SQL (`RANGE … 1 PRECEDING`) exactly on 2,500 rows.
- **Model export:** `model_parameters.json` (logistic regression) holds the scaler, category vocabularies, coefficients, intercept, threshold and feature version. Scores in the CSVs are written with `%.17g`; read them with `float_precision="round_trip"` for exact parity.

## Tests

- **Oracle:** a row-by-row definition equals the vectorised features.
- **Future invariance:** deleting or rewriting later rows never changes earlier features.
- **Same-instant isolation.**
- **Label separation:** flipping every label or shuffling input order changes nothing.
- **Cloud schema:** the input columns exist in the BigQuery schema.
- **SQLite window-SQL reproduction.**
- **Currency exponent:** round amounts respect it.
- **Folds:** expanding, chronological, embargoed, and never splitting an instant.
- **Metric definitions:** hand-computed Brier and ECE, and capacity tie-breaks.
- **Bootstrap:** determinism and single-class skips.
- **Segment support:** minimum-support rules.
- **Promotion rule:** three cases.
- **Sealed window:** single release, and only to the frozen record.
- **Run order:** selection is frozen before the test labels are read.
- **Thresholds and validation hygiene:** the threshold is validation-only, and the validation and test sets don't overlap.
- **Artifacts:** hashes are recorded, and a run ID can't be reused.

## Limitations

- **Synthetic labels:** the v2 generator's labels are not confirmed fraud. Benign controls deliberately reuse the burst, round-payment and spike mechanisms, and the first transaction of each burst has no history. Together these cap precision at about 0.4–0.5. That reflects the generator, not real-world performance.
- **One dataset, one seed, one test window.** Fold metrics share history and are not independent. CIs are day-block percentile intervals and assume independence between days.
- **Calibration:** neither model uses class weights or post-hoc calibration. ECE is low on this data, but there is no guarantee under prevalence shift.
- **Segments:** exact corridors and the branch channel lack support. Segment results are point estimates without CIs, are not a fairness assessment, and use no protected attributes.
- **Deployment trade-off:** the deployed model is the fold-3 model, trained before the last development block, so recency is traded for a validation-only threshold.
- **Challenger portability:** HistGradientBoosting is not cloud-portable in the M9C plan. Had it won, M9C would have needed a different scoring path.

## Readiness for M9B

M9B is ready to start. It has:
- a frozen logistic-regression model with full parameters in `model_parameters.json`;
- the feature contract;
- exact validation and test predictions (`%.17g`);
- the partition, fold and selection records.

M9B needs per-decision logit contributions (an exact additive decomposition is possible for the logistic regression), reason codes, permutation importance across folds, and a model card generated from `m9a-eval-1`.
