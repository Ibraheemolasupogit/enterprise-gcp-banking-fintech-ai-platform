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
