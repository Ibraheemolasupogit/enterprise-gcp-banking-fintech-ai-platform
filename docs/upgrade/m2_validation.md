# M2 validation record

## Scope and provenance

M2 adds a versioned local synthetic-data contract alongside the inherited generator. The repository remains an in-place upgrade with its existing Git history, package layout and legacy CLI. The historical sample files remain legacy demonstration evidence; M2 creates fresh datasets under ignored `outputs/runs/<run-id>/` paths. No legacy sample was rewritten.

Implementation and validation began at revision `7ab53915990ae5dc9bd742f0904e07bf38e61c27` (`feat: establish leakage-safe fraud evaluation baseline`). The implementation source fingerprint recorded during independent verification was `978817a5103b97538455f4e9b6e3519c151637d632cb36485a6b36eb343f88f5`. Each run manifest records the source-file hashes and revision/dirty state as well as dataset payload hashes, configuration, contract version, seed and library versions. The 43 inherited artifacts were checked against both the M0 baseline hashes and their original Git blobs; all 43 were unchanged.

## Data contract

Contract version `2.0.0` generates customers, accounts, beneficiaries, devices, transactions, outcomes and an M1-compatible evaluation export. Each entity has a primary key; transactions explicitly reference their customer, account, beneficiary and device. Ownership and all references are validated. Customer onboarding, account opening, beneficiary/device first-seen and transaction event times must form a valid chronology. Transaction times are UTC, second precision, within the configured half-open event window. The configured end is a deterministic fictional snapshot boundary, not a claim about real-world current time.

Amounts are positive integer minor units with an explicit supported currency exponent and a canonical decimal-string representation. Currency must match the account; totals are reported separately by currency, with no implied FX conversion. The transaction table has no outcome, episode or scenario fields. Labels and scenario descriptions live in a separate outcome table; the M1 export contains only its required target column in addition to transaction observations. Behavioural episodes include velocity bursts, repeated round payments and account-profile amount spikes, with benign examples of the same patterns. The generator does not add predictors by reading the final label.

These are synthetic scenario assumptions, not estimates of real fraud. The scenario mix, private amount profile, event timing, country choices and fraud prevalence are deliberately generated. A generated event time does not establish that a real institution would have observed the entity then. There is no FX ledger, refunds/chargeback lifecycle, account closure or real identity evidence. AML typologies, graph effects, NLP, and the limits of synthetic data remain unresolved; see [the contract and assumptions](../synthetic_v2_contract.md).

## Executed validation

Environment: Python 3.12.8, NumPy 2.4.6, pandas 2.3.3, SciPy 1.17.1, scikit-learn 1.8.0, PyYAML 6.0.3. No dependency was added or installed for M2.

Commands run:

```text
python3 -m ruff check .
python3 -m pytest -q
python3 -m financial_crime_ml.cli final-check
python3 -m financial_crime_ml.cli validate-docs
python3 -m financial_crime_ml.cli generate-data-v2 --run-id m2-data-1
python3 -m financial_crime_ml.cli generate-data-v2 --run-id m2-data-2
python3 -m financial_crime_ml.cli validate-data-v2 --dataset-dir outputs/runs/m2-data-1 --run-id m2-check-1
python3 -m financial_crime_ml.cli evaluate-fraud --input outputs/runs/m2-data-1/evaluation_transactions.csv --run-id m2-fraud-1
python3 -m financial_crime_ml.cli validate-data-v2 --dataset-dir <disposable-invalid-fixture> --run-id m2-invalid-1
python3 -m financial_crime_ml.cli generate-data-v2 --seed 7 --customers 10 --accounts 15 --beneficiaries 30 --devices 20 --transactions 600 --fraud-prevalence 0.15 --run-id m2-cli-options
```

Ruff reported **All checks passed**. Pytest reported **191 passed in 15.52s** with no warnings. The legacy final check passed 79 checks with zero issues; legacy documentation validation passed 6 documents and 5 diagrams with zero issues. New generation and validation help commands also exited successfully.

The two default generation runs produced byte-identical contents for all 10 hashed payload artifacts. Independent verification of the generated rows found zero foreign-key, ownership, chronology or monetary-rule violations. The validator passed all 125 checks. The data counts were:

| Table | Rows |
| --- | ---: |
| customers | 100 |
| accounts | 150 |
| beneficiaries | 300 |
| devices | 200 |
| transactions | 5,000 |
| outcomes | 5,000 |
| evaluation_transactions | 5,000 |

Exactly 400 transactions (8%) were suspicious. Scenarios counted 200 velocity-burst, 150 round-payment and 50 amount-spike suspicious events, plus 260 benign velocity, 195 benign round-payment and 405 benign amount-spike events; 3,740 rows were background. Transactions by currency were EUR 1,300, GBP 1,092, JPY 1,024 and USD 1,584. Minor-unit totals were kept separate: EUR 57,234,727; GBP 50,184,152; JPY 429,539; USD 68,289,025.

The invalid-fixture CLI check altered a decimal amount to a noncanonical precision and correctly exited 1, reporting both `transactions.exact_money` and the resulting `evaluation_transactions.observation_amount` mismatch. The configurable CLI run exited 0 with 10 customers, 15 accounts, 30 beneficiaries, 20 devices and 600 transactions; prevalence was exactly 15% (90 positive labels).

## M1 compatibility and metrics

The M1 `evaluate-fraud` command consumed the generated M1 export without an adapter or change to M1 code. Its chronological partitions were 3,000 train rows (250 positives), 1,000 validation rows (67 positives) and 1,000 test rows (83 positives). The validation-selected threshold was 0.2. On the held-out test partition, precision was 0.0827, recall 0.9398, F1 0.1520, ROC AUC 0.4873 and average precision 0.0837; confusion counts were TN 52, FP 865, FN 5 and TP 78. These results are weak and are retained as measured evidence. No tuning was done against the held-out test set, and no improvement is claimed. The row-local M1 feature set does not express the temporal sequence patterns represented by the scenarios.

Historical fraud metrics and simulated governance records remain **legacy demonstrations**, not production evidence. The legacy CLI paths are preserved. The new seven-table dataset contract is an additional workflow and does not replace or silently reshape legacy samples.

## Evidence locations and residual limits

Ignored run evidence is in `outputs/runs/m2-data-1/`, `outputs/runs/m2-data-2/`, `outputs/runs/m2-check-1/`, `outputs/runs/m2-fraud-1/` and `outputs/runs/m2-cli-options/`. Independent integrity and command checks are in `outputs/runs/m2-validation-checks/`. These run outputs are local and ignored by Git. Generation is deterministic for a fixed source/configuration/environment and seed; cross-version or cross-platform byte identity was not tested. No cloud resources, external services or deployments were used. These checks establish local contract behavior only, not real-world fraud validity or operational production readiness.
