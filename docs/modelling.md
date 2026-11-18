# Fraud modelling and evaluation

## Upgrade M1 evaluation

Run from the repository root using the existing environment:

```bash
python3 -m financial_crime_ml.cli evaluate-fraud --run-id m1-review
```

The installed entry point also accepts `financial-crime-ml evaluate-fraud`.
`--input` selects a local transaction CSV and `--config` selects the evaluation
YAML. Defaults are `data/sample/transactions.csv` and `configs/fraud_evaluation.yaml`.
Omit `--run-id` for a UUID-based name. CLI output always goes under
`outputs/runs/`; IDs cannot include paths or overwrite an existing run.

### Feature contract

`evaluation/contracts.py` defines the complete allowlist:

- Numeric: `transaction_amount_log`, UTC `transaction_hour`, UTC
  `transaction_day_of_week`, `is_cross_border_transaction`.
- Categorical: `currency`, `transaction_type`, `merchant_category`, `origin_country`,
  `destination_country`, `channel`.

These are row-local transaction observations. There are no entity joins,
aggregates, labels, helper patterns, account suspicious rates or learned features
in feature construction. Extra input columns are ignored. Labels can be changed
or removed without changing features. Future records cannot change earlier
feature values. Future features require a reviewed code change to the allowlist.
Missing/invalid required observations fail rather than being silently repaired.
Timestamps must be ISO 8601 (naive timestamps are interpreted as UTC); IDs must be
unique. Only binary target labels are accepted.

### Time and learning boundaries

Transactions are sorted by UTC timestamp and transaction ID. Default fractions
are 60% training, 20% validation and the remainder test. A timestamp group that
crosses a boundary stays in the earlier partition, so actual counts can differ
from the fractions. Time windows must be nonempty and strictly separated; each
partition must contain both classes. There is no random shuffle or stratification.

StandardScaler and OneHotEncoder are fitted only on training data, followed by
balanced Logistic Regression with the fixed `liblinear` solver, 1,000 maximum
iterations and the configured seed. Unknown later categories are ignored by the
trained encoder. No preprocessing or model refit follows threshold selection.

The fixed configured threshold grid is evaluated only on validation labels.
Choose maximum F1, then precision, recall and higher threshold to break ties.
The frozen threshold is applied to the final test window; test predictions and
metrics are computed once per run. No selection uses test metrics. Repeated
manual tuning against this same test window would still undermine independence.

### Execution artifacts

Each new `outputs/runs/<run-id>/` contains:

| Artifact | Evidence |
| --- | --- |
| `manifest.json` | Actual status/timestamps, input/config/source hashes, Git revision and dirty state, seed, versions, feature names, partition times/counts and selected threshold |
| `input.csv`, `config.yaml` | Exact consumed bytes, read once and copied for reproducibility |
| `partitions.csv` | Transaction IDs, UTC timestamps and partition assignments |
| `validation_predictions.csv` | Validation outcomes and scores at the selected threshold |
| `validation_thresholds.csv` | Complete configured validation threshold sweep |
| `test_predictions.csv` | Held-out outcomes, probabilities and predictions |
| `metrics.json` | Test precision, recall, F1, ROC AUC, average precision and confusion counts |
| `model_parameters.json` | Fitted training scaler/category state, coefficients and transformed feature names; not a serving artifact |

Completed manifests hash every other run artifact. Failures record their real
exception and failed status; they do not claim successful completion. A reused
run ID fails before changing any files. Git revision plus source hashes identify
uncommitted code; hashes are integrity checks, not signatures or control approval.
No fitted-model deserialization or external model/service is used.

## Inherited milestone 5: legacy demonstration

`train-fraud-model`, its scripts, output schemas and sample artifacts are retained
for compatibility. The inherited workflow reads `transaction_features.csv`, uses
numeric/boolean feature inference, scales training data and performs a seeded
random 80/20 split. It excludes the raw target and `suspicious_pattern`, but not
derived label features or aggregates computed using future/holdout records.
Its saved metrics and model card must not be used as generalisation evidence.
Run the legacy demo only in a disposable local copy when validating M1.

## Residual limitations

The target is a synthetic suspicious flag, not confirmed fraud. The generator's
chronology, behaviour and prevalence are illustrative. M1 does not fix AML's
target context, graph label dependence, NLP label fallback, anomaly leakage,
cross-currency aggregation or simulated governance events. It does not implement
population scoring, calibration, uncertainty, fairness, independent validation,
model registry, APIs, investigator decisioning or GCP deployment. Different
features/splits make old and new metrics unsuitable for direct comparison.

## M9A evaluation v2

```bash
python3 -m financial_crime_ml.cli generate-data-v2 --config configs/synthetic_v2_m9a.yaml --run-id <data-id>
python3 -m financial_crime_ml.cli evaluate-fraud-v2 --dataset-dir outputs/runs/<data-id> --run-id <eval-id>
```

M9A adds the following on top of the M1 row-local features (M1 itself is unchanged):
- **Strictly point-in-time behavioural features.** Each uses only earlier event times and observations, never labels.
- **Rolling-origin validation:** three expanding folds with a one-hour embargo.
- **Champion and challenger:** a logistic regression champion and a HistGradientBoosting challenger, with a pre-registered, validation-only promotion rule.
- **Frozen threshold:** set at the alert capacity on the last validation fold.
- **Sealed test window:** evaluated once, after the selection is frozen and hashed.

Reported metrics:
- PR-AUC, Brier and ECE;
- precision and recall at alert capacity and at the threshold;
- day-block bootstrap 95% CIs;
- currency, channel and corridor segments with minimum-support rules.

The cloud reproduction requirements are part of the feature contract. Definitions, evidence and limitations are in [M9A validation](upgrade/m9a_validation.md).

## M9B explainability and governance

```bash
python3 -m financial_crime_ml.cli explain-fraud-v2 --m9a-run outputs/runs/<m9a-run> --run-id <id>
```

M9B explains the frozen M9A logistic-regression champion without retraining it:
- **Contributions:** exact per-decision logit contributions, verified against the original model's logits within 1e-9.
- **Reason codes:** three deterministic codes for every alert.
- **Importance:** permutation importance on validation folds only, using fold replicas verified bit-for-bit against M9A, with Spearman rank stability across folds.
- **Governance:** a model card and a champion/challenger decision record, both generated from the run evidence.

See [M9B validation](upgrade/m9b_validation.md).

## M9C portable model and SQL scoring

```bash
python3 -m financial_crime_ml.cli export-fraud-model --m9a-run outputs/runs/<m9a> \
  --m9b-run outputs/runs/<m9b> --run-id <id>
```

M9C exports the frozen champion as a hashed, canonical specification and generates GoogleSQL scoring from it:
- strictly past-only features;
- INT64 parts-per-million scores;
- reason codes;
- a transactional refresh and a read-only preflight.

The same builder's SQLite rendering is checked against the Python reference on every row, with exact integer, alert and reason-code parity. A label-free monitoring reference profile with PSI is included. The deployable specification is v2 (`m9c-spec-2.0.0`, approved Option B), which alerts on the integer rule `score_ppm >= 463905`. Its approval is limited to offline development and preparing the synthetic pilot; production approval is pending. See [M9C validation](upgrade/m9c_validation.md); GoogleSQL execution belongs to gated M9D.
