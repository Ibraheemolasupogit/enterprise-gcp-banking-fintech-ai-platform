# Controlled synthetic GCP pilot approval checklist

This runbook governs pilot infrastructure/workload operations. All cloud and Terraform steps are run by the operator under separate approvals; the assistant works offline. The status below is **operator-reported** and was last updated at the M6T closure (2026-11-12). Earlier M5–M6S records remain historical evidence.

## Current status (M6T closure)

| Gate / item | Status / source |
| --- | --- |
| Dedicated project | `ebank-pilot-dev-261031-k7m4`, number `203569506404` — user Console-verified (M6G) |
| Parent / billing link | No organization; linked existing GBP billing account — user Console-verified |
| Monthly project alert budget | `Enterprise Banking AI Pilot - Dev`, **£5**, **50%, 80%, 100%** thresholds — user Console-verified. Alerts are not caps |
| Existing portfolio budget | Unchanged — user Console-verified; protected from pilot changes |
| Location | `europe-west2`; the pilot bucket and dataset are deployed there |
| Gate A: bootstrap, state custody, saved plans | **Complete.** IAM bootstrap applied and verified ([M6Q](upgrade/m6q_validation.md), [M6R](upgrade/m6r_validation.md)); local backends and encrypted OneDrive backups in use ([M6L](upgrade/m6l_validation.md)–[M6O](upgrade/m6o_validation.md)) |
| Gate B: infrastructure | **Complete.** All **18 pilot resources deployed**: 17 in M6S, and the dataset IAM policy in the M6T recovery (1 created, 0 changed, 0 destroyed). Dataset access read back as named user OWNER plus the runner's metadata role only ([M6T](upgrade/m6t_validation.md)) |
| Provisioner access | **Revoked** (2 grants destroyed); impersonation denied afterwards; temporary access-window input removed |
| State backups | Pilot serial 21 and bootstrap serial 26, encrypted and verified after a OneDrive round-trip |
| Gate C: synthetic workload | **Not started.** Tables are empty; no upload, load or query. Input-table ingestion prepared offline (M7B packet, M7C1 loader identity, M7C2 bundle, verifiers and [M7D runbook](upgrade/m7d_runbook.md)); runs only as a separately authorised M7D. The first M7D `loader-access` apply partially failed and was recovered; the table-IAM correction is implemented offline ([incident](upgrade/m7d_incident_table_iam.md)) |
| Gate D: cleanup | Not started; resources remain under deletion protection |

Open limitations, including the unmapped numeric service-account identity in the 8 `GenerateAccessToken` audit entries, are listed in [M6T](upgrade/m6t_validation.md#outstanding-limitations). **This pilot is not production-ready and performs no cloud data ingestion.**

Project creation, billing linkage and budget creation are **already reported complete**; do not request or repeat those operations. Approval for completed Console actions does not authorize API activation, IAM/bootstrap, state preparation, planning, apply or workloads. See [M6G source boundaries and next gate](upgrade/m6g_validation.md). No infrastructure/workload command in this runbook was executed by the assistant as part of M6G.

## Gate A: prerequisites, downloads and read-only planning

Use the Console-verified project/number above and intended `europe-west2`. Remaining approval inputs are fresh bucket/dataset/environment/prefix names, exact dedicated provisioner account, named operator and expiry, synthetic-only data classification, secure state custody/backup and reviewer. New-project API states, IAM/creator/default grants, resource conflicts, quotas, budget spend basis/recipients/delivery and authentication are not established by the Console summary. Verify them through separately approved bounded reads or explicit Console evidence. A bootstrap administrator must separately approve only missing API enablement and exact IAM/identity grants; never bundle them into a Terraform plan or copy old-project grants.

Use Terraform >=1.9 and inspect both environment examples. Base prefix is now environment-independent; the resulting account/role namespace includes `dev` or `test`. Require fresh names and separate state for each environment. From M6K, identity prerequisites come from the separate bootstrap root (`infrastructure/terraform/bootstrap/`, its own state, run by the bootstrap administrator without impersonating the provisioner). `provisioner.role.json` has no project IAM-write or custom-role administration; its remaining resource IAM writes are documented escalation paths. `cleanup.role.json` is an independent Gate D add-on. Record every approved bootstrap grant and its expiration; nothing is applied automatically. Missing permissions must return to review rather than triggering a broad predefined role grant.

M6A completed provider initialization/validation and lockfile generation; no routine provider download/upgrade approval is needed again. After separate state/backend and source-authentication approval, prepare exact ignored inputs and rerun validation if configuration changes. M6D's proposed local state path requires an explicit local backend before use. Each saved plan still requires its own approval. (Bootstrap and pilot plans have since been generated, approved and applied by the operator; see the status table above.) Audit provisioning permissions against the approved named operations; stop on missing permissions instead of widening roles automatically.

Review the complete plan and `terraform show -json` output. Expect one bucket, one dataset, seven empty typed tables, two authoritative resource policies and seven table-role members (**18 pilot resource instances**). The runtime account, five runtime roles and project jobs member are **bootstrap** instances (bootstrap total: 10 core instances, or 12 while provisioner access is enabled, plus 1 IAM Credentials audit config when enabled, the default: 11 / 13; see [M6P](upgrade/m6p_validation.md)). Pilot plans must contain no service-account, custom-role or project IAM resources, and must pass the identity postconditions. There must be no project creation, billing changes, API activation, replacement/destruction, downloads of data, extra services or credentials. Resource IAM policies may only target new dedicated resources. Verify all labels, schemas, locations, deletion guards, bucket versioning/soft-delete and full IAM diffs. Store plan binary/hash, Terraform/provider versions and lock checksum privately. Do not publish raw state or access tokens.

## Gate B: infrastructure and budget

Approve the **specific saved plan SHA256**, tfvars/environment, resource inventory, cost estimate, provisioning identity/permissions, state custody and an execution window. Apply only that saved plan after approval; stop on a stale plan or changed hashes. The user already confirmed the project-specific **£5 monthly budget with 50/80/100% thresholds**. Confirm spend basis, credit treatment, recipients/delivery and any forecast rule before paid workloads; those settings were not supplied. Preserve the existing portfolio budget. Review region-specific pricing/free-tier availability and quotas; no budget rewrite or extra Billing Budget API activation is implied. **Alerts are not spending caps.** Infrastructure creation approval does not authorize data loads or queries.

M6K aligned `infrastructure/pilot_controls.json` to the Console-verified **GBP £5 monthly, 50/80/100%** budget (source M6G; forecast rule recorded as unverified). The **US$1 workload allowance remains a separate manual proposal**; approve its currency treatment without treating £5 as a USD equivalent. **Budget alerts do not cap spending.** No billing configuration changed.

Capture resource IDs, effective IAM (including inheritance), schema exports and API/operation results. Confirm the runner has no project admin, schema-changing, object-deleting or impersonation grants; test an authorized read and a safe permission check for forbidden operations. Never prove denial by attempting destructive operations. Revoke provisioning access until maintenance/cleanup is approved.

## Gate C: bounded synthetic workload

**Rewritten in M7C2 (2026-11-13).** Gate C now has two parts:
- **C-inputs (M7):** load the three input tables. Prepared offline; it runs only as M7D, under the [M7D runbook](upgrade/m7d_runbook.md).
- **C-marts (M8):** build the four marts. Not prepared; **not authorised**.

The earlier text named the provisioner as uploader and loader. That contradicted its M6K role, which has no data or job permissions, and has been removed (gap G7).

### C-inputs: synthetic input-table ingestion (M7D)

**Identities:**
- **Loader:** the dedicated keyless account `bankpilot-dev-loader` (bootstrap root, [M7C1](upgrade/m7c1_validation.md)) uploads and loads. During one approved window of at most 8 hours (3 hours recommended), it holds only six grants, all removed by the `loader-revoke` apply:
  - `bigquery.jobs.create` on the project (expiring);
  - create-only `storage.objects.create`/`get`, narrowed by condition to `m7/ingestion/<packet_id>/` in the pilot bucket (expiring);
  - `tables.get`/`getData`/`updateData` on `customers`, `accounts` and `curated_transactions` only. These are **unconditional**, because BigQuery rejected conditional table bindings ([M7D incident](upgrade/m7d_incident_table_iam.md)). They end only at revocation;
  - operator token minting on the loader account (expiring).
- **Provisioner:** keeps no access.
- **Runner:** unchanged and read-only on the inputs.
- **Pilot policies:** the bucket and dataset policies (authoritative) are never edited for the loader.

**Approvals** (exact values pinned; see the [M7D runbook §1](upgrade/m7d_runbook.md#1-approvals-each-recorded-separately-before-its-step)):
1. the code commit;
2. the [M7B](upgrade/m7b_validation.md) packet manifest SHA-256 (4,000 synthetic transactions, about 1.1 MB, label-free, no local paths);
3. the `loader-identity` plan SHA-256;
4. the window;
5. the `loader-access` plan SHA-256;
6. the operator command bundle SHA-256;
7. the cost envelope;
8. optional read probes;
9. the `loader-revoke` plan SHA-256;
10. the `pilot-loaded` plan SHA-256 (not applied);
11. closure.

**Execution** is the M7C2 bundle (`render-ingestion-bundle`), run step by step by the operator in Terminal.app:
1. **Pre-checks:** the bundle and packet re-verify against their approved hashes. The loader token mints. The three tables are read back empty with the exact packet schema.
2. **Upload:** three create-only XML API uploads (`x-goog-if-generation-match: 0`, `Content-MD5` checked server-side) to the packet's content-addressed names. The object metadata is captured and verified: generation, metageneration 1, size, MD5, CRC32C, `text/csv`, no encoding, holds, retention or custom metadata.
3. **Load:** exactly three load jobs, each with its immutable packet job ID (`m7_load_<id>_<table>`). Settings: explicit `source_tables.json` schema, CSV with one header row skipped, no autodetect, `maxBadRecords=0`, no jagged rows or quoted newlines, no ignored values or schema updates, `CREATE_NEVER` and `WRITE_EMPTY`. Each job is submitted once:
   - HTTP 409 (ID exists) or a transport error stops the step;
   - a corrected packet gets a new ID.

   **BigQuery rejects generation-qualified load URIs**, so object generations are verified before and after the loads instead.
4. **Verification:** the captured jobs must be `DONE`, submitted by the loader, with every setting as requested, output rows equal to the packet, 0 bad records and no query statistics.
5. **Read-back:** full `tabledata.list` pagination with exact int64-microsecond timestamps. Each table is rebuilt **byte-identical to its packet file (same SHA-256)**; row identities, values and per-currency totals reconcile; no writer runs during acquisition.
6. **Revocation:** apply the `loader-revoke` plan, back up state, read back. Then the pilot plan check `pilot-loaded`, which accepts only the exact row and logical-byte drift on the input tables.

**Cloud Storage checksums.** Cloud Storage reports MD5 and CRC32C, not SHA-256. These are transfer checks only; SHA-256 equivalence comes from the read-back rebuild.

**Limits:**
- ≤ 5,000 transactions;
- ≤ 10 MiB uploaded;
- 3 jobs out of the 20 allowed;
- 0 query bytes;
- the £5 budget alerts **are not caps**.

**Stop rules:** stop on any permission error, unexpected value or extra job. Never widen a role, retry with different settings, or delete inside the window. A wrong but successful load is recovered only by a separately reviewed operation (M7A G9).

### C-marts: mart processing (M8; not prepared, not authorised)

The following design is retained for M8 and needs its own review, identity decision and approvals. **The runner**, not the loader or provisioner, would execute it after all loader access is revoked.

- **Rendering and validation:** render only the approved project/dataset identifiers into `marts.v1.insert.proposed.sql` (identifiers cannot be query parameters). Validate and estimate the four SELECT bodies individually before DML, because script dry runs have limitations. Require exact expected fields and grains, with no approximation.
- **Byte and job ledger:**
  - set `maximum_bytes_billed=104857600` on each applicable job, and keep a cumulative job and bytes ledger;
  - if a limit is unsupported or exceeded, stop for review;
  - execute the transactional script sequentially, replacing only the four pilot marts;
  - reuse a stable job ID/hash for an identical retry, and inspect its prior status.
- **Reconciliation:**
  - reconcile every mart's transaction count and minor-unit sum **per currency** against M3 and M4;
  - compare every grain's counts, exact sums, distinct active entities, first/last UTC instants, joins and required fields;
  - check duplicate grains and anti-joins;
  - sort and normalise DATE/TIMESTAMP/INT64 representations before comparing; do not require cloud storage hashes to match SQLite bytes;
  - use the existing seven-table comparator (`deployment/reconcile.py`) with mart read-backs acquired by `tabledata.list` under the runner.
- **Job reservation:** the 20-job limit includes parent and child jobs and dry runs. M7 uses 3; a conservative M8 reservation is 4 SELECT dry runs + 1 script dry run + 1 script parent + up to 10 child jobs. Enumerate the actual child jobs, and stop if BigQuery produces a different number. [Script dry-run limitations](https://docs.cloud.google.com/bigquery/docs/multi-statement-queries).
- **Evidence:** capture redacted job configurations, IDs, parent/child results, bytes processed and billed, cache status, output schemas and row counts, exact SQL and hashes, and actual start/end times.
- **No widening:** runtime permission errors are investigated against the approved IAM table. No automatic role widening, quota or scan-limit increase, schema relaxation or service enablement is authorised.

References:
- [CSV load limitations](https://docs.cloud.google.com/bigquery/docs/loading-data-cloud-storage-csv)
- [tabledata.list](https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/tabledata/list)
- [jobs.list](https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/jobs/list), with `parentJobId` and `allUsers=false` for child jobs under the submitting identity

## Gate D: cleanup and residual billing

Approve a named-resource cleanup inventory, exported evidence, retention decision, reversible backup checks, deletion plan and separate cleanup identity/temporary permissions. Protect shared project/billing settings. Runtime has no object-delete rights. Explicitly approve removing Terraform prevent-destroy and table deletion protection in reviewed configuration/state; do not remove resources from state to hide them and do not rely on deleting blocks to bypass protection.

Review the exact deletion add-on in `infrastructure/iam/cleanup.role.json` together with provisioning metadata/IAM access. Bind object deletion only to the pilot bucket and scope all other supported deletions as narrowly as possible; any unavoidable project-level rights require the dedicated sandbox and a short expiration. No table data read or job rights are implied by this add-on; approve any evidence-export access separately. Do not leave cleanup privileges attached to either operational identity.

After approved exports, authorize object deletion for **all** live and noncurrent generations and then the bucket, accounting for seven-day soft-delete retention/charges. No locked retention is configured. Remove table contents/tables explicitly, then the empty dataset; `delete_contents_on_destroy=false` and `force_destroy=false` must not be switched on as shortcuts. Review a saved destroy plan restricted to the exact pilot resources, then seek approval to execute it. Grant `cleanup.role.json` (now including `storage.objects.list` to enumerate noncurrent generations) through the bootstrap root only for the approved cleanup window. After dependent grants/resources are gone, remove the runtime account, custom roles and pilot IAM members through a separately reviewed **bootstrap-root** change (they are prevent-destroy). Revoke impersonation/bootstrap grants separately. Preserve required private state/evidence records.

Inspect the post-cleanup inventory, soft-deleted storage, BigQuery time-travel/fail-safe retention, outstanding jobs and delayed billing. Do not promise immediate zero storage charges. Do not disable shared APIs or delete the cloud project/billing account. Keep billing alerts until delayed charges are reviewed and document residual resources/costs.
