# M7D runbook: controlled synthetic ingestion into the GCP pilot

Prepared offline in M7C2 (2026-11-13). Updated the same day for the [M7D table IAM incident](m7d_incident_table_iam.md): the first `loader-access` apply partially failed because BigQuery rejected conditional table bindings. The three input-table grants are now **unconditional** and are ended only by revocation. Before any new window, run the incident re-entry gates R1–R5 (§5.6). **Apart from that partially failed window and its recovery, nothing in this runbook has been executed.** M7D is a separately authorised GCP operation. The operator runs every step by hand in **Terminal.app**, never in Claude Code. The assistant only reviews outputs the operator chooses to share, and only after redaction.

Scope: load three synthetic input tables (`customers`, `accounts`, `curated_transactions`) from the pilot bucket into the existing, empty BigQuery tables, prove that they match the approved packet exactly, and revoke all loader access. **The four marts are out of scope** (M8).

Related documents:
- [M7A assessment](m7a_assessment.md)
- [M7B packet](m7b_validation.md)
- [M7C1 loader identity and gates L0–L7](m7c1_validation.md)
- [M7C2 validation](m7c2_validation.md)
- [M7D incident: conditional table IAM rejected](m7d_incident_table_iam.md)
- [Gate C](../gcp_pilot_plan.md#gate-c-bounded-synthetic-workload)

Conventions:
- `C="$HOME/.secure/ebank-pilot-dev-261031-k7m4"`.
- Every Terraform call goes through `scripts/terraform_custody.py run` in the M6P `env -i` form, exactly as in [M6S](m6s_validation.md#runbook-terminalapp-only-never-in-claude-code).
- `BUCKET` is always read from custody, never typed or pasted:
  ```sh
  BUCKET="$(sed -n 's/^bucket_name *= *"\(.*\)"$/\1/p' "$C/inputs/pilot-dev.tfvars")"
  ```
  Never `echo` it, and never paste it into a document, chat or commit.

## 1. Approvals (each recorded separately before its step)

| # | Approval | Pinned value | Checked by |
| --- | --- | --- | --- |
| A1 | **Code:** the commit that contains M7B, M7C1 and M7C2, with CI green and a clean tree | Commit SHA | `git status`, CI |
| A2 | **Packet:** rebuilt from the A1 commit with `configs/synthetic_v2_m7_pilot.yaml` (4,000 transactions) | Manifest SHA-256 and packet ID | `verify-ingestion-packet --expected-manifest-sha256` |
| A3 | **Loader identity:** bootstrap `loader-identity` saved plan | Plan SHA-256 | `--mode loader-identity` (`ok`, 4 creates, 11 unchanged) |
| A4 | **Window:** start and expiry in UTC; recommended **3 h**, at most 8 h | `EXP` | Terraform validation; bundle refuses longer windows |
| A5 | **Loader access:** bootstrap `loader-access` saved plan | Plan SHA-256 | `--mode loader-access --packet-id --bucket` (`ok`, 6 creates, 15 unchanged). Three creates carry the exact expiry; the three table members carry **no** condition |
| A6 | **Command bundle:** rendered for the A2 packet, the custody bucket and the A4 window | Bundle SHA-256 | `verify-bundle` step |
| A7 | **Cost envelope:** see §2 | Values in §2 | Evidence ledger in `report.json` |
| A8 | **Optional read probes:** the operator-identity prefix listing (`inventory`); the L5 out-of-prefix probe, or Policy Troubleshooter instead (separate API enablement) | Yes or no for each | Evidence present or absent |
| A9 | **Revocation:** `loader-revoke` saved plan. Recommended as pre-approved with A5, since it only removes access. **Mandatory:** the table grants do not expire on their own | Plan SHA-256 | `--mode loader-revoke` (`ok`, 6 deletes) |
| A10 | **Post-load pilot check:** a pilot plan, **not applied** | Plan SHA-256 | `--mode pilot-loaded` (`ok`, 18 unchanged) |
| A11 | **Closure:** the redacted evidence and the M7D closure document | Report SHA-256 | Review |

Rules:
- An approval covers exactly the pinned value. A changed packet, plan, bundle or window needs a new approval.
- No approval authorises role widening, a new API, a quota or limit increase, or any delete.

## 2. Cost and volume limits (A7)

| Item | Limit | Expected | Enforced by |
| --- | --- | --- | --- |
| Upload bytes | ≤ 10 MiB (`max_total_upload_bytes`) | About 1.1 MB (packet `limits.total_upload_bytes`) | Packet verifier; `Content-MD5` per object |
| Transactions | ≤ 5,000 (`max_transactions`) | 4,000 | Packet verifier |
| BigQuery jobs | ≤ 20 per execution | **Exactly 3 load jobs**; 0 query jobs; 0 dry runs | Immutable job IDs; job verifier rejects any job with query statistics |
| Query bytes billed | ≤ 100 MiB per job | **0** (load jobs are not billed as queries) | Job verifier rejects non-zero `totalBytesBilled` |
| Read-back | Complete `tabledata.list` pagination | 1 page each for `customers` and `accounts`; 4–5 for `curated_transactions` at 1,000 rows | Page limit in the bundle |
| Storage | ≤ 100 MiB, all versions | About 1.1 MB (GCS) plus about 1 MB (BigQuery logical) | Packet; `pilot-loaded` byte check |
| Spend | The £5 alert budget is **not a cap** | About £0 | Console review after 24–48 h |

**Stop** if any extra job appears, any query job runs, or any billed bytes are reported.

## 3. Stop rules (apply to every step)

1. Any non-success response, permission error, or unexpected identity, resource, attribute or value: **stop**. Do not retry with different settings, and do not widen roles in the window. Then revoke (§6.8) and review.
2. Never re-upload or re-load blindly:
   - an object that already exists (HTTP 412) is inspected with `describe`;
   - a job ID that already exists (HTTP 409) is inspected with `job`;
   - a corrected packet means **a new packet ID**, and so new object names and job IDs.
3. A failed load is atomic, so the table stays empty. Record it, revoke and review; nothing is deleted.
4. A wrong but successful load cannot be undone by the loader, which has no delete rights. Stop, revoke, and plan recovery (truncate DML or time travel) as a **separately reviewed** operation (M7A G9).
5. If the clock reaches `EXP` minus 15 minutes, the bundle refuses new steps. **Revoke at once.**
   - `EXP` stops **new** loader tokens and new jobs.
   - It does **not** end the three table grants. They persist until the `loader-revoke` apply removes them.
   - A loader token issued before `EXP` stays usable on those tables (read and streaming insert) until the token's own expiry, up to 1 hour, or until revocation, whichever comes first (incident §5).
6. Evidence files are never overwritten or edited. If one is unusable, record why, and capture a fresh one under a new name only after review.

## 4. Offline preparation (no cloud access)

1. Check out the A1 commit. Run `make gcp-offline-check` and `pytest`. **L0:** run `env -i … terraform_custody.py run --root bootstrap -- validate`.
2. Rebuild the packet (M7B §Sample packet) and verify it:
   ```sh
   PYTHONPATH=src python3 -m financial_crime_ml.cli verify-ingestion-packet \
     --packet-dir outputs/packets/<packet_id> --expected-manifest-sha256 <A2 hash>
   ```
3. Compute the post-load expectation that `pilot-loaded` will enforce, and record it with A2:
   ```sh
   PYTHONPATH=src python3 -c 'import sys,json; from financial_crime_ml.deployment.ingestion_bundle import loaded_table_expectations as e; print(json.dumps(e(sys.argv[1], sys.argv[2]), indent=1))' outputs/packets/<packet_id> <A2 hash>
   ```

## 5. Read-only pre-checks (L1)

1. `preflight --root bootstrap --phase run` and `--root pilot-dev --phase run` print no problems. `state list` shows 11 bootstrap and 18 pilot addresses.
2. The latest bootstrap and pilot state backups are verified (serials 26 and 21 at M6T closure, or later).
3. **Live baseline:**
   - the project policy has only the Owner binding and runner → `bankpilot_dev_jobs`;
   - the provisioner account policy is empty;
   - the audit config is unchanged;
   - all seven tables report `numRows` `0`.
4. **L1:** no principal other than the planned operator window grant will hold `getAccessToken`, Token Creator or `actAs` on the loader. Check the project policy for inherited Token Creator or Service Account User roles.
5. **Budget (Console):** the £5 budget is present, and month-to-date spend is about £0.
6. **Incident re-entry (R1–R5):** complete the [incident](m7d_incident_table_iam.md#6-re-entry-gates-before-a-new-loader-access-window) gates first. In particular, the `--mode loader-recovery` check on a base-tfvars plan must report `ok`, 0 creates, 0 deletes and 15 unchanged, and `state list` must hold none of the six grant addresses. The §6.1 loader identity already exists, so §6.1 is skipped; its read-back (no keys; exact role permissions; empty account policy) is repeated.

## 6. Window procedure

### 6.1 Loader identity (L2; A3)

1. Add to the **base** custody file `$C/inputs/bootstrap-dev.tfvars`: `loader_identity_enabled = true`.
   - This stays `true` from now on.
   - Setting it back to `false` would plan a destroy, which `prevent_destroy` refuses.
2. Plan with the base file only. Check:
   ```sh
   … show -json "$P" | python3 scripts/bootstrap_plan_check.py --mode loader-identity
   ```
   Expect `ok`, 4 creates and 11 unchanged. Record `shasum -a 256 "$P"`.
3. Apply the saved plan after A3. Then, immediately: `state_backup.py create --root bootstrap`, upload, and `verify-download`.
4. Read back:
   - the loader account exists with **no user-managed keys**;
   - the 3 custom roles have exactly the permissions in `infrastructure/iam/loader_roles.json`;
   - the account's policy is empty.
5. Record the loader's numeric `uniqueId` (for the §6.9 audit mapping).

### 6.2 Loader access window (L3; A4, A5)

```sh
EXP="$(date -u -v+3H +%Y-%m-%dT%H:%M:00Z)"   # A4; plan promptly
umask 077
printf 'loader_access_enabled = true\nloader_access_expiry  = "%s"\nloader_bucket_name    = "%s"\nloader_object_prefix  = "m7/ingestion/%s/"\n' \
  "$EXP" "$BUCKET" "<packet_id>" > "$C/inputs/bootstrap-dev-loader.tfvars"
P="$C/plans/bootstrap/$(date -u +%Y%m%dT%H%M%SZ)-loader-access.tfplan"
# plan -input=false -var-file="$C/inputs/bootstrap-dev.tfvars" \
#      -var-file="$C/inputs/bootstrap-dev-loader.tfvars" -out="$P"
shasum -a 256 "$P"
env -i HOME="$HOME" PATH="$PATH" python3 scripts/terraform_custody.py run --root bootstrap -- show -json "$P" \
  | python3 scripts/bootstrap_plan_check.py --mode loader-access --packet-id=<packet_id> --bucket="$BUCKET"
```

1. Expect `ok`, 6 creates, 15 unchanged and `bucket_approved: true`. The checker refuses any condition on a table grant and requires the exact expiry on the other three. Apply after A5. Then back up the bootstrap state and verify it.
2. **Read back with policy version 3:**
   - the project policy has exactly 2 conditional loader bindings (jobs; objects with the packet prefix), each with the A4 expiry;
   - each of the three input tables has exactly one loader binding, `bankpilot_dev_loader_tables`, **with no condition**; no mart table has one;
   - the loader account policy has the operator with `bankpilot_dev_impersonate` and the same expiry.
3. If an expiring grant lacks its condition, a table binding carries one, or any apply step errors: **stop, revoke (§6.8) and review.** Never re-apply a partially failed saved plan, and never remove the condition from the jobs, objects or token-minting grants.
4. Wait about 5 minutes for IAM propagation.

### 6.3 Render and approve the bundle (A6)

```sh
mkdir -m 700 -p "$C/m7d"
PYTHONPATH=src python3 -m financial_crime_ml.cli render-ingestion-bundle \
  --packet-dir "$PWD/outputs/packets/<packet_id>" --expected-manifest-sha256 <A2 hash> \
  --bucket "$BUCKET" --window-start "<window start, UTC>" --window-expiry "$EXP" \
  --output-dir "$C/m7d/<packet_id>"
```

- The command prints the bundle SHA-256, job IDs, object names and checksums. It never prints the bucket.
- It refuses to write inside any git worktree, refuses an existing directory, and writes files with mode 0600.
- **Review before A6:** read `m7d.sh` and the three `requests/*.job.json` files. Each request must be exactly the packet's load configuration plus `sourceUris`.
- Use the **same** `EXP` as the Terraform window.

### 6.4 Bundle steps (L4)

```sh
cd <repository checkout at the A1 commit>
export M7_APPROVED_BUNDLE_SHA256=<A6 hash>
B="$C/m7d/<packet_id>"
bash "$B/m7d.sh" verify-bundle
bash "$B/m7d.sh" preflight          # loader token works; three tables empty with the exact schema
bash "$B/m7d.sh" upload customers   # then accounts, then curated_transactions
bash "$B/m7d.sh" load customers     # then: bash "$B/m7d.sh" job customers  (repeat until DONE)
#   ... accounts, curated_transactions
bash "$B/m7d.sh" post-objects       # objects unchanged after the loads
bash "$B/m7d.sh" readback customers # then accounts, then curated_transactions
bash "$B/m7d.sh" inventory          # only if A8 approved it (operator identity, read-only)
bash "$B/m7d.sh" verify-all         # writes evidence/report.json (redacted)
```

**Every step:**
- re-checks the bundle against `M7_APPROVED_BUNDLE_SHA256`;
- re-verifies the packet;
- refuses to run outside the window;
- mints a fresh loader token (passed to `curl` by a builtin `printf`, never through argv);
- runs the offline verifier on what it captured before the next step can rely on it.

**What each step proves:**

| Step | Captured | Verified offline (`verify-ingestion-evidence`) |
| --- | --- | --- |
| `preflight` | `tables/<t>.preload.json` | Each table exists, is a plain `TABLE` in `europe-west2` with the exact packet schema, and has 0 rows and 0 bytes |
| `upload` | `upload/<t>.headers`, `objects/<t>.json` | Create-only PUT (`x-goog-if-generation-match: 0`), with Cloud Storage checking `Content-MD5` server-side. Name, bucket, generation, metageneration 1, size, MD5, CRC32C, `text/csv`, no encoding, holds, retention, custom metadata or composite; created inside the window. Server hash headers match |
| `load` / `job` | `jobs/<t>.insert.json`, `jobs/<t>.json` | The packet's job ID, project and location. Submitted by the loader. Every load setting equals the request (defaults may be omitted). `DONE` with no errors. Input files 1, input bytes = packet size, output rows = packet rows, 0 bad records, no query statistics, times inside the window |
| `post-objects` | `objects-post/<t>.json` | Same generation, metageneration, size and hashes as at upload |
| `readback` | `readback/<t>/table.before.json`, `page-NNNN.{token,json}`, `table.after.json` | Complete, chained pagination; `totalRows` constant; exact int64-microsecond TIMESTAMPs. The table rebuilt in the M3 CSV dialect is **byte-identical to the packet file (same SHA-256)**. Row identities, every value and per-currency count/amount totals match. Table metadata identical before and after (no writer during acquisition) |
| `inventory` | `inventory.json` | Exactly the three verified generations under the packet prefix; no other object or version |
| `verify-all` | `report.json` | All of the above, plus the evidence layout and the job/byte ledger |

**Checksum scope:**
- The object MD5/CRC32C/size checks are transfer checks. They are **not** SHA-256 proof.
- SHA-256 equivalence of the loaded data comes from the byte-identical rebuild in `readback`.

### 6.5 Negative checks as the loader (L5, non-destructive)

- `bigquery.tables.testIamPermissions` on each input table returns exactly `bigquery.tables.get`, `getData` and `updateData`. On a mart table it returns nothing.
- `projects.testIamPermissions` as the loader, asking for `bigquery.jobs.create`, `bigquery.tables.update`, `bigquery.tables.delete`, `bigquery.datasets.update`, `storage.objects.delete`, `storage.objects.list` and `iam.serviceAccounts.getAccessToken`, returns **only** `bigquery.jobs.create`.
  - The storage grants carry a `resource.name` condition, so a project-level answer is not evidence of their scope either way.
  - Record the result; do not infer prefix scope from it.
- Listing the bucket as the loader is denied.
- **Out-of-prefix probe** only if A8 approved it (a zero-byte create elsewhere must be denied). If it is wrongly allowed, it leaves a stray object that is removed at Gate D. Record it as a finding.

### 6.6 Audit event check

In Logs Explorer (Data Access), filter `protoPayload.methodName="GenerateAccessToken"` over the window.
- Expect the operator as caller and the loader as resource, matched by the §6.1 `uniqueId`.
- This closes the M6T unmapped-identity limitation **for this window**.

### 6.7 Optional revocation enforcement (L6)

The M7C1 table-condition test no longer applies: the table grants have no condition, so a token minted before `EXP` keeps table access until revocation (incident R-A).

Only if approved, test instead that revocation cuts an existing token:
1. Mint a loader token before the §6.8 revocation.
2. Revoke, and wait about 5 minutes for propagation.
3. Call `tabledata.list` on one input table with that token. Expect a denial.

Keep the token in a shell variable only, and `unset` it afterwards.

### 6.8 Revocation (L7; A9) — straight after §6.4–6.6; start no later than `EXP` − 15 minutes

The table grants are unconditional, so this step, not `EXP`, ends the loader's table access.

1. Plan with the **base file only** (which keeps `loader_identity_enabled = true`). Check with `--mode loader-revoke`: expect `ok` and 6 deletes, including the three unconditional table grants. Apply.
   - If the revoke apply fails part-way, follow **§6.8.1**. Never leave a table grant in place.
2. Back up the bootstrap state, upload and `verify-download`.
3. `rm "$C/inputs/bootstrap-dev-loader.tfvars"`.
4. **Read back (mandatory; closure is blocked without it):**
   - no loader binding on the project or the loader account;
   - **each of the three input-table policies has no loader member.** The pilot root cannot see these members, so `pilot-loaded` cannot detect a leftover;
   - a loader token mint fails with `PERMISSION_DENIED`, and that attempt is logged.
5. The loader identity stays, dormant and without grants.

### 6.8.1 Emergency recovery after a part-way failed revocation

Use this when a `loader-revoke` apply (or a `loader-access` apply) stops part-way, so some grants are gone and others may remain.

1. **Stop.** Do not re-apply the failed saved plan, and do not retry with different settings.
2. **Live IAM read-back (mandatory, operator identity, read-only, policy version 3).** Record for each of the six grants whether it is still live:
   - the project policy: the loader jobs and objects bindings;
   - the loader account policy: the operator's `bankpilot_dev_impersonate` binding;
   - **each** of the three input-table policies: the loader `bankpilot_dev_loader_tables` binding.
3. **Contain** (if the window may still be in use): no further bundle steps. A token issued before the failure stays usable on any remaining table grant until the token expires (up to 1 hour).
4. **Re-plan with the base file only** and check it:
   ```sh
   … show -json "$P" | python3 scripts/bootstrap_plan_check.py --mode loader-revoke-partial
   ```
   - Expect `ok`, 0 creates and 15 unchanged. `delete_addresses` must equal **exactly** the grants that step 2 found still live **and** that state still records. `already_removed` and `absent_from_state` cover the rest.
   - If all six are still in state, the checker refuses and names `--mode loader-revoke`: use the strict mode.
   - **Stop and review** if the check fails, or if any planned delete is not live per step 2 (or a live grant is neither planned for deletion nor in state). A live grant that state does not record cannot be removed by Terraform. Removing it outside Terraform is an operator decision, recorded as in the [incident](m7d_incident_table_iam.md#2-emergency-recovery-completed-by-the-operator).
5. Approve the plan SHA-256 and apply it. Back up and `verify-download`.
6. **Live IAM read-back again (mandatory).** None of the six grants may remain. In particular, each of the three table policies has no loader member. A loader token mint fails with `PERMISSION_DENIED`.
7. Confirm with `state list` that none of the six grant addresses remain. Record everything for closure (A11), which is blocked until step 6 passes.

`loader-recovery` remains the narrower check for the original incident state, where the table grants were never created.

### 6.9 Live read-back after revocation (operator identity, read-only)

- `tables.get` on the three input tables: `numRows` and `numBytes` equal the §4.3 expectation; the four marts still report 0.
- The bucket and dataset policies are unchanged from M6T.

### 6.10 Pilot plan check (G6; A10) — plan only, no apply

```sh
# pilot-dev plan with -var-file="$C/inputs/pilot-dev.tfvars" -out="$P" (M6S form)
… show -json "$P" | python3 scripts/bootstrap_plan_check.py --mode pilot-loaded \
  --dataset-owner=<approved owner> --packet-dir=outputs/packets/<packet_id> \
  --expected-manifest-sha256=<A2 hash>
```

- Expect `ok`, 18 unchanged, and `accepted_load_drift` listing only input tables.
- **Do not apply.** If the check fails on `num_bytes` alone, the live logical-byte figure differs from the model: stop and review the model (§M7C2 Q3). **Never relax the check to pass.**
- The plan creates no state change, so no pilot backup is needed. Delete the saved plan file only after recording its hash.

### 6.11 State backups and evidence retention

**State:**
- bootstrap backups after §6.1, §6.2 and §6.8 (each verified after the OneDrive round-trip);
- the pilot state is not modified by M7D.

**Evidence:**
- Keep the bundle directory, including the evidence, in custody (`$C/m7d/`). It contains the bucket name.
- Only `evidence/report.json`, which is redacted, may be copied into the repository closure document, after review.

## 7. Closure (A11)

The M7D closure document records:
- the A1–A10 values;
- the `report.json` SHA-256 and its summary (per-table rows, rebuilt SHA-256, per-currency totals);
- the job IDs and object generations;
- the L1–L7 outcomes, including any findings;
- the incident re-entry gates R1–R5 and the `loader-recovery` report;
- the audit mapping;
- the backup serials;
- residual risks.

**No cloud success is claimed unless `verify-all` reported `ok: true`, revocation is read back (including all three table policies), and the `pilot-loaded` check passed.**
