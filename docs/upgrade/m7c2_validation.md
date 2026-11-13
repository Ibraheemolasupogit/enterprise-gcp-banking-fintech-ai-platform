# M7C2 operator bundle, evidence verifiers, read-back reconciliation and post-load plan check

Date 2026-11-13. Baseline `main` at `1a59b47` (M7C1), building on M7B (`594a25d`). **Offline only, uncommitted.**

M7C2 did none of the following:
- run gcloud, `bq`, `gsutil` or any cloud API call;
- run Terraform other than `fmt -check`;
- change IAM;
- access custody (`~/.secure`), state, plans or backups;
- download anything or change dependencies;
- commit or push.

The generated operator script ran **only inside a test sandbox**. There, `PATH` holds fake `gcloud` and `curl` (`tests/m7c2_support.py`) and no real cloud tool.

## Summary

| Gap | Delivered |
| --- | --- |
| **G3** command bundle | `render-ingestion-bundle` renders, from a verified M7B packet, a bash script plus exact job request bodies. It binds the custody bucket and the approved window, and is written only outside any git worktree. Every step is operator-typed and fail-closed |
| **G4** result verifiers | Independent offline checks of captured object metadata, upload response headers, `tables.get`, load jobs, post-load objects and the optional prefix inventory. Unknown attributes fail closed |
| **G5** read-back | A strict `tabledata.list` converter (exact int64-microsecond timestamps). Each input table is rebuilt **byte-identical to its packet file**. Row identities, values and per-currency totals reconcile. Inputs only |
| **G6** pilot drift | A new checker mode, `pilot-loaded`: all 18 resources unchanged; the only drift allowed is input-table `num_rows`/`num_bytes` from 0 to the exact packet values, with `etag` and `last_modified_time`. The M6 and M7C1 modes are unchanged |
| **G7** runbook | Gate C rewritten around the loader. The [M7D runbook](m7d_runbook.md) covers approvals, cost limits, stop rules, the L0–L7 gates, evidence, post-load checks, revocation, live read-back and backups |

## Changed files

| File | Change |
| --- | --- |
| `src/financial_crime_ml/deployment/ingestion_bundle.py` | **New.** `derive`, `render_bundle`, `load_bundle` (independent re-derivation), `loaded_table_expectations`, the script template, and the `render-ingestion-bundle` CLI |
| `src/financial_crime_ml/deployment/ingestion_evidence.py` | **New.** Pure verifiers: `verify_object`, `verify_upload_headers`, `verify_post_object`, `verify_inventory`, `verify_table`, `verify_load_job`. Also a pure-Python CRC32C (RFC 3720 vectors) and the BigQuery logical-byte model |
| `src/financial_crime_ml/deployment/ingestion_readback.py` | **New.** `convert_pages`, `timestamp_from_micros`, `reconcile_table`, `acquisition_stable` |
| `src/financial_crime_ml/deployment/ingestion_verify.py` | **New.** Stage runner and the `verify-ingestion-evidence` CLI (stages `bundle`, `preload`, `objects`, `job`, `post-objects`, `readback`, `inventory`, `all`). Output is redacted JSON |
| `src/financial_crime_ml/cli.py` | Two subcommand dispatches. Nothing else changed |
| `scripts/bootstrap_plan_check.py` | `pilot-loaded` mode, `--packet-dir` and `--expected-manifest-sha256`. **Additive:** the existing mode logic and drift tables are unchanged, and the packet arguments are refused for every other mode |
| `tests/m7c2_support.py` | **New.** Fixture packet builder and the fake cloud (stub `gcloud`/`curl`) |
| `tests/test_ingestion_bundle.py` | **New.** 56 tests |
| `tests/test_ingestion_evidence.py` | **New.** 149 tests |
| `tests/test_ingestion_readback.py` | **New.** 46 tests |
| `tests/test_pilot_loaded_plan_check.py` | **New.** 110 tests |
| `Makefile` | `gcp-offline-check` includes the four new test files |
| `docs/gcp_pilot_plan.md` | Gate C rewritten (G7); status row updated |
| `docs/upgrade/m7d_runbook.md` | **New.** The M7D procedure |
| `docs/upgrade/m7a_assessment.md`, `docs/index.md` | Roadmap status and links |

**Unchanged:**
- all existing tests, the M7B packet code and the M7C1 Terraform;
- every M6 and M7C1 checker mode;
- `reconcile.py`, the seven-table comparator.

## G3: operator command bundle

**Rendering** (`render_bundle`) requires:
- the packet, verified against an approved manifest SHA-256;
- a bucket name matching the bootstrap validation;
- a strict-UTC window of at most 8 hours, longer than the 15-minute safety margin.

The loader email is **derived** (`<dataset prefix>-loader@<project>`), never supplied.

**Output location:**
- must not exist;
- must not be inside any git worktree, the repository or the packet;
- paths must be shell-safe.

It is created with mode 0700, and files are written create-exclusive with mode 0600. The CLI prints only the bundle SHA-256, job IDs, object names and checksums, **never the bucket**. Error messages are redacted.

**Bundle contents:**
- `m7d.sh`;
- `bundle.json` (the record);
- `requests/<table>.job.json`: the packet's `configuration.load` minus the packet-only `sourceObject` and `sourceUriTemplate`, plus `sourceUris = ["gs://<bucket>/<object>"]` and the packet's `jobReference`;
- `packet.sha256`;
- `SHA256SUMS`;
- an empty `evidence/` tree.

**The bundle SHA-256 is the SHA-256 of `SHA256SUMS`** (approval A6).

**Re-derivation** (`load_bundle`): it re-verifies the packet, re-derives every bundle file from the recorded inputs, and requires byte identity, no extra files and no symbolic links. A consistently rewritten record (bucket, window, repository or packet path) is still rejected.

**Script properties** (each tested in the sandbox):

| Property | Mechanism |
| --- | --- |
| Approved bundle only | Every step requires `M7_APPROVED_BUNDLE_SHA256` to equal the SHA-256 of `SHA256SUMS`, and `shasum -c` to pass |
| Approved packet only | Every mutating step runs `shasum -c` on the packet plus a full offline re-derivation |
| Window | Refuses before the start and in the last 15 minutes before expiry |
| Create-only upload | XML API `PUT` with `x-goog-if-generation-match: 0` and `Content-MD5` (server-side byte check). HTTP 412 stops: "describe and review" |
| One submission per job ID | `jobs.insert` with the packet's job ID. HTTP 409 or a transport error stops ("never resubmit"). Insert evidence is never overwritten |
| No destructive verbs | Only `GET`, one `PUT` and one `POST` call site. No delete, patch, IAM, `gsutil`, `bq` or Terraform (asserted) |
| Token handling | A fresh `gcloud auth print-access-token --impersonate-service-account=<loader>` per step. The token reaches `curl` via `-H @<(printf …)`, never through argv (asserted from the fake-cloud call log) |
| Sequencing | Upload needs verified empty tables. Load needs verified object evidence. Read-back needs a verified job. Each step runs the offline verifier on what it captured |
| Evidence | `set -o noclobber`; evidence files are refused if present; the job poll is recorded only once `DONE` |
| Identity separation | Every call uses the loader token except the optional `inventory`, which uses the operator's own read-only listing (approval A8) |

## G4: evidence verifiers

All are pure functions. **Any unreviewed attribute in a response is a problem**, so an API shape change fails closed.

- **Objects:**
  - exact bucket, name and `id`;
  - positive decimal generation;
  - metageneration `"1"`;
  - size, MD5 and CRC32C equal to the packet;
  - `text/csv`;
  - created inside the window;
  - rejected if present: content encoding, composite, CSEK/KMS, holds, retention, soft or hard deletion, ACL/owner, custom metadata.

  A `sha256` claim in custom metadata is rejected: it would be an assertion, not proof.
- **Upload headers:** the final non-100 response is 200; `x-goog-generation` equals the described generation; the server `x-goog-hash` MD5 and CRC32C equal the packet values; stored length matches; stored encoding is `identity`.
- **Post-load objects:** generation, metageneration, size and hashes are unchanged.
- **Tables:**
  - the exact reference and `id`;
  - `TABLE` in `europe-west2`;
  - the exact schema in order (`INTEGER` accepted only as the legacy name for `INT64`);
  - `numRows` exact; `numBytes` `0` when empty;
  - no expiration, partitioning, clustering, streaming buffer, external, view or CMEK settings.
- **Load jobs:**
  - the exact job ID, project, location and `id`;
  - `user_email` and `principal_subject` are the loader;
  - a plain `LOAD` with no dry run or labels;
  - every request setting is echoed exactly, with booleans and integers kept distinct. Only settings whose packet value is the documented API default may be omitted;
  - no unrequested setting;
  - `DONE` with no `errorResult` or `errors`;
  - `inputFiles` 1, `inputFileBytes` equal to the packet size, `outputRows` equal to the packet rows, `badRecords` 0;
  - no query statistics or billed bytes;
  - creation, start and end times ordered and inside the window.

  A pending job reports `pending` (exit 3), not success or failure. A failed job reports its reason, redacted, and the readback step refuses to run.
- **Inventory:** exactly the three verified generations under the packet prefix, with no other object, version or sub-prefix, and a complete listing.

## G5: read-back converter and reconciliation

- **Pagination:** contiguous pages. The first token is empty; each following token equals the previous page's `pageToken`; the last page has none. `totalRows` is a constant decimal string equal to the packet rows. The page count is bounded by the bundle.
- **Values:**
  - cells exactly `{"v": "<string>"}`; NULLs rejected (all columns are REQUIRED);
  - INT64 is an exact decimal with no leading zeros;
  - TIMESTAMP is **int64 microseconds** (requested with `formatOptions.useInt64Timestamp=true`), whole seconds only, converted by integer arithmetic to `YYYY-MM-DDTHH:MM:SSZ`. Float-second forms are rejected.
- **Reconciliation:**
  - unique primary keys;
  - the key sets are equal;
  - every row equals its packet row;
  - the rows sorted by lineage `source_row` and written in the M3 CSV dialect are **byte-identical to the packet file** (same SHA-256 and size);
  - for `curated_transactions`, the per-currency row counts and `amount_minor` sums equal the packet reconciliation.

  Table metadata before and after acquisition must be identical (`etag`, `numRows`, `numBytes`, `lastModifiedTime`).
- **Scope:** the three input tables only. The marts and the seven-table comparator are M8.

## G6: `pilot-loaded` checker mode

The mode requires `--dataset-owner`, `--packet-dir` and `--expected-manifest-sha256`. The packet is verified by the M7B verifier, then turned into the exact expectation: per input table, `num_rows` and `num_bytes`, the BigQuery logical size (STRING 2 + UTF-8 bytes; INT64 and TIMESTAMP 8).

| Rule | Detail |
| --- | --- |
| Planned changes | All 18 pilot resources `no-op`. Any create, update, replace or delete is rejected. The existing pilot value checks, the owner rule and the forbidden-token scan all apply |
| Refreshed sizes | Input tables: `num_rows`, `num_bytes` exact and `num_long_term_bytes` 0. The four marts: 0, 0, 0. These are integers, not strings or booleans |
| Accepted drift | Only on the three input tables, once each, as `update`, with identical attribute sets and no unknowns. Changes are **exactly** `num_rows` 0 → packet rows, `num_bytes` 0 → packet logical bytes, plus `etag` (non-empty) and `last_modified_time`, which moves forward and is not after the plan timestamp + 5 minutes. All four must change together |
| Rejected drift | Anything else, including schema, labels, deletion protection, expiration, `resource_tags`, `num_long_term_bytes`, a reload on top of existing data, and drift on the dataset, bucket, policies, runner or loader members, or marts |
| Persisted state | If a later apply already persisted the sizes, no drift is needed. The sizes are then checked on the `no-op` entries |

**Reference packet** (the M7B sample, rebuilt offline in this session with the identical packet ID `m7p-a89332976c69d583137f9323` and manifest `7e2fb8b7…540f`; not an approval):

| Table | `num_rows` | `num_bytes` (logical model) | Read-back pages at 1,000 rows (bundle limit) |
| --- | --- | --- | --- |
| `customers` | 100 | 13,100 | 1 (2) |
| `accounts` | 150 | 21,750 | 1 (2) |
| `curated_transactions` | 4,000 | 982,049 | 4 (5) |

## G7: Gate C and the M7D runbook

- **[Gate C](../gcp_pilot_plan.md#gate-c-bounded-synthetic-workload)** is split:
  - **C-inputs (M7):** the loader identity, approvals, bundle execution, checksum semantics, limits and stop rules;
  - **C-marts (M8):** the retained mart design, now assigned to the runner, **not authorised**.

  The provisioner-as-loader text is removed.
- **[M7D runbook](m7d_runbook.md):**
  - approvals A1–A11;
  - cost and volume limits;
  - stop rules;
  - offline preparation (L0);
  - read-only pre-checks (L1);
  - identity (L2);
  - the access window (L3);
  - bundle render and approval;
  - bundle steps with per-step evidence (L4);
  - negative checks (L5);
  - the audit mapping;
  - optional L6;
  - revocation (L7);
  - live read-back;
  - the `pilot-loaded` plan (not applied);
  - backups, evidence retention and closure.

## Tests

| File | Tests | Covers |
| --- | --- | --- |
| `test_ingestion_bundle.py` | 56 | Derivation from the packet; determinism; private, create-exclusive output; invalid buckets (11) and windows (6); manifest hash enforcement; git-worktree, repository and packet locations; shell-unsafe paths; tampered packets; edited files (6); consistent record swaps (5); extra or linked files; script content invariants. **Sandboxed execution:** full end-to-end run; missing or wrong approval (no calls made); outside the window (no calls made); edited request; tampered packet before upload; denied impersonation; load before upload; create-only upload and no retry; corrupted object (MD5/CRC32C); single job submission (409); failed job (redacted) blocking read-back; altered read-back; transport error without resubmission; unknown steps and tables |
| `test_ingestion_evidence.py` | 149 | CRC32C vectors; logical bytes; 31 object, 10 header, 16 table, 8 schema, 27 load-setting and 28 job mutations; omitted defaults; pending states; inventory variants; redaction |
| `test_ingestion_readback.py` | 46 | Byte-identical rebuild for 3 tables × 3 page sizes; money (including a compensating change); exact timestamps; 23 page mutations; substituted, duplicated and missing rows; scope; stability |
| `test_pilot_loaded_plan_check.py` | 110 | Accepted shapes (drift, persisted, partial); other modes unchanged; owner required; 10 malformed expectations; 22 drift mutations × 3 tables; non-update actions; shape problems; duplicates; 7 foreign drift addresses; marts; exact sizes; planned changes; extra resources and IAM; CLI packet binding and argument rules |

**Intent checks:** for every adversarial case in the G4, G5 and G6 tables, the failure reason was printed and checked against the intended check.

**Mutation check:** 42 mutations across the five changed modules and the checker. **All 42 were detected**, and the files were restored byte-identical (SHA-256 compared).

**Incident during the mutation run:**
- **What happened:** with the "git-worktree guard disabled" mutation active, one test really rendered a fixture bundle into `outputs/never-here/` in the checkout. This is the same class of incident as the M7B mutation run.
- **What was written:** the directory is git-ignored. It held only fixture data (bucket `fixture-pilot-bucket-7q2`, `source_id` `m7c2-fixture`, an empty `evidence/` tree).
- **Cleanup:** it was found by `ls outputs` after a later failing run, inspected, and removed. `outputs/runs/` predates this session and was not touched.
- **Fix:** both tests that point at the real repository now replace derivation (which precedes any write) with a failing stub, so a broken guard can no longer write into the checkout. Re-running that mutation alone: still detected, nothing written, file restored.

## Validation results

| Check | Result |
| --- | --- |
| Full `pytest` | **1789 passed, 1 skipped** (M7C1 baseline 1428 + 361) |
| `make gcp-offline-check` | **1545 passed, 1 skipped** (1184 + 361); `terraform fmt` passed |
| `ruff check .` | Passed |
| `terraform fmt -check -recursive` | Passed |
| Docs validator | 0 issues (5 Mermaid diagrams checked) |
| `final_project_check.py` | 79 checks, 0 issues |
| `git diff --check` (including new files) | Passed |

**Not run:** `terraform validate` (gate L0) and any shellcheck (not installed). The script was syntax-checked with `bash -n` under macOS bash 3.2 and exercised end to end in the sandbox.

## Security decisions needed before M7D (not decided here)

| # | Decision | Why it is a security decision | Default in this work |
| --- | --- | --- | --- |
| D1 | **Prefix inventory under the operator's identity** (`inventory` step) | The loader deliberately cannot list. Proving "no extra objects under the prefix" needs a read by a broader identity (the operator, who is effectively project Owner) | Optional, off unless A8 approves. Without it, absence of extra objects rests on create-only, content-addressed names and the loader's lack of list rights |
| D2 | **L5 out-of-prefix probe vs Policy Troubleshooter** (carried from M7C1) | A probe that wrongly succeeds leaves a stray object the loader cannot delete; Policy Troubleshooter needs a new API enablement | Neither implemented in the bundle |
| D3 | **Window length** | Tokens minted inside the window remain valid for up to 1 h after expiry | 3 h recommended; the bundle starts no step in the last 15 min |
| D4 | **Job read-back identity** | The bundle reads load jobs as the loader, relying on BigQuery letting a job's creator get its own job without `bigquery.jobs.get` (M7C1 Q6). If denied, the fallback is an operator (Owner) read | No fallback implemented; a denial stops the step. Adding `jobs.get` to the loader or an operator read needs approval |
| D5 | **Bundle trusts the repository checkout** | The offline verifier the script calls is the repository's Python code at execution time. The bundle pins the packet and itself, **not the code commit** | Runbook A1 requires a clean checkout at the approved commit. Pinning the commit inside the bundle was not added (see R6) |

## Unresolved risks and API questions

| # | Item | Effect if wrong | Resolved by |
| --- | --- | --- | --- |
| R1 | XML API create-only `PUT` needs only `storage.objects.create` (with `x-goog-if-generation-match: 0`), and the server rejects a `Content-MD5` mismatch (`BadDigest`) | Denied upload or unchecked MD5. The post-upload MD5/CRC32C check still catches corruption | L4 |
| R2 | `tables.get`/`tabledata.list` and `jobs.get` response shapes match the reviewed attribute lists | Unknown attribute: the verifier fails closed. A reviewed code change is needed, never a relaxation at run time | L4 |
| R3 | **Logical-byte model** (STRING 2 + bytes; INT64/TIMESTAMP 8) equals BigQuery `numBytes` under the `LOGICAL` billing model | `pilot-loaded` fails closed on `num_bytes` and pilot plans stay blocked until reviewed. The evidence report flags `num_bytes_matches_logical_model` early, without failing the data check | `readback` evidence before §6.10 |
| R4 | `numLongTermBytes` becomes non-zero after 90 days without modification | `pilot-loaded` rejects it, so the mode is valid for 90 days after the load | Review before then |
| R5 | Job type cannot be restricted (carried): `jobs.create` plus table `updateData` allows query or DML jobs in the window | Unledgered job or modified rows | Job verifier only covers the three IDs; full read-back reconciliation exposes changes; audit logs; short window |
| R6 | The bundle does not pin the code commit (D5) | Modified local verifier code could weaken checks | Clean checkout at A1; CI |
| R7 | Evidence and bundle contain the bucket name | Disclosure if copied out of custody | Custody only; only the redacted `report.json` leaves it |
| R8 | The fake cloud models documented behaviour, not Google's implementation | Shape or semantic differences surface only live | Fail-closed verifiers; stop rules |
| R9 | Carried from M6/M7C1: unrestricted project Owner, personal-account dataset owner, alerts that are not caps, BigQuery table-condition support (Q1/Q2), storage prefix condition (Q3), load permission set (Q4) | See [M7C1](m7c1_validation.md#unresolved-provider-and-api-questions) | L0–L7 |

## Precise M7D approval gates

M7D may start only after M7C2 is committed with CI green. Then, in order:

1. **A1:** the code commit.
2. **L0:** offline `validate`.
3. **A2:** the packet manifest SHA-256 (rebuilt at A1), plus the recorded `pilot-loaded` expectation.
4. **L1:** read-only pre-checks.
5. **A3:** the `loader-identity` plan, then L2 and a backup.
6. **A4:** the window.
7. **A5:** the `loader-access` plan, then L3 (policy read-back with conditions) and a backup.
8. **A6:** the bundle SHA-256.
9. **A7:** the cost envelope.
10. **A8:** the optional probes (D1, D2).
11. **L4:** the bundle steps through `verify-all` (`ok: true`).
12. **L5:** negative checks, then the audit mapping and optional L6.
13. **A9/L7:** revocation, read-back and a backup.
14. **Live read-back:** table sizes after revocation.
15. **A10:** the `pilot-loaded` plan check (not applied).
16. **A11:** closure.

Any failed gate means: stop, revoke if a window is open, and review. Details: [M7D runbook](m7d_runbook.md).

**Stopped after M7C2 offline implementation.**
