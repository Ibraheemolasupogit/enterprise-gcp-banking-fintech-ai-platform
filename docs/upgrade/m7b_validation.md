# M7B immutable synthetic ingestion packet

Date 2026-11-12. Baseline `main` at `ec7a86d`, with the uncommitted [M7A assessment](m7a_assessment.md). Project `ebank-pilot-dev-261031-k7m4`, region `europe-west2`.

**Offline implementation only.** M7B did none of the following:
- call gcloud, `bq` or any cloud API;
- run Terraform, other than `fmt -check`;
- change IAM (temporary access stays revoked);
- touch custody files, credentials or state;
- download packages;
- commit or push.

M7C loader permissions and M7D cloud operations are **not** implemented.

## What was built

| File | Change |
| --- | --- |
| `src/financial_crime_ml/deployment/ingestion_packet.py` | New. `build_packet`, `verify_packet`, `compose_manifest`, `analyse_tables`, `load_schemas`, `load_controls`, `canonical_json`, plus the two CLI entry points |
| `src/financial_crime_ml/cli.py` | Two subcommands, `build-ingestion-packet` and `verify-ingestion-packet`, dispatched like the existing M2–M4 commands. Nothing else changed |
| `configs/synthetic_v2_m7_pilot.yaml` | New. Identical to `synthetic_v2.yaml` except `transactions: 4000` (G8). **The default config is unchanged** |
| `tests/test_ingestion_packet.py` | New. 39 test functions, **103 test cases** |
| `Makefile` | `gcp-offline-check` now includes the packet tests |
| `docs/upgrade/m7a_assessment.md`, `docs/index.md` | Roadmap status and an index link |

No existing interface, dataset, schema, sample or test changed.

## Packet contract

### Input and acceptance

- **One M3 foundation run**, checked first by the existing `analytics.workflow.verified_source`. That function re-hashes every artifact, re-derives validated and curated rows from the raw inputs, and re-checks M3 reconciliation.
- **The run must be `completed`.** A `completed_with_rejects` run is refused: quarantined rows need a separate review.
- **Exactly three artifacts** are captured, byte for byte: `curated/transactions.csv`, `validated/customers.csv` and `validated/accounts.csv`. Each is re-hashed after verification and must still match the M3 manifest, which closes the window between check and use.
- **Cross-check against M3:** the packet's row counts and per-currency minor-unit totals must equal M3's `reconciliation.json`.

### Content validation

`analyse_tables` runs at build time and again on every verify.

- **CSV format:** strict UTF-8, no byte-order mark, LF line endings only, a final newline, no quoted newlines (BigQuery's `allowQuotedNewlines` is false), equal row widths, at least one data row.
- **Schema:** the header must equal `source_tables.json` **exactly, in order**, and that file must itself equal the M3 contract (`ENTITY_SCHEMAS` + `LINEAGE_FIELDS`, `CURATED_FIELDS`), with all fields REQUIRED and typed STRING, INT64 or TIMESTAMP. The error names any missing column, unknown column or reordering.
- **Forbidden fields:** M2 outcome and label fields (`is_suspicious`, `episode_id`, `scenario_type`), plus any name containing `label`, `target`, `fraud`, `suspicious`, `outcome`, `scenario` or `episode`. These are rejected in both the data header and the schema file.
- **Values:** M3's own row validator (`_schema_reasons`) checks ID formats, enums, `YYYY-MM-DDTHH:MM:SSZ` timestamps, and minor units from 1 up to the 1e9 cap. It also checks **exact currency precision**: `amount` must equal `format_amount(amount_minor, currency)`, for example GBP `12.34` and JPY `1234`. On top of that: `customer_country` must be a valid country, every REQUIRED value must be non-empty, and INT64 values must be positive decimal integers.
- **Lineage:** each table has one `source_id`, `source_time` and `ingestion_time`, all equal to the snapshot. Its `source_sha256` must equal the raw M3 input hash, and its `source_row` values must strictly increase. No event may be later than the source snapshot.
- **Relationships across the three tables:**
  - unique primary keys;
  - account → customer; transaction → customer and account;
  - the transaction's customer must own the account;
  - the transaction currency must match the account's base currency;
  - `customer_country` must match the customer;
  - events must not precede their parent (account opened after onboarding, transaction after account opened).

### Limits (from `infrastructure/pilot_controls.json`)

- transactions ≤ `max_transactions` (5,000);
- total bytes ≤ `max_total_upload_bytes` (10 MiB) and ≤ `max_stored_bytes_all_versions`;
- 3 load jobs ≤ `max_jobs_per_approved_execution` (20);
- `pilot_hours` must be a positive integer;
- `budget_is_spending_cap` must be `false`.

The packet records `query_bytes_billed: 0`: load jobs are not billed as queries. Tightening a control makes an existing packet fail verification.

### Identity, names and job IDs

- `identity` is the canonical JSON of the following, and nothing else:
  - classification `synthetic`;
  - the target (project, dataset, location);
  - the source snapshot (`source_id`, times, raw-input SHA-256s);
  - per-table SHA-256, MD5, size, rows and columns;
  - the SHA-256s of the schema and controls files;
  - the selected controls;
  - the strict load settings;
  - the packet format and version;
  - the M3 contract version.
- `identity_sha256` = SHA-256 of that canonical JSON. The packet ID is `m7p-` followed by its first **24 hex characters (96 bits)**.
- **Object names**, relative to the bucket: `m7/ingestion/<packet_id>/<table>.csv`, uploaded with `ifGenerationMatch=0`. New bytes produce a new ID and therefore new names, so nothing is ever overwritten.
- **Job IDs:** `m7_load_<24 hex>_<table>`. An identical retry reuses the ID, which BigQuery rejects as a duplicate; that is the replay guard. `WRITE_EMPTY` is the second guard.
- **The bucket name is not in the packet.** M6 deliberately never prints it. The manifest records `"bucket": "bound_at_execution_from_custody_inputs"` and a `sourceUriTemplate` of `gs://{bucket}/<object>`. M7C binds the bucket from the custody inputs.
- **Target validation:** the project ID must be lowercase, 6–30 characters; the dataset ID uses `[A-Za-z_][A-Za-z0-9_]*`; the location must be `europe-west2`.

### Load specification (one per table)

The configuration follows the BigQuery `jobs.insert` `configuration.load` shape:
- `sourceFormat: CSV`, `skipLeadingRows: 1`, `autodetect: false`, `maxBadRecords: 0`;
- `createDisposition: CREATE_NEVER`, `writeDisposition: WRITE_EMPTY`, `schemaUpdateOptions: []`;
- `encoding: UTF-8`, `fieldDelimiter: ","`, `quote: "\""`;
- `allowJaggedRows: false`, `allowQuotedNewlines: false`, `ignoreUnknownValues: false`;
- the explicit `schema.fields` from `source_tables.json`;
- `destinationTable` set to {project, dataset, table};
- the job `location` is the target location.

### Manifest and integrity

- **`manifest.json`** is canonical: sorted keys, no whitespace, ASCII, a final newline. It contains: `packet_id`, `identity`, `identity_sha256`, `tables`, `objects`, `load_jobs`, `reconciliation` (rows per table; per-currency rows, minor units and exponent), `limits` and `checksum_note`.
- **`manifest.sha256`** uses `shasum -a 256 -c` format. The command reports `manifest.json: OK` on the sample packet. **Approval pins the manifest SHA-256**, and `verify --expected-manifest-sha256` enforces it.
- **No machine-dependent data:** no timestamps, absolute paths, usernames, git state, or M3 run ID or manifest hash.
  - The M3 manifest hashes timestamps and absolute paths, so including it would have made the packet hash differ per run. (The first implementation included it; the reproduction check below caught this, and it was removed.)
  - `build` prints the M3 run ID and manifest hash as a **local record only**. The data and raw-input SHA-256s still pin the lineage exactly.
- **SHA-256 versus MD5:** SHA-256 is the integrity anchor. MD5 (hex and the base64 form GCS reports as `md5Hash`) is recorded only for comparison with GCS after upload, and `checksum_note` says the two are not interchangeable. CRC32C is not computed; no approved library is available.

### Build safety

1. The output root may not be under `data`, `outputs/sample`, `reports/sample`, `infrastructure`, `src` or `docs` in the repository, nor inside the source run. The default is `outputs/packets/` (ignored by git).
2. The packet is written into a new private `.staging-<id>-<random>` directory with create-exclusive writes, **verified in full**, then renamed into place. On any failure the staging directory is deleted, so no partial packet carries the final name.
3. If the final directory exists:
   - a valid, identical packet is **reused without writing**;
   - a valid packet with a different manifest raises an **identity collision**;
   - an invalid or partial one fails verification and is **never overwritten**.

### Verification (`verify_packet`)

Verification independently re-derives everything from the packet's own bytes:
1. **Inventory:**
   - the packet directory must not be a symbolic link;
   - exactly `manifest.json`, `manifest.sha256` and `data/<3 tables>.csv`, with no other file or directory and no symbolic links.
2. **Manifest integrity:**
   - the sidecar equals the manifest's SHA-256;
   - the hash equals the pinned value if one is given;
   - the manifest is valid JSON in canonical form.
3. **Identity:** the packet ID format is valid and equals the directory name.
4. **Structure:**
   - the target is valid;
   - the load jobs cover exactly the three tables, with unique, well-formed IDs;
   - object names are unique;
   - every strict setting holds, and the destination and source object are correct.
5. **Full recomputation:** `compose_manifest` runs again from the packet bytes with the **current** schema and controls files. Every table entry must match, then the whole manifest. Altered bytes, totals, identity, names, job IDs or settings are all rejected.

## Sample packet (controlled 4,000-transaction snapshot)

**Reproduction recipe**, all local:
1. Generate with `configs/synthetic_v2_m7_pilot.yaml` (seed 42).
2. Run M3 `ingest` with `--source-id m7b-pilot-4000 --source-time 2026-01-01T00:00:00Z --ingestion-time 2026-01-01T00:00:00Z`.
3. Run `build-ingestion-packet --project-id ebank-pilot-dev-261031-k7m4 --dataset-id bankpilot_dev`.

The 4,000-row snapshot generated, ingested (M3 `completed`, 0 rejects) and verified. **Reducing the count broke no relationship, reconciliation or contract.**

| Table | Rows | Bytes | SHA-256 |
| --- | --- | --- | --- |
| `customers` | 100 | 16,187 | `3c1927b521d1e45997e2633a2dd16eeb6836a2860f5f6d4d3a4a6f010fd9de66` |
| `accounts` | 150 | 26,251 | `c0ed9882b2fb8bcff3077454a8aba54c61603ccde90385814867aae130871991` |
| `curated_transactions` | 4,000 | 1,054,859 | `5e142c1ddb207a7f2d5af47d571a4b865ee0604bee10b888d92033f94b582e5c` |
| **Total** | | **1,097,297** (10.5% of 10 MiB) | |

| Currency | Rows | Minor units | Exponent |
| --- | --- | --- | --- |
| EUR | 1,061 | 47,972,939 | 2 |
| GBP | 874 | 41,044,581 | 2 |
| JPY | 813 | 331,566 | 0 |
| USD | 1,252 | 54,122,799 | 2 |

- **Packet ID:** `m7p-a89332976c69d583137f9323`
- **Identity SHA-256:** `a89332976c69d583137f9323b3658c3951ca5d8110f98f8e868d0976a21f1325`
- **Manifest SHA-256:** `7e2fb8b7500a4aaf0652d2f1381359d333011a5f2ff5f92b1b684be0142b540f`
- **Reproducibility:** two **independent** generate → ingest → build runs, in separate directories with different M3 run IDs, produced byte-identical packets (`diff -r` clean) and the same manifest SHA-256. `shasum -a 256 -c manifest.sha256` reports OK.
- **Job IDs:** `m7_load_a89332976c69d583137f9323_{customers,accounts,curated_transactions}`.

The sample packet lives only in the session's scratch directory, not in the repository. This hash is **not an approval**: M7D must rebuild and pin a packet built from the commit it runs on.

## Tests

`tests/test_ingestion_packet.py`: 39 functions, 103 cases. They run on a 100-transaction M2 → M3 fixture built in a temporary directory.

| Requirement | Cases |
| --- | --- |
| **Deterministic output** | Independent builds are byte-identical. Independent M3 runs of one snapshot give the same packet ID and manifest hash. Rebuilding into the same root reuses the packet without writing (file times unchanged) |
| **Contents** | Source bytes and column order preserved; SHA-256 equals M3's; per-currency totals equal M3's; JPY exponent 0; MD5 recomputed and kept separate from SHA-256; no machine metadata or M3 provenance in the manifest |
| **Byte tampering** | A valid-looking enum change in a data file is rejected |
| **Manifest tampering** | 10 cases with a consistent sidecar: totals, a table's SHA-256 or MD5, limits, precondition, an extra key, an injected provenance path, a missing identity, the snapshot, the controls. Also: a stale sidecar, non-canonical JSON, invalid JSON, a wrong pinned hash |
| **Altered identity** | 7 cases: the packet ID (and a directory renamed to match a fake ID), a path-like ID, the identity hash, the classification, the project, the location, the version |
| **Columns** | A missing, an unknown and a reordered column; the schema file with a label column, reordered fields or a NULLABLE field |
| **Outcome or label leakage** | `is_suspicious`, `scenario_type`, `episode_id`, `fraud_score`, `label` |
| **Invalid foreign keys and lineage** | 10 parametrised cases (unknown customer or account, `source_id`, `source_sha256`, `ingestion_time`, `source_row` 0, non-UTC timestamp, bad country, currency mismatch, empty REQUIRED value); account ownership; reordered `source_row`; duplicate primary key |
| **Financial totals and precision** | GBP `12.34` accepted; `12.3` and `12.340` rejected; JPY `1234` accepted and `12.34` rejected; above the 1e9 cap rejected. The build is refused when M3 reconciliation disagrees on money, customer rows or curated rows |
| **CSV rules** | Byte-order mark, CRLF, no final newline, header only, invalid UTF-8, quoted newline |
| **Transaction, size and cost limits** | 7 cases: `max_transactions`, upload bytes, stored bytes, the job limit, a string or zero control, a spending-cap claim. A tightened control invalidates an existing packet |
| **Targets** | Wrong location (2), invalid project, invalid dataset |
| **Unsafe paths** | 6 protected roots, with the repository root sandboxed; the guard is anchored to the real repository; an output root inside the source run; a symbolic-link packet directory or data file; extra files and directories; missing files |
| **Identity collisions and overwrites** | A collision is refused; a corrupt existing packet is never overwritten; a leftover partial directory is rejected and not completed |
| **Partial writes** | A failure while writing the manifest leaves neither a packet nor a staging directory |
| **Duplicate job IDs and invalid load settings** | 16 cases: duplicate, malformed or missing job, `WRITE_TRUNCATE`, `CREATE_IF_NEEDED`, autodetect, `maxBadRecords` 1, `skipLeadingRows` 0, schema update options, JSON format, wrong destination, wrong source object, wrong location, a dropped schema field, a wildcard URI, a malformed configuration. Also duplicate object names |
| **Source acceptance** | A run with rejects is refused; a tampered M3 artifact is refused |
| **CLI** | Build and verify through `financial-crime-ml`; the local source record is printed; a wrong pinned hash and a wrong location exit 1 |
| **Pilot config** | 4,000 transactions; otherwise identical to the default; the default is still 5,000 |

**Mutation check:** 28 mutations in `ingestion_packet.py`, **all detected**. The file was restored byte-identical (SHA-256 compared).
- **Content checks disabled:** forbidden fields, header order, foreign keys, ownership, lineage SHA-256, `source_row` order.
- **Limits disabled:** transactions, size, jobs.
- **Load jobs:** duplicate job IDs accepted, load settings unchecked, `WRITE_EMPTY` changed to `WRITE_APPEND`.
- **Manifest checks disabled:** sidecar, canonical form, directory name, full recomputation.
- **File and path safety:** extra files allowed, symbolic links allowed, staging left behind, collision ignored, protected roots disabled.
- **Source checks:** rejected runs accepted, the M3 money and row cross-checks disabled.
- **Format and leakage:** MD5 replaced by truncated SHA-256, quoted newlines allowed, a byte-order mark accepted, provenance leaked into the manifest.

"Ownership" and the "M3 money cross-check" survived the first pass, and two tests were added for them.

**Incident during the mutation run:** with the "protected roots disabled" mutation active, the old protected-root test really built a 100-row fixture packet in `data/sample/packets/`. It was found by `git status` immediately afterwards, confirmed to be only that fixture packet (`source_id` `m7b-fixture`), and removed. The inherited sample artifacts are hash-checked by `conftest.py`, and none changed. The test now replaces `REPO_ROOT` with a temporary directory, so a broken guard cannot write into the checkout. A separate test asserts that the real guard uses the real repository root.

## Validation

| Check | Result |
| --- | --- |
| Full `pytest` | **1273 passed, 1 skipped** (previously 1170 + 103 new) |
| `make gcp-offline-check` | **1029 passed, 1 skipped** (926 + 103); `terraform fmt` passed |
| `ruff check .` | Passed |
| `terraform fmt -check -recursive` | Passed |
| Docs validator; `final_project_check.py` | 0 issues; 79 checks, 0 issues |
| `git diff --check` (including new files) | Passed |

## Trade-offs

| Decision | Alternative | Reason |
| --- | --- | --- |
| Validate locally, freeze bytes, then upload | Validate in GCP (Dataflow, SQL checks) | M3 validation is already tested; BigQuery is a schema-strict, atomic sink; no new service or cost |
| Content-addressed identity (data, schema, controls, target, settings) | Random or time-based packet IDs | Same inputs give the same IDs, so a re-upload or re-load is detected by GCS and BigQuery rather than duplicated |
| 96-bit ID prefix in names | Full 256-bit hash | Readable names and job IDs. 96 bits is collision-resistant at this scale, the full `identity_sha256` is recorded, and a collision would be refused at build |
| Bucket bound at execution | Bucket in the packet | Keeps M6's rule that the bucket name is never printed or committed |
| M3 run provenance as a local record only | M3 manifest hash in the packet | That manifest hashes timestamps and paths, so packet hashes would be machine-dependent. The data and raw SHA-256s pin lineage |
| MD5 alongside SHA-256, no CRC32C | Add `google-crc32c` | No new dependency (downloads are not authorised). GCS reports MD5 for single-part uploads |
| Packet directory with verify-before-rename | Archive file (tar or zip) | Directly inspectable; the operator's `shasum -c` works; atomic publication within one filesystem |
| Recompute-and-compare verifier | Check the listed hashes only | Catches any manifest field edit, including load settings, without a separate rule for each field |

## Limitations

1. **Integrity, not authenticity.** Anyone who can edit the packet can also rewrite a consistent manifest and sidecar. Protection comes from **pinning the approved manifest SHA-256 outside the packet** (the approval record) and verifying with `--expected-manifest-sha256`. There is no signature.
2. **Verification depends on the current schema and controls files.** That is intended (drift is rejected), but a packet becomes invalid after any edit to `pilot_controls.json`, even an unrelated one, because the file's SHA-256 is part of the identity.
3. **Only completed runs are accepted.** A snapshot with quarantined rows needs a future, reviewed path.
4. **Relationships are checked only among the three loaded tables.** Beneficiary and device references are verified by `verified_source` at build time but are not loaded or re-checked at verify.
5. **Rename atomicity:** a non-empty existing directory is never replaced, but an *empty* directory created concurrently at the final path could be replaced by the rename. This is local-only and there is no concurrent builder.
6. **The 4,000-row pilot config adds a file to `configs/`,** which changes the code fingerprint recorded in new M2–M4 run manifests. Existing local run directories reused by run ID would report a signature change. Historical committed samples are unaffected.
7. The sample packet hash depends on the M3 `source_id` and times chosen at ingestion. The recipe above must be followed exactly to reproduce it.

## Remaining GCP dependencies (M7C and M7D)

Unchanged from [M7A](m7a_assessment.md):
- **G1:** the loader identity and expiring grants;
- **G3:** a command bundle that binds the custody bucket name;
- **G4:** verifiers for the captured upload and job JSON;
- **G5:** a `tabledata.list` → CSV converter and inputs-only reconciliation (moved from M7B to M7C);
- **G6:** a pilot checker that tolerates data-metadata drift;
- **G7:** a Gate C rewrite.

**Live verification still required:**
- BigQuery CSV acceptance of `YYYY-MM-DDTHH:MM:SSZ` TIMESTAMPs (load jobs cannot be dry-run);
- the exact load permissions;
- IAM Conditions support.

**Readiness for M7C:** the packet format is stable and tested, and it gives M7C everything it consumes: object names, job IDs, load specifications, MD5 for upload checks, row counts and per-currency totals for reconciliation. The approval inputs, loader decision C and the executor choice are still pending.

**Stopped after M7B offline implementation.**
