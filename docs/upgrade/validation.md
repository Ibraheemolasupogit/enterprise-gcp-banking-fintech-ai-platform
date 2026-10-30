# Upgrade M1 executed validation

This records checks actually executed during M1. It is not an inherited sample
report or a claim of cloud deployment. The implementation remains uncommitted;
Git HEAD is `27e955fbac442483dc90d6fa2722770fd9418ba7`.

## Environment

Existing local environment; no dependency installation or dependency-file change:

| Component | Version |
| --- | --- |
| Python | 3.12.8 |
| NumPy | 2.4.6 |
| pandas | 2.3.3 |
| SciPy | 1.17.1 |
| scikit-learn | 1.8.0 |
| PyYAML | 6.0.3 |

Python 3.11 CI execution was not independently observed. Cross-environment
bit-for-bit numerical reproducibility has not been established.

## Commands and results

From the working repository:

```bash
python3 -m ruff check .
python3 -m pytest -q
python3 -m financial_crime_ml.cli evaluate-fraud --run-id m1-fraud-1
python3 -m financial_crime_ml.cli evaluate-fraud --run-id m1-fraud-2
python3 -m financial_crime_ml.cli evaluate-fraud --help
python3 -m financial_crime_ml.cli
```

- Ruff: `All checks passed!` (exit 0).
- Full pytest: **115 passed in 6.36s**, no warnings (exit 0).
- Both evaluator CLI runs: completed (exit 0), with one held-out test evaluation
  each. The status command and new-command help also exited 0.
- An earlier focused suite passed 39 tests with one timestamp-inference warning.
  The parser was then made explicitly ISO 8601; the full suite above passed
  without that warning.

The tests cover label removal/permutation and extra leaked columns, future-row
and input-order independence, training-only scaling/categories, chronological
ordering and tied timestamp boundaries, single-class rejection in all three
partitions, invalid inputs/configuration, threshold tie policy, call ordering and
threshold independence from test labels, independent metrics, hashes, failure
records, reproducibility, overwrite/path rejection, sample preservation and
legacy CLI dispatch. Existing regression tests are included in the total.

## Actual baseline results

No tuning was performed against these test results. Both runs used the checked-in
configuration (seed 42), 10 allowlisted input features and the same source fingerprint:

`781e3a5f968e7d1842486b2befb56bd6584f7e62c5db6f56bfc74ec6e788c953`

| Partition | Rows | Positive labels | UTC start | UTC end |
| --- | ---: | ---: | --- | --- |
| Train | 3,000 | 555 | 2025-01-01 02:01 | 2025-08-03 13:56 |
| Validation | 1,000 | 182 | 2025-08-03 13:59 | 2025-10-17 10:58 |
| Test | 1,000 | 163 | 2025-10-17 11:27 | 2025-12-31 21:48 |

| Test metric | Value |
| --- | ---: |
| Validation-selected threshold | 0.60 |
| Precision | 0.3879781420765027 |
| Recall | 0.43558282208588955 |
| F1 | 0.41040462427745666 |
| ROC AUC | 0.716985142672853 |
| Average precision | 0.5019424448862164 |
| True negatives / false positives | 725 / 112 |
| False negatives / true positives | 92 / 71 |

Confusion matrix, with rows actual and columns predicted `[0, 1]`:
`[[725, 112], [92, 71]]`.

An independent standard-library Python CSV check recomputed precision, recall,
F1, pairwise ROC AUC and grouped-score average precision, and reconciled them
within `1e-12`. It also reconstructed the best threshold from validation rows,
verified every manifest artifact hash and compared all eight non-manifest
artifacts byte-for-byte across the two runs. Manifests appropriately differ in
run ID and execution timestamps. No data/configuration/feature changes were made
between the runs, and no thresholds were selected on test data.

Local ignored evidence (available in this workspace, not committed):

- `outputs/runs/m1-fraud-1/manifest.json` and `metrics.json`
- `outputs/runs/m1-fraud-2/manifest.json` and `metrics.json`
- `outputs/runs/m1-validation-checks/reconciliation.json`

## Legacy demo and CLI compatibility

The legacy demo ran only in `/private/tmp/gcp-fincrime-m1-17liiode`, a disposable
copy of the working files. `PYTHONPATH` was set to that copy's `src/`. A preliminary
import assertion verified that `financial_crime_ml.ingestion.load_data.REPO_ROOT`
resolved to the disposable copy, not this repository.

Commands executed there, all exit 0:

```bash
bash scripts/run_all_local.sh
python3 scripts/final_project_check.py
python3 -m financial_crime_ml.cli generate-data
python3 -m financial_crime_ml.cli validate-data
python3 -m financial_crime_ml.cli build-features
python3 -m financial_crime_ml.cli train-fraud-model
python3 -m financial_crime_ml.cli run-anomaly-detection
python3 -m financial_crime_ml.cli run-network-risk
python3 -m financial_crime_ml.cli run-nlp-triage
python3 -m financial_crime_ml.cli run-monitoring
python3 -m financial_crime_ml.cli generate-governance-pack
python3 -m financial_crime_ml.cli validate-docs
python3 -m financial_crime_ml.cli final-check
```

Actual command arguments, working directory, timestamps, exit codes and stdout/
stderr logs are retained in ignored `outputs/runs/m1-validation-checks/`.
`legacy_validation.json` indexes the commands and `legacy-*.log` files.

## Preservation and scope checks

All 43 inherited artifacts match both the baseline SHA-256 inventory and original
Git blob bytes at the inherited revision. `git check-ignore --quiet` verified
both run manifests are ignored. Changes are limited to evaluation code/config,
governance output isolation, tests and documentation; no sample, dependency,
infrastructure or workflow files were changed. No commit, push, cloud command,
external service, install, repository rename or new framework was used.

## Remaining risks

These are synthetic suspicious-transaction outcomes, not production fraud
performance. The inherited AML/graph/NLP/anomaly and generator defects remain
documented in [baseline.md](baseline.md). There is no calibration, fairness or
independent model validation. Test-set reuse across future manual experiments
must not become a tuning loop. Run snapshots retain the input CSV locally and
are intended for synthetic data only. Integrity hashes are not signed approval
records, and no model is registered or deployed.
