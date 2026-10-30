# M4 local execution evidence

M4 adds four versioned financial analytics marts, built with local SQLite from verified M3 curated observations. [Contracts and migration plan](../financial_analytics.md) define grains, metrics, timestamps, currency rules, lineage and limitations. No BigQuery execution or GCP deployment is claimed.

## Changes and provenance

Starting revision: `9c33546a32e2ad0a6dadfa8d1adc57611215a321` (`feat: implement enterprise financial data foundation`). M4 was not committed or pushed. Executed implementation fingerprint: `4615f430cedbf1020fcbcfeea11cda84ffcc685eaf06faf4ef46116799be6118`. Executed SQL SHA256: `31e4a39010d483e8c50b3c990e0da9b37626185456d45e88eaa9cb609136410b`. SQLite version: `3.45.3` (Python 3.12.8 environment).

Files changed:

- `src/financial_crime_ml/cli.py`: additive `build-financial-marts` dispatch.
- `src/financial_crime_ml/analytics/{__init__,contracts,workflow}.py`: analytics version/grains, proposed target field definitions, source verification, SQLite execution, lineage, reconciliation and idempotent run handling.
- `sql/analytics/v1/marts.sqlite.sql`: executed SQL.
- `sql/analytics/v1/bigquery.schemas.json` and `marts.bigquery.proposed.sql`: proposed target schemas and untested GoogleSQL translation.
- `tests/test_financial_analytics.py`: 25 new cases.
- `docs/financial_analytics.md`, this evidence record, `README.md`, `docs/index.md`.

No dependencies or inherited sample artifacts changed.

## Executed commands and results

```text
python3 -m ruff check .
python3 -m pytest -q
python3 -m financial_crime_ml.cli generate-data-v2 --run-id m4-source
python3 -m financial_crime_ml.cli ingest-financial-data --dataset-dir outputs/runs/m4-source --source-id m4-synthetic-v2 --source-time 2026-01-01T00:00:00Z --ingestion-time 2026-01-02T00:00:00Z --run-id m4-foundation
python3 -m financial_crime_ml.cli build-financial-marts --source-run outputs/runs/m4-foundation --run-id m4-analytics
# The identical analytics command above was executed again, returning Reused: True.
python3 -m financial_crime_ml.cli build-financial-marts --source-run outputs/runs/m4-foundation --run-id m4-analytics-repeat
python3 -m financial_crime_ml.cli evaluate-fraud --input outputs/runs/m4-source/evaluation_transactions.csv --run-id m4-m1-evaluation
python3 -m financial_crime_ml.cli final-check
python3 -m financial_crime_ml.cli validate-docs
```

Ruff: **All checks passed**. Full pytest: **244 passed in 23.71s**, no warnings. Legacy final check: 79 checks, zero issues. Documentation validation: 6 configured documents and 5 diagrams, zero issues; the existing validator does not discover all new Markdown documents automatically. `git diff --check` passed. The test session verifies all 43 inherited sample hashes before/after execution.

Tests independently derive every group and metric in Python, check SQL results and unique grains, validate dimension joins and lineage, and cover currency-separated totals, required fields, malformed records, source tampering, rehashed inconsistent observations, reconciliation mismatch, failed source status, path traversal in source manifests, empty snapshots, partial M3 acceptance, idempotent unchanged files, run collisions and artifact tampering. SQL mutation tests verify that nulls, duplicate grains, lost rows and incorrect counts fail the build.

## Observed mart results

M2 generated and validated a fresh default dataset. M3 accepted all 5,000 transactions. All four M4 marts completed and reconciled:

| Product | Mart rows | Grain |
| --- | ---: | --- |
| transaction_daily | 1,377 | UTC event date, currency |
| customer_daily | 4,235 | UTC event date, customer, currency |
| account_daily | 4,254 | UTC event date, account, currency |
| operations_daily | 4,703 | UTC event date, currency, type, channel, origin/destination countries |

Each mart independently reproduces these source transaction counts and minor-unit totals:

| Currency | Transactions | Minor-unit total |
| --- | ---: | ---: |
| EUR | 1,300 | 57,234,727 |
| GBP | 1,092 | 50,184,152 |
| JPY | 1,024 | 429,539 |
| USD | 1,584 | 68,289,025 |

No cross-currency monetary sum is defined. First/last event timestamps and active entity counts were independently recomputed for every group. Distinct entity metrics are not additive across groups. These are observed synthetic transaction amounts and activity, not balances, revenues or settlement outcomes.

Reusing the run ID verified existing outputs and returned the original run. A separate run produced identical hashes for all **10 payload artifacts**, including the SQLite database. An independent stdlib verification script checked all output hashes, grouped SQL counts/totals against curated input, all group metrics, and **20,000** transaction-to-mart lineage contributions (four per transaction). All **43** inherited sample hashes matched the baseline.

A deliberately damaged copy of the M3 run in a disposable temporary directory was submitted through the real analytics CLI. It exited **1** with a source artifact hash mismatch; no mart was published for that source. The exact command and result are saved in `outputs/runs/m4-verification/verification.json` and `malformed_cli.log`.

## Compatibility, capability status and risks

M1 evaluation executed unchanged on the fresh M2 evaluation export: ROC AUC **0.4872883**, F1 **0.1520468**, precision **0.0827147**, recall **0.9397590**, average precision **0.0837019**, validation-selected threshold **0.2**, confusion matrix `[[52, 865], [5, 78]]`. No model or tuning changed. M2 generation, M3 ingestion and legacy checks passed; historical sample paths have no tracked diff.

SQLite marts, validation, lineage and reconciliation are **implemented and locally executed**. BigQuery field schemas and GoogleSQL are **proposed and untested in BigQuery**. No cloud API, connector, provisioning, external service or additional dependency was used. No deployment is evidenced.

Remaining risks: synthetic inputs; caller-declared source timestamps; complete-snapshot semantics; no cross-batch/concurrent publishing protocol; in-memory replay costs; signed 64-bit SQLite aggregate limits; no verified cross-version byte reproducibility; manifests are integrity records without source authentication; output SQLite databases remain mutable; real sensitive identifiers would require retention/access controls. Arbitrary downstream SQL must preserve currency grouping. AML, graph, NLP and real-world fraud validity remain outside this milestone.

Run evidence is ignored under `outputs/runs/m4-source/`, `m4-foundation/`, `m4-analytics/`, `m4-analytics-repeat/`, `m4-m1-evaluation/` and `m4-verification/`. No commit or push was made. Work stops after M4.
