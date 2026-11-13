# M7A synthetic data ingestion readiness and gap assessment

Date 2026-11-12. Baseline `main` at `ec7a86d` (M6T closed). Project `ebank-pilot-dev-261031-k7m4`, region `europe-west2`.

**Assessment and documentation only.** M7A did none of the following:
- call gcloud, `bq` or any cloud API;
- run any Terraform command;
- change IAM;
- read custody files, state, plans or backups;
- commit or push.

The only code run was the existing local M2 → M3 → M4 workflow, writing to a scratch directory outside the repository, to measure real file sizes. No repository file, dataset or sample changed apart from this document and its index link.

**Scope of M7:** load three synthetic input tables (`curated_transactions`, `customers`, `accounts`) from the pilot bucket into the existing, empty BigQuery tables, then verify them against M3. **Building the four marts in BigQuery (Gate C steps 4–5) is out of scope** and is proposed for M8.

## 1. Current state

- **Infrastructure:** 18 pilot resources are deployed: the bucket and its policy, the dataset and its policy, 7 empty tables and 7 runner table grants. The IAM bootstrap is applied.
- **Temporary access:** the provisioner role and the operator impersonation grant are revoked.
- **State backups:** pilot serial 21 and bootstrap serial 26.
- **Data:** none in GCP.

**Local measurement** (default `configs/synthetic_v2.yaml`, seed 42; M3 `completed`, no rejects; M4 `completed`):

| File loaded | Rows | Bytes |
| --- | --- | --- |
| `curated/transactions.csv` | 5,000 | 1,303,681 |
| `validated/customers.csv` | 100 | 15,887 |
| `validated/accounts.csv` | 150 | 25,801 |
| **Total** | | **1,345,369 (≈ 13% of the 10 MiB limit)** |

**Per-currency minor units** (curated equals source): EUR 57,234,727; GBP 50,184,152; JPY 429,539; USD 68,289,025.

The default generator produces **exactly 5,000 transactions, the `max_transactions` limit**. There is no headroom, so a config change that adds rows would breach it.

## 2. Reusable components

| Component | Where | What M7 can reuse |
| --- | --- | --- |
| Synthetic generation | `data_generation/v2/workflow.py` (`run_generation`) | Seeded, contract-validated before and after serialisation, fresh run directory |
| Source contracts | `data_generation/v2/contracts.py`, `ingestion/foundation/contracts.py` | Closed schemas; `YYYY-MM-DDTHH:MM:SSZ` UTC; exact minor-unit money and currency exponents; lineage fields |
| Immutable snapshot ingestion | `ingestion/foundation/pipeline.py` (`ingest`) | Raw/validated/curated/quarantine; per-artifact SHA-256 in the manifest; signature-checked reruns; row and per-currency reconciliation |
| **Independent re-verification** | `analytics/workflow.py` (`verified_source`) | Re-hashes every artifact, re-derives validated and curated rows from raw, checks reconciliation. **The natural pre-upload gate** |
| Reference marts | `analytics/workflow.py` (`build_marts`) | SQLite marts, lineage and `bigquery_schemas.json` for later parity |
| BigQuery input schemas | `infrastructure/schemas/v1/source_tables.json` | Column names and order **match the CSV headers exactly** (checked: 8, 9 and 20 columns); all REQUIRED; money as INT64 minor units plus a STRING `amount` |
| Export comparator | `deployment/reconcile.py` | Lossless typed comparison of **seven** exported tables against M3/M4; accepts BigQuery UTC timestamp formats |
| Cost and volume limits | `infrastructure/pilot_controls.json` | 5,000 transactions, 10 MiB upload, 20 jobs per execution, 100 MiB per query, £5 alert budget (not a cap) |
| Access-window pattern | Bootstrap root, `bootstrap_plan_check.py`, M6S runbook | Expiring (≤ 8 h) conditional grants; plan checker per mode; token-minting audit; revoke and read-back; encrypted state backups |
| Pilot drift checker | `bootstrap_plan_check.py --mode pilot-recovery` | Bounded refresh-drift model; needs extending (gap G6) |
| Gate C procedure | `docs/gcp_pilot_plan.md` | Create-only names, `ifGenerationMatch=0`, strict CSV load options, `CREATE_NEVER`/`WRITE_EMPTY`, job ledger, `tabledata.list` acquisition |

## 3. Assessment by control

| Control | Existing | Gap |
| --- | --- | --- |
| **Source immutability** | M3 run directory with per-artifact SHA-256; `verified_source` re-derives everything | `manifest.json` itself is not hashed by anything. No frozen "ingestion packet" pins the exact bytes, M3 manifest hash and M4 reference run used for upload |
| **SHA-256 checksums** | SHA-256 for every M3 artifact | GCS reports **MD5 and CRC32C, not SHA-256**. The packet must record the MD5 (and CRC32C if a library is approved) to compare with object metadata. Custom SHA-256 object metadata would only be an assertion |
| **Schema compatibility** | Exact column match; explicit REQUIRED schemas | **Unverified:** BigQuery CSV parsing of `2025-01-01T00:00:00Z` (`T` separator plus `Z`) as TIMESTAMP. **Load jobs cannot be dry-run**, so the first real load is the test; the load is atomic, so a failure leaves the table empty |
| **Idempotent uploads** | Gate C requires `ifGenerationMatch=0`, create-only run-prefixed names | No object-name scheme, no upload command bundle, no post-upload verifier comparing generation, size and MD5 |
| **Load replay protection** | Gate C requires idempotent job IDs, `WRITE_EMPTY` | No deterministic job ID scheme. Proposed: `m7_<packet>_<table>_<sha256[:12]>`. BigQuery rejects a reused job ID in the project, and `WRITE_EMPTY` refuses a non-empty table: two independent guards |
| **Row counts** | M3 reconciliation; comparator row checks | No post-load acquisition tool (`tabledata.list` → canonical CSV). The comparator requires all **seven** tables, with no **inputs-only** (3-table) mode |
| **Financial reconciliation** | Per-currency minor units at every M3 stage; exact INT64 handling in the comparator | Same as above: needs the three-table mode, plus reconciliation against the packet totals |
| **IAM boundaries** | Runner: `objects` role on the bucket (create, get, list; **no delete**); `readers` on the 3 inputs; `tables` on the 4 marts; project `jobs`. Provisioner: no data or job permissions | **No identity can currently load the inputs** (G1). Gate C says the provisioner uploads and loads, but its M6K role has none of those permissions, and the bootstrap has no load grants |
| **Cost controls** | Job/byte limits; budget alerts | Load jobs and `tabledata.list` are not billed as queries; storage is about 1.3 MB × (GCS + BigQuery) plus versions. Expected cost is negligible. No ledger tool exists, and alerts are not caps |
| **Evidence hygiene** | Raw state and plans stay in custody | M3/M4 manifests contain the absolute local `source_directory` (which includes the local username) and `git_status_porcelain`. **Uploading manifests as-is would publish local paths** |
| **Terraform coexistence** | M6T bounded drift tolerance | After loading, a refresh will show new `num_rows`, `num_bytes` and similar values on the input tables. The current checker **fails closed** on those, so any later pilot plan would be rejected (G6) |

## 4. Implementation gaps (prioritised)

| ID | Gap | Blocking? |
| --- | --- | --- |
| **G1** | **Load identity and grants.** Nothing can write to the input tables or run a load job as an approved loader. Needs a design decision (§6.2), bootstrap Terraform, a plan-checker mode, and revoke handling | Blocks M7D |
| **G2** | **Ingestion packet builder and verifier.** Select a verified M3 run; freeze the three files with SHA-256, MD5, size, row counts and per-currency totals; object names; job IDs; load configurations; limit checks; a redacted manifest; a packet hash for approval | Blocks M7C and M7D |
| G3 | **Operator command bundle.** Exact upload, describe, load, job-show and read-back commands generated from the packet (Terminal only, as in M6S) | Blocks M7D |
| G4 | **Result verifiers.** Offline checks of operator-captured JSON: object generation, size, MD5, no extra objects under the prefix; load job state, configuration, output rows, zero bad records and job ID | Blocks M7D closure |
| G5 | **Acquisition and three-table reconciliation.** Convert `tabledata.list` pages to canonical CSV (exact epoch-to-UTC conversion); add an inputs-only mode to the comparator | Blocks M7D closure |
| **G6** | **Pilot checker data-metadata drift.** Bounded tolerance for row and byte counters on the 3 input tables only, with schema and IAM still identical | Blocks the next pilot plan after loading |
| G7 | **Runbook alignment.** Gate C steps 1–3 name the provisioner as uploader and loader, which contradicts the M6K role. Rewrite Gate C to match the chosen loader | Documentation |
| G8 | **Headroom.** Generate the pilot snapshot at < 5,000 transactions (for example 4,000), or approve exactly-at-limit explicitly | Approval decision |
| G9 | **Rollback for a wrong-but-successful load.** No identity may delete. Recovery needs a reviewed truncate (DML `DELETE` under `updateData` + `jobs.create`, one billed query) or table time travel | Documented decision |

## 5. Dependencies

1. **M7B → M7C:** the command bundle, verifiers and IAM checker take the packet format as input.
2. **External verification before M7D**, possible only in GCP, as read-only steps or in the window:
   - whether IAM Conditions on `resource.name` work for the chosen grants;
   - the exact permission set for a load into an existing table with `WRITE_EMPTY` (candidate: `bigquery.jobs.create`, `bigquery.tables.get`, `bigquery.tables.updateData`, `storage.objects.get`; check whether `bigquery.tables.update` is needed), via `testIamPermissions`;
   - the TIMESTAMP format accepted by CSV loads.
3. **Approvals before M7D:**
   - the loader design;
   - the packet SHA-256;
   - the bootstrap access plan SHA-256;
   - window length (≤ 8 h);
   - cost envelope and data classification (synthetic only).
4. **M6 boundaries stay intact:** no change to the authoritative bucket or dataset policies, the runner's roles, or the 18 pilot resources' configuration.

## 6. Architectural trade-offs

### 6.1 Direct BigQuery load jobs versus managed processing

| Option | For | Against | Verdict |
| --- | --- | --- | --- |
| **GCS → BigQuery batch load jobs** | No new API or service agent; atomic per job; strict explicit schema; `max_bad_records=0`; job ID replay protection; load slots not billed as queries; small IAM surface | No dry run; transformation must happen before upload (already done locally by M3) | **Recommended** |
| Local file → BigQuery load (no GCS) | Simplest; one less permission | No immutable landing copy and no object generation evidence; departs from Gate C | Rejected for the audit trail |
| BigQuery external tables over GCS | No copy | Mutable source at query time; per-query cost; weaker lineage | Rejected |
| BigQuery Data Transfer Service (GCS) | Managed and schedulable | New API and service agent with its own grants; recurring-run semantics; overkill for one snapshot | Defer |
| Dataflow / Dataproc / Data Fusion | Scalable, streaming-capable | New APIs, workers, networking, service agents with broad roles, real cost, bigger blast radius; nothing to transform | Rejected for the pilot |
| Cloud Run / Functions triggered on upload | Automation | New runtime identity, triggers and deployment pipeline; harder to bound within a window | Defer |
| Storage Write API / streaming | Low latency | Not a single atomic batch; weaker replay semantics; streaming cost | Rejected |

**Rationale:** at about 1.3 MB of already-validated data, managed processing adds identities, APIs and cost without adding control. Validation stays in M3 locally, where it is tested; BigQuery acts only as a schema-strict, atomic sink.

### 6.2 Load identity (decision needed for G1)

| Option | For | Against |
| --- | --- | --- |
| A. Widen the runner (`updateData` on the inputs) | Reuses existing bucket and jobs grants | Breaks the runner's read-only input boundary **permanently**. Rejected |
| B. Temporary data/job grants to the provisioner | Reuses the impersonation and audit path | Mixes provisioning and data rights on one identity; contradicts the M6K "no data, no jobs" role; makes audit attribution harder |
| **C. Dedicated loader service account** (bootstrap root), expiring conditional grants, operator impersonation only in the window | Separation of duties; matches the M6K pattern; clean audit trail; no change to pilot authoritative policies if grants are project-level with `resource.name` and `request.time` conditions | One more identity and a new checker mode; condition support must be verified (dependency 2) |

**Recommendation: C.** Grants live in the bootstrap root, never as members of the authoritative bucket or dataset policies. A pilot apply would otherwise remove them, and they would appear as drift.

### 6.3 Executor

| Option | For | Against |
| --- | --- | --- |
| **Operator-run `gcloud storage` / `bq` commands generated from the packet, with offline verifiers of captured JSON** | Same pattern as M6S; no new dependency (no Google SDK is installed); credentials never in the assistant's session | Manual and slower; relies on exact copy-paste |
| Python executor (`google-cloud-storage`, `google-cloud-bigquery`) | Automatable, easier to verify inline | New dependencies, in-process credentials, a new attack surface; premature before one manual run |

**Recommendation:** operator-run commands for M7D; consider an executor only after a successful manual run.

## 7. Proposed milestones

| Milestone | Type | Content | Exit criteria |
| --- | --- | --- | --- |
| **M7A** | Offline: assessment | This document. Decisions requested: loader option C; executor; headroom (G8); rollback (G9); marts deferred to M8 | **Done (2026-11-12).** Decisions still open, except headroom: 4,000 transactions adopted in M7B |
| **M7B** | Offline: code | **Ingestion packet** (G2) and the controlled 4,000-transaction snapshot (G8) | **Implemented (2026-11-12), committed `594a25d`:** 103 tests, 28/28 mutations detected; see [M7B validation](m7b_validation.md) |
| **M7C1** | Offline: code and Terraform (no plan or apply) | Loader identity and expiring grants in the bootstrap root, disabled by default (G1); `loader-identity`, `loader-access` and `loader-revoke` checker modes | **Implemented (2026-11-12), committed `1a59b47`:** see [M7C1 validation](m7c1_validation.md). Live gates L0–L7 defined there |
| **M7C2** | Offline: code and docs (no plan or apply) | Pilot data-metadata drift tolerance (G6); command bundle binding the custody bucket name (G3); result verifiers (G4); `tabledata.list`→CSV converter and inputs-only reconciliation (G5, moved from M7B); Gate C rewrite (G7); M7D runbook including the M7C1 live gates | **Implemented (2026-11-13), uncommitted:** see [M7C2 validation](m7c2_validation.md) and the [M7D runbook](m7d_runbook.md). Offline `validate` remains gate L0 |
| **M7D** | **GCP operations, separately authorised**, run by the operator | Read-only pre-checks; bootstrap `loader-identity` and `loader-access` plans, checks and applies, with live gates L0–L7 ([M7C1](m7c1_validation.md)); upload; describe; three loads; job capture; `tabledata.list` acquisition; offline verification; `loader-revoke` and read-back; state backups; evidence. Procedure: [M7D runbook](m7d_runbook.md) | 3 tables match M3 exactly per row, key and currency; access revoked; backups verified; a closure document |

Mart SQL in BigQuery (Gate C steps 4–5), with the seven-table comparator, follows as **M8**.

## 8. Risks

1. **Loader privilege creep:** any grant via the pilot root's authoritative policies would be wiped or would cause drift. Use bootstrap-root conditional grants only.
2. **IAM Conditions limits:** if `resource.name` conditions are unsupported for a needed permission, the fallback is broader (project-wide) for up to 8 hours. That needs explicit review, not silent widening.
3. **TIMESTAMP format rejection** at the first real load (no dry run). Mitigation: atomic load, `WRITE_EMPTY`, a fresh job ID per corrected packet.
4. **Exactly-at-limit volume** (5,000 transactions). Any variance breaches `max_transactions`.
5. **Local path disclosure** if manifests are uploaded unredacted.
6. **Checksum mismatch semantics:** SHA-256 is local only; the cloud comparison relies on MD5/CRC32C plus a post-load full-content reconciliation.
7. **Later Terraform drift** from data-bearing tables blocks pilot plans until G6 lands.
8. **Wrong-but-successful load:** no delete rights by design. Recovery needs a reviewed truncate or time travel within the window.
9. **Operator-executed evidence** is reported, not observed. Verifiers reduce but do not remove copy-paste and transcription risk.
10. **Carried over:** personal-account dataset owner, unrestricted project Owner (P8), budget alerts are not caps, and the unmapped numeric identity in M6T audit entries. Loader impersonation in M7D should be used to close that gap (map the numeric ID once).

## 9. Recommended first coding task (M7B)

**An ingestion packet builder and verifier**, `financial_crime_ml/deployment/ingestion_packet.py`, with `build` and `verify` CLIs:
- Input: one M3 run directory. Run `verified_source` first; refuse anything not `completed` (with-rejects only if a quarantine-totals approval is passed explicitly).
- Copy exactly `curated/transactions.csv`, `validated/customers.csv` and `validated/accounts.csv` into a new packet directory. Record each file's SHA-256, base64 MD5, size, row count and per-currency minor-unit totals, plus the SHA-256 of the M3 `manifest.json`.
- Assert the limits from `pilot_controls.json` (transactions, total bytes), and that no label or outcome field is present.
- Derive create-only object names (`m7/<packet_id>/<table>.csv`), deterministic job IDs, and three load configurations from `source_tables.json`: CSV, skip 1 header row, no autodetect, `max_bad_records=0`, `CREATE_NEVER`, `WRITE_EMPTY`, region `europe-west2`, UTF-8, no jagged rows or quoted newlines.
- Write a **redacted** packet manifest (no absolute paths, no git status) and print its SHA-256 for approval.
- `verify` re-hashes everything and fails on any difference.

Tests should cover tampering with any file or manifest, an over-limit row count or size, schema order drift, absolute-path leakage, determinism (same input → same packet hash and job IDs), and a refusal to overwrite an existing packet. The task needs no cloud access, no new dependency and no Terraform change.

**Stopped after M7A.** Nothing was implemented, and no cloud, Terraform, IAM or custody action took place.
