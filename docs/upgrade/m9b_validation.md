# M9B: explainability and model governance

Date 2026-11-18. Built on M9A run `m9a-eval-1` (uncommitted working tree, as in M9A). Synthetic data only. **No GCP, Terraform, IAM, deployment, retraining of the champion, or use of test labels.**

## What M9B adds

| Piece | Where |
| --- | --- |
| Exact contributions, reconstruction checks, reason codes, fold replicas, permutation importance, Spearman stability | `evaluation/explainability.py` |
| Model card and champion/challenger decision record, generated from evidence | `governance/model_card_v2.py`. Reuses the legacy card's section structure and `model_risk_assessment.VALID_READINESS_STATUSES` |
| Run orchestration and CLI | `governance/m9b_workflow.py`; `financial-crime-ml explain-fraud-v2 --m9a-run <dir> [--run-id]` |
| Tests | `tests/test_explainability.py` (15) |

No new dependencies. Spearman is computed with pandas ranks; permutation importance uses scikit-learn.

## Method

**Inputs**
- M9B refuses an M9A run unless:
  - every M9A artifact hash matches;
  - the selection digest matches the manifest;
  - the champion is the logistic regression;
  - the feature-contract hash equals the code's.
- Features are recomputed from the dataset only after its hash is checked against the M9A manifest. The recomputed row order must equal M9A's `partitions.csv`.
- M9B never writes into the M9A run.

**Contributions** come only from the frozen `model_parameters.json`, with no refit:

`logit = intercept + Σ coefᵢ·(xᵢ − meanᵢ)/scaleᵢ + Σ coef(active category)`

- There is one contribution per model input feature (22). A category unseen in training contributes 0.
- Numeric contributions are relative to the training mean. Categorical ones are relative to "no category active", the one-hot reference.

**Reconstruction checks** (fail closed). The contributions must match, within 1e-9:
- an independent transformed design-matrix product;
- the **original model's logit**, recovered from M9A's stored probabilities.

The probabilities must also match within 1e-12. This is checked on the 4,000 test rows and on the 4,000 fold-3 validation rows.

**Reason codes**
- Every stored alert (`alert = 1`) receives exactly three codes, taken from the risk-increasing contributions.
- Ranking is by contribution descending, with ties broken by the fixed feature-contract order.
- If fewer than three contributions are positive, the remaining slots are filled with `R00 No further risk-increasing factor`.
- The mapping is stable and versioned (`m9b-reasons-1.0.0`): 22 unique codes in human-readable text, saved as `reason_code_mapping.json`.
- Alert flags and scores are read, never changed.

**Permutation importance (validation folds only)**
- M9A saved only the champion's parameters, so its six fold models (2 models × 3 folds) are re-created with M9A's code, config, seed and rows.
- A replica is used only if its validation predictions are **bit-identical** to M9A's stored ones, and the fold-3 replica's coefficients must equal the frozen champion's. Otherwise M9B aborts.
- These replicas are diagnostic only. The deployed champion is never refit or replaced.
- Training labels come from development rows only: test-window labels are dropped on load. The manifest records `test_labels_read: false`.
- Scoring is the PR-AUC drop, with 10 repeats and seed 42 + fold.

**Stability**
- **Primary:** pairwise Spearman (average ranks) of mean importance across folds, over all 22 features, plus the top-5 features common to all folds.
- **Supplementary, not pre-registered:** Spearman over material features (|importance| ≥ 0.005 in some fold). I added this after seeing that the all-feature ranks are dominated by about 11 near-zero features.

## Evidence run `m9b-explain-3`

M9A's directory hash was `96eafe4e…f426` before and after every M9B run, so M9A is unchanged. Runs `m9b-explain-1` and `-2` are superseded: run 1 lacked the supplementary metric, and run 2 rendered the stability evidence badly. Their computational outputs are byte-identical to run 3's.

| Check | Result |
| --- | --- |
| Max \|logit error\| vs original model (test / validation fold 3) | 1.8e-15 / 1.8e-15 (tolerance 1e-9) |
| Max \|logit error\| vs design matrix | 1.2e-15 / 1.8e-15 |
| Max \|probability error\| vs original model | 3.3e-16 / 3.3e-16 (tolerance 1e-12) |
| Fold replicas reproducing M9A validation predictions bit-for-bit | 6 / 6 |
| Alerts with exactly three reason codes | 188 / 188 (0 padded) |
| Most frequent first codes | R21 round amount (118), R10 10-minute account activity (53), R17 time since previous payment (17) |
| Test labels read | No |

**Permutation importance (PR-AUC drop) and stability**

| Model | Mean Spearman, all 22 features (pairs 1-2 / 1-3 / 2-3) | Material features, supplementary | Top-5 common to all folds |
| --- | --- | --- | --- |
| Logistic regression | 0.202 (0.092 / 0.147 / 0.365) | 0.479 over 11 | 10-minute count, seconds since previous, round amount |
| HistGradientBoosting | 0.192 (0.446 / −0.042 / 0.173) | 0.205 over 12 | amount, round amount |

**Reading the stability figures.** The logistic regression relies on the same three burst and round-payment signals in every fold:

| Fold | Seconds since previous | Round amount | 10-minute count |
| --- | --- | --- | --- |
| 1 | 0.20 | 0.15 | 0.11 |
| 2 | 0.18 | 0.13 | 0.12 |
| 3 | 0.14 | 0.12 | 0.06 |

HistGradientBoosting switches between substitute burst signals: the 10-minute count is worth 0.15–0.18 in folds 1–2 but 0.001 in fold 3, where seconds-since-previous (0.19) takes over. The low all-feature Spearman for both models mostly reflects random ranks among near-zero features.

**Artifacts** (SHA-256):

| Artifact | SHA-256 |
| --- | --- |
| `manifest.json` | `b9f9926eaa77804eb351668c2084349bef134e65c2b38f9007655fc57511e594` |
| `model_card.md` | `47980b436b7d67ea379b7671347beef6c658104ff7cc63f284b569d22fa43cf4` |
| `model_card.json` | `41845ecee41ffbab2e64c582df51a4a68b89e8cffe146a4fc9ff4a566fa2a0d2` |
| `decision_record.md` | `7e4842bcfd09b3212ef5d3d86e4f869d75e2740fae03813e7447894c765b0fc2` |
| `decision_record.json` | `916a9e48cdf06d8dd9d97709135e05336455f3eccc667771b3bee9a6e36edccf` |
| `contributions_test.csv` | `b2109478d2a0828e2190af14b915e78e5de5ad21ddc88e16c3e7abb68546b8b2` |
| `reason_codes.csv` | `64fd26953534519452915ff0c62f44cb3de63c31b6ba1397f1f8ae2468f9263f` |
| `permutation_importance.json` | `09fcecddc7cd9df55fd39051ff22462fcabf786233160eeb511d93e4ab3e2a07` |

**Decision record `DR-M9B-m9a-eval-1`:** keep the logistic regression. Evidence:
- the challenger's CI lower bound (0.332) is not above the champion's 0.369;
- the champion has the better Brier and ECE;
- the champion is portable to exact SQL;
- the champion's decisions decompose exactly into contributions;
- the champion's top features are stable across folds.

The test window was not used for the decision. **Approval status: `pending_human_review`.** No approver is recorded by code.

**Model card:** generated from the M9A manifest, selection, metrics, feature contract and parameters plus the M9B outputs. It covers provenance (including M9A's dirty-tree flag), selection, test metrics with CIs, calibration, segment limits, explainability, leakage controls, synthetic-data limitations, intended and prohibited uses, and readiness `not_production_ready`.

## Tests (15)
- **Reconstruction:** contributions reconstruct the original model, and columns sum to the logit.
- **Unseen categories:** contribute 0 and stay consistent with the design matrix.
- **Tampering:** tampered parameters fail reconstruction, probabilities of 0 or 1 are rejected, and a tampered M9A artifact is refused (hash mismatch).
- **Reason codes:** deterministic tie-break order, R00 padding, a complete and unique mapping, and every alert getting exactly three codes, all with positive contributions.
- **Spearman:** ties and constant inputs (returning `None`).
- **Importance:**
  - validation-only (no test-window ids in the labels used);
  - deterministic for a seed;
  - a replica differing from M9A by 1e-15 in one score is rejected.
- **Stability:** report shape, and the material-feature filter.
- **Governance outputs:** the card and decision record match the M9A evidence and include every required section.
- **Integrity:** M9A's directory is byte-identical afterwards, artifact hashes are recorded, and M9B refuses to write into the M9A run.

## Limitations
- **What contributions explain:** they explain the model's arithmetic, not causation. Numeric contributions depend on the training-mean baseline, and categorical ones on the one-hot reference. Reason codes describe risk-increasing terms only.
- **Importance:** permutation importance on correlated burst features shares credit between substitutes. Fold-level importances use 4,000 rows each.
- **Stability metric:** the supplementary material-feature Spearman was defined after seeing results. It is diagnostic, not a pre-registered criterion.
- **Approval:** human approval of the decision record is still outstanding.
- **Data:** all M9A synthetic-data limitations apply.

## Readiness for M9C

M9C (offline SQL scoring parity, then a gated BigQuery run) has everything it needs:
- **Champion:** the frozen champion parameters, with an exact, verified decomposition. The SQL logit can be checked term by term against `contributions_test.csv`.
- **Features:** the feature contract and its cloud-reproduction requirements (from M9A).
- **Reason codes:** a deterministic reason-code definition that SQL can reproduce, ranked by contribution with the fixed feature-order tie-break.
- **Governance:** a model card and decision record to attach to the approval.

Still needed before any cloud step:
- human approval of `DR-M9B-m9a-eval-1`;
- committing M9A and M9B, so that the evidence run can be tied to a commit.
