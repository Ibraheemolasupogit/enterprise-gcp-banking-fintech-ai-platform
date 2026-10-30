# Financial analytics v1

## Implemented scope and grains

The analytics layer builds four SQL marts from one completed M3 foundation snapshot. It runs locally with Python's standard-library SQLite. The input is M3's accepted curated observation product, enriched through verified validated customer/account joins. The existing M1 evaluation export, M2 generator, M3 ingestion and historical demonstrations retain their own interfaces. The analytics contract is `1.0.0`.

| Mart | Unique analytical grain | Additional dimensions / metrics |
| --- | --- | --- |
| transaction_daily | UTC event date, currency | active customer and account counts |
| customer_daily | UTC event date, customer_id, currency | customer country, active account count |
| account_daily | UTC event date, account_id, currency | owning customer_id |
| operations_daily | UTC event date, currency, transaction_type, channel, origin_country, destination_country | observed transaction volume by supported operational dimensions |

Every mart also contains `transaction_count`, `total_amount_minor`, `first_event_time`, and `last_event_time`. Counts mean accepted observations, not successful settlements or distinct payment orders. Minor-unit totals sum positive observed transaction amounts within one currency. Customer and account marts contain active entities only: no zero-activity rows are manufactured. Entity keys are the unchanged M2/M3 IDs. Country and ownership joins are many-to-one and are rechecked against M3 raw lineage before SQL execution. There are no new business entities.

`event_date` is the UTC date of `transaction_timestamp`, not source snapshot date or ingestion receipt date. First/last event timestamps are min/max within the grain. M3's declared source/ingestion semantics are preserved in the embedded source manifest and input database; actual analytics execution times are separate manifest fields. Null source values, unexpected columns, malformed rows and lineage mismatches fail source validation. Required mart fields are checked for SQL NULL, and unique indexes enforce each grain. Empty valid snapshots produce header-only mart exports.

Transaction counts and monetary sums are additive across disjoint grains within one mart. Distinct active entity counts are **not additive** across dates, currencies or operational groups. Never add totals from different marts, since they summarize the same transactions. Currency must remain in monetary grouping keys; no cross-currency financial total, FX conversion, fee, revenue, balance, settlement status, failure rate or processing latency is defined by these inputs. Arbitrary downstream SQLite queries can violate currency discipline, so consumers remain responsible for their own SQL.

## Build and lineage

```bash
python3 -m financial_crime_ml.cli build-financial-marts \
  --source-run outputs/runs/m3-ingest \
  --run-id financial-marts-example
```

Use a fresh run ID for changed input, metadata, SQL or implementation. The command reads each source artifact once, verifies its M3 manifest hash and contract, reruns M3 raw validation, and compares all validated entities and curated rows to rederived observations and lineage. It also recomputes M3 reconciliation. Only completed M3 v1 snapshots are accepted. A source marked `completed_with_rejects` is supported; marts describe only its accepted rows, and analytics records that source status. Analytics is not a new quarantine stage. Missing files, bad metadata or malformed source data fail before an analytics run starts. SQL/build failures retain a failed manifest and any partial artifacts; consumers must require `status=completed`.

Outputs are isolated under ignored `outputs/runs/<run-id>/`:

- `analytics.sqlite`: verified curated transactions, customer/account dimension inputs, four SQL marts and grain indexes.
- Four deterministic, grain-sorted CSV exports.
- `reconciliation.json`: mart row counts and transaction-count/minor-unit totals by currency. Each mart must equal the same curated input totals; failure blocks successful completion.
- `lineage.jsonl`: one contribution per source transaction per mart, recording grain key, transaction ID, M3 source record ordinal and raw file hash. Non-additive distinct counts still require the complete group to recompute.
- Exact executed `marts.sqlite.sql`, proposed `bigquery_schemas.json`, embedded `source_manifest.json`, and execution `manifest.json`.

The analytics manifest binds the source manifest SHA256 (which binds all M3 files), SQLite SQL hash, source-code fingerprint, revision/dirty state, dependency versions, SQLite version, actual execution timestamps and every artifact hash. The SQL is read from `sql/analytics/v1/marts.sqlite.sql` and is fingerprinted separately from Python sources. Same run ID/signature returns the original run after verifying artifact hashes, without rewriting files. Same inputs and code with a different run ID produce identical payload bytes in the tested environment; run IDs and actual timestamps differ in the manifest. There is no concurrent publishing protocol. Hashes detect accidental changes; they do not authenticate a source whose manifest and content are both maliciously replaced.

## BigQuery targets: proposed, not executed

Versioned target schemas are in `sql/analytics/v1/bigquery.schemas.json`; each table includes explicit required field definitions and its grain. The JSON wrapper carries version/status/table metadata; pass only a selected table's `fields` list to a future BigQuery schema operation. No cloud operation is implemented here.

`sql/analytics/v1/marts.bigquery.proposed.sql` is an **untested GoogleSQL translation**. It replaces SQLite's `substr(timestamp, 1, 10)` with `DATE(timestamp, 'UTC')`, assuming typed TIMESTAMP inputs, and uses placeholder fully qualified table names. Local execution covers only `marts.sqlite.sql`.

A future migration must:

1. Load verified M3 inputs using the documented source mapping: keys/categories/currency/countries as STRING; minor units as INT64; event/source/ingestion timestamps as TIMESTAMP; canonical decimal amount as STRING. Keep source lineage columns and customer/account dimension keys.
2. Create approved BigQuery target tables using the selected versioned field schemas: event date DATE; entity/dimension fields STRING; counts and minor-unit totals INT64; first/last event times TIMESTAMP. Define date partitioning and optional customer/account clustering only after workload review.
3. Replace `PROJECT.DATASET` placeholders in the proposed SQL. Its CTAS form is a translation prototype; use explicit target DDL plus INSERT for REQUIRED field enforcement. BigQuery key declarations alone would not enforce unique grains.
4. Test null/duplicate/join checks, timestamp semantics, numeric limits, source-to-mart reconciliation and currency grouping in BigQuery before claiming compatibility. Retain manifest-level lineage and per-transaction contributions in appropriate tables if deploying.

No BigQuery SQL was dry-run, parsed by a BigQuery client or executed. No connector, GCP resource, API call, provisioning, billing or deployment evidence exists for M4.

## Limits and capability status

The four marts and local reconciliation are implemented and locally tested. BigQuery schemas and migration SQL are proposed artifacts. The source remains synthetic M2 data in an M3 snapshot; this is not evidence of real payment operations. Processing loads the source in memory, SQLite totals use signed 64-bit integers, and oversized sums fail rather than convert to floating point. Repeatability across SQLite/library/platform versions was not tested. SQLite CTAS tables rely on explicit build-time null checks and grain indexes, rather than a production database policy. Consumers can modify output databases after the build, which invalidates their recorded hashes. Full source replay adds work per build. Entity IDs and lineage in output files require retention and access controls before real sensitive data is introduced. No dependencies or cloud costs were added.

Executed results: [M4 validation](upgrade/m4_validation.md).
