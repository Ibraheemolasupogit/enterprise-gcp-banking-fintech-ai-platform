# M8 closure: four analytics marts in the GCP pilot

Date 2026-11-18. Code `m8-marts-runner-access` at `ce3ec20`. Project `ebank-pilot-dev-261031-k7m4`, region `europe-west2`. Procedure: [M8 runbook](m8_runbook.md).

**Status: complete, with documented evidence gaps.**
- The four marts hold exactly the expected 11,914 rows. They were built by the runner in one transactional script and reconcile per currency to the 4,000 M7D transactions.
- The temporary operator-to-runner token-minting window was opened, verified, used and revoked. Final REVOKED capture: 8/8 PASS.
- The carried-over M7D limitations (A10, L5, bucket/dataset policy read-back) still stand: see [Gaps](#evidence-gaps).

The operator ran every cloud and Terraform step in Terminal.app. The assistant re-ran the offline verifiers on custody evidence, read-only. It made no GCP call, ran no Terraform command, and changed no evidence, state, plan or backup. The operator principal and bucket name are omitted.

## Outcome

| Mart | Rows | Logical bytes | Read-back CSV SHA-256 (= expected) |
| --- | --- | --- | --- |
| transaction_daily | 1,312 | 80,032 | `30d428d0182cac11…9875a` |
| customer_daily | 3,412 | 238,840 | `1bb7d414baf53eb6…93e2` |
| account_daily | 3,428 | 243,388 | `21560df0f20c8f93…901b` |
| operations_daily | 3,762 | 278,877 | `fe98aabf4b848d8b…bb20` |
| **Total** | **11,914** | 841,137 | |

**Financial reconciliation.** Every mart reconciles to the packet per currency (rows / minor units):

| Currency | Rows | Minor units |
| --- | --- | --- |
| EUR | 1,061 | 47,972,939 |
| GBP | 874 | 41,044,581 |
| JPY | 813 | 331,566 |
| USD | 1,252 | 54,122,799 |

There are no duplicate grains and no anti-join failures. Every event falls inside its UTC date, and every activity count is in range. Read-back `numBytes` equals the logical model for all four marts.

**Evidence.** `evidence/report.json`: `ok: true`, `problems: []`, SHA-256 `0a6d1ea33b84aaa7f8ba9748d515b0d0c9faceb7e8fd8c19bd418a9a3395a35c`. Re-running `verify-mart-evidence --stage all` offline reproduces it exactly.

## Approvals and hashes

| Item | Value |
| --- | --- |
| Code | `ce3ec20` (branch `m8-marts-runner-access`) |
| Packet | `m7p-a89332976c69d583137f9323`, manifest `7e2fb8b7500a4aaf0652d2f1381359d333011a5f2ff5f92b1b684be0142b540f` |
| Runner access plan | `20261118T013357Z-runner-access-ce3ec20.tfplan` `18957acbfdd8ff6cb0cce86fe50167589e209197b4d9876fcf1416761a1f63a1`: 1 create, 15 unchanged, expiry `2026-11-18T04:33:00Z` (2.98 h). Repo checker `runner-access` ok, accepting only the strict etag drift on `runner_jobs` and the audit config; independent verifier ok |
| Mart bundle | **`a4a563846d1f4f94d46201a56dfaaf167acd47ddd428cd308a9ec5d9bcdf48c1`** (14 files, all verify). Window 01:44:00Z–04:33:00Z, inside the runner window |
| Script job | `m8_marts_a89332976c69d583137f9323_85d9100f72d67a01_a1`, SQL SHA-256 prefix `85d9100f72d67a01` |
| Runner revoke plan | `20261118T020618Z-runner-revoke-ce3ec20.tfplan` `cbf8c2b392ace84412ecb54975b309cc197b98bc434686909ea900b3dddb1706`: 1 delete, 15 unchanged. Both checkers ok |
| Report | `0a6d1ea33b84aaa7f8ba9748d515b0d0c9faceb7e8fd8c19bd418a9a3395a35c` |

## Execution (2026-11-18, UTC)

| Time | Step | Result |
| --- | --- | --- |
| 01:32:21 | Baseline capture, revoked phase | PASS. The runner token mint was denied before any grant existed |
| 01:33:58 | Runner access plan | Checked as above |
| ~01:35:38 | Apply; state serial 58 | Backup created 01:35:43 |
| 01:36:20 | Window capture | **7/8: R7 FAIL.** The token mint was denied about 52 s after the apply. Diagnosed as IAM propagation: the binding, principal and condition were already exact (R1 PASS). Nothing was changed |
| 01:40:09, 01:43:11 | Window captures | Token mint allowed. 01:43:11 re-evaluated offline: **8/8 PASS** |
| 02:03:41–02:03:56 | Script job | Run by the runner: `DONE`, no errors, 10 child jobs |
| 02:06:18 | Runner revoke plan | Checked as above. Applied; state serial 60, backup created 02:07:23 |
| 02:09:55 | Final revoked capture | **REVOKED 8/8 PASS.** The token mint was denied (`IAM_PERMISSION_DENIED` on `iam.serviceAccounts.getAccessToken`) |

**Jobs.** The 5 dry runs: the four SELECTs read only their expected inputs, processing 139,900–200,546 bytes each; the script dry run processed 718,146 bytes. Then 1 parent job and 10 children. Total 16 of the 20 allowed. Children, in order:

| Child | Bytes billed | Rows affected |
| --- | --- | --- |
| `BEGIN_TRANSACTION` | 0 | |
| 4 × `DELETE` | 0 each | 0 deleted (first build) |
| `INSERT` transaction_daily | 20 MiB | 1,312 inserted |
| `INSERT` customer_daily | 30 MiB | 3,412 inserted |
| `INSERT` account_daily | 30 MiB | 3,428 inserted |
| `INSERT` operations_daily | 20 MiB | 3,762 inserted |
| `COMMIT_TRANSACTION` | 0 | |

All children ran as the runner. No child exceeded the 100 MiB per-job cap, and none was a cache hit. The marts held 0 rows before the run.

**Input integrity.** Before and after M8, `customers` (100 rows / 13,100 bytes), `accounts` (150 / 21,750) and `curated_transactions` (4,000 / 982,049) were identical in `numRows`, `numBytes`, `lastModifiedTime` and schema, and equal to the M7D read-back. `lastModifiedTime` (2026-11-17T10:24:28Z) dates from the M7D loader-revoke table-IAM change, not from M8.

**Runner permissions.** R2–R5 passed in the captures re-evaluated offline (01:32 baseline, 01:36, 01:43) and in the operator's final 02:09 REVOKED result: the project policy (Owner plus runner jobs), the seven table policies, the five runtime roles, and the runner's keyless status were all unchanged. The loader and provisioner stayed revoked (R6).

## State backups

| Serial | When | Ciphertext SHA-256 | State SHA-256 | Verification |
| --- | --- | --- | --- | --- |
| 58 (runner window open) | 01:35:43Z | `8e221231d4bd8312329643a58f10657b442d8be59a2f36a1a8390e9a465db871` | `53aebfb51a841c23…e831` | OneDrive download SHA-256 verified by the operator in Terminal.app |
| **60** (revoked) | 02:07:23Z | `caec0c6ab381829dd6e117560800de3681f67cfaae744f93ae7c314ca13dd018` | `fda71a48d82aa721cb95a373f56ded21948ce116cd3136e74427eaacd3478431` | Same. The current bootstrap state's SHA-256 equals this serial |

The backup ledger records both as `created` but has no `verified` event for either, because `state_backup.py verify-download` was not used. Verification rests on the operator's independent check (see gaps).

## Billing

| Basis | Bytes billed | Cost |
| --- | --- | --- |
| Actual job statistics (parent = sum of children) | **104,857,600 (100 MiB)** | About US$0.00095 at the repository's illustrative US$10/TiB; well under 1p against the £5 alert budget |
| Pre-run conservative estimate | 140 MiB | |
| Dry runs, `tabledata.list`, `tables.get` | 0 | Not billed |

Actual billing was lower than the estimate because `DELETE` on empty tables billed 0. The INSERTs matched the 10 MiB-per-referenced-table minimum, including the destination table.

**Limitations:** these are BigQuery job statistics, not an invoice. Charges after free tier, the London rate, tax and currency conversion are not evidenced. See gap 1.

## Evidence gaps

1. **Actual charges.** No Billing Console or billing-export evidence is in custody. Review month-to-date spend after the 24–48 h billing delay.
2. **Audit mapping.** No Logs Explorer record of `GenerateAccessToken` (operator → runner) for this window is in custody.
3. **Backup ledger.** Serials 58 and 60 have no `verified` ledger event. Verification is operator-attested only. Running `state_backup.py verify-download` on both would record it in the ledger.
4. **Carried over from [M7D](m7d_closure.md#limitations)**, all still open:
   - A10 `pilot-loaded` blocked;
   - L5 probes inconclusive;
   - **no bucket or dataset policy read-back**. The M8 captures cover the project, the service accounts, the seven tables and the roles only.

## Residual state

- The runner is dormant, with its unchanged grants. Bootstrap state holds 15 addresses at serial 60. `bootstrap-dev-runner.tfvars` has been removed from custody inputs.
- The marts and inputs remain populated until Gate D. A re-run needs a new bundle (`--attempt 2`) and a new runner window.
