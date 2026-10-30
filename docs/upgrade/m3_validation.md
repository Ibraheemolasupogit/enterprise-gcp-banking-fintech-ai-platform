# M3 executed local validation

## Implementation and provenance

M3 adds foundation contract `1.0.0` using the five M2 `2.0.0` observation schemas: customer, account, beneficiary, device and transaction. It preserves the inherited interfaces and observation/outcome separation. Design, semantics, quarantine policy, CLI usage, limitations and future BigQuery mapping are in [the foundation contract](../financial_data_foundation.md).

Starting revision: `7857f2c054f8f4c526c0e759eec050b15820ebb8` (`feat: add deterministic synthetic financial data contracts`). No M3 commit or push was made. Actual run manifests fingerprint the dirty working source, including untracked modules. Source fingerprint for the executed runs: `ad080753609563fde27adaa4127ecb7f085b9a98d37eb1a4d4bd4b673899c3f4`.

Changed files: `src/financial_crime_ml/cli.py`; four new files in `src/financial_crime_ml/ingestion/foundation/` (`__init__.py`, `contracts.py`, `pipeline.py`, `cli.py`); `tests/test_financial_foundation.py`; `docs/financial_data_foundation.md`; this record; `README.md`; and `docs/index.md`. There were no dependency or inherited sample changes.

## Checks actually run

```text
python3 -m ruff check .
python3 -m pytest -q
python3 -m financial_crime_ml.cli generate-data-v2 --run-id m3-source
python3 -m financial_crime_ml.cli ingest-financial-data --dataset-dir outputs/runs/m3-source --source-id m3-synthetic-v2 --source-time 2026-01-01T00:00:00Z --ingestion-time 2026-01-02T00:00:00Z --run-id m3-ingest
# The exact ingestion command above was then repeated unchanged.
python3 -m financial_crime_ml.cli ingest-financial-data --dataset-dir outputs/runs/m3-source --source-id m3-synthetic-v2 --source-time 2026-01-01T00:00:00Z --ingestion-time 2026-01-02T00:00:00Z --run-id m3-ingest-repeat
python3 -m financial_crime_ml.cli evaluate-fraud --input outputs/runs/m3-source/evaluation_transactions.csv --run-id m3-m1-evaluation
python3 -m financial_crime_ml.cli final-check
python3 -m financial_crime_ml.cli validate-docs
```

Ruff: **All checks passed**. Full pytest: **219 passed in 22.25s**, no warnings (191 inherited/M1/M2 cases plus 28 M3 cases). The legacy final check passed 79 checks, zero issues. Documentation validation passed 6 configured documents and 5 configured diagrams, zero issues; this validator does not automatically discover every new Markdown document.

New tests cover duplicate-key rejection without an arbitrary survivor, parent-rejection propagation, malformed headers/record widths/encoding, strict timestamps and entity chronology, invalid references and ownership, exact amount precision including JPY, currency mismatch without silent conversion, row and value reconciliation, unaccountable values, SQL execution and grouping, per-row lineage, repeat-run file immutability, collision and tamper detection, protected sample paths, CLI dispatch, and M1 evaluation of a fresh M2 export. The session fixture verifies all 43 sample hashes before and after the full suite.

## Actual data and repeatability results

The fresh default M2 generation passed its own validation. M3 accepted 100 customers, 150 accounts, 300 beneficiaries, 200 devices and 5,000 transactions, with zero rejects and zero unaccountable monetary rows. The curated product contains 5,000 rows. Repeating the exact command returned `Reused: True`; the tests additionally verified that reuse preserves file bytes and modification times. A separate run ID produced identical hashes for all **19 payload artifacts**. Manifests retain distinct execution times and IDs.

Independent stdlib verification loaded the generated DDL and curated CSV into in-memory SQLite. `GROUP BY currency` sums matched source, validated and curated values exactly:

| Currency | Minor-unit total |
| --- | ---: |
| EUR | 57,234,727 |
| GBP | 50,184,152 |
| JPY | 429,539 |
| USD | 68,289,025 |

These values are separate currency totals and must not be summed into a monetary grand total. Independent checks also verified all 5,000 curated source-row links, customer-country joins, raw byte copies, input hashes and all output hashes. All 43 inherited sample hashes were unchanged.

A deliberately damaged source copy was created in a disposable temporary directory and ingested through the real CLI as `m3-quarantine`. It introduced a duplicated transaction ID (two rejected rows), a noncanonical decimal amount, an invalid minor-unit value, an event after snapshot time and an invalid account reference. The batch returned `completed_with_rejects`, exit 0, with exactly **4,994 curated + 6 quarantined = 5,000 source rows**. The quarantine recorded `duplicate.primary_key`, `money.precision`, `schema.amount_minor`, `time.after_source_snapshot` and reference/ownership reason codes. One monetary row was unaccountable and explicitly reported. All other rejected values reconciled per currency; no value was fabricated for the unaccountable row. The raw damaged bytes were retained in that run. Exact CLI invocation, independent results and quarantine reason lists are saved in `outputs/runs/m3-verification/verification.json` and `quarantine_cli.log`.

## Compatibility and residual risks

M1 evaluated the fresh M2 export successfully: test ROC AUC **0.4872883**, F1 **0.1520468**, precision **0.0827147**, recall **0.9397590**, average precision **0.0837019**, validation-selected threshold **0.2**, confusion matrix `[[52, 865], [5, 78]]`. These remain weak synthetic baseline results; no new model, tuning or improvement is claimed. The curated observation product intentionally lacks the target required by M1; use the M2 evaluation export for that interface.

All capabilities evidenced here are **implemented and locally executed**. The BigQuery mapping is **documented design only**. There was no GCP deployment, connector, cloud command, external service or added dependency. The source remains simulated M2 data, and genuine local execution does not establish real-world fraud validity. Snapshot/receipt times are caller assertions, not authenticated source clocks. The engine is in-memory and handles independent snapshots without CDC, cross-batch deduplication or concurrent publishing guarantees. Raw and quarantine copies need a production retention/PII/access policy before real data use. Currency discipline is enforced by ingestion reconciliation; arbitrary downstream SQL still requires correct grouping. AML, graph, NLP and synthetic-data limitations remain unresolved.

Evidence directories are ignored local outputs: `m3-source`, `m3-ingest`, `m3-ingest-repeat`, `m3-quarantine`, `m3-m1-evaluation`, and `m3-verification` under `outputs/runs/`. Tests use temporary directories. No inherited demo was run in the working repository and no committed sample was overwritten.
