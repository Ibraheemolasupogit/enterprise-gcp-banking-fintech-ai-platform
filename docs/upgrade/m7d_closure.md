# M7D closure: synthetic input-table ingestion

Date 2026-11-17. Code `main` at `b8849f1`. Project `ebank-pilot-dev-261031-k7m4`, region `europe-west2`. Procedure: [M7D runbook](m7d_runbook.md).

**Status: closed with limitations.** The three input tables hold exactly the approved packet (4,250 rows, byte-identical on read-back), and all six loader grants are revoked and read back. **Under runbook §7, M7D does not claim full cloud success:**
- A10 (`pilot-loaded`) is **blocked**, not passed;
- the L5 permission probes are **inconclusive**;
- bucket and dataset policy drift after the load is **not verified**.

The operator ran every cloud step in Terminal.app. The assistant re-ran the offline checkers listed below on the custody evidence, read-only. It made no GCP call, ran no Terraform command, and changed no IAM, state, plan, backup or custody file. Bucket name and operator principal are deliberately omitted.

## Outcome

| Item | Result | Evidence |
| --- | --- | --- |
| Rows loaded and reconciled | **4,250**: customers 100, accounts 150, curated_transactions 4,000 | `report.json` `ok: true`; REVOKED check 4L |
| Loader grants | **All 6 revoked**; no loader member on the project, the loader account or any of the 7 table policies | REVOKED PASS checks 1–3b |
| Loader impersonation | **Denied** after revocation | Operator-reported mint attempt; loader account policy has 0 bindings (check 2) |
| Bootstrap state | **Serial 56**, exactly the 15 expected addresses, no grant in state | REVOKED check 6c; state SHA-256 equals the serial 56 backup |
| Final backup | Serial 56, encrypted, verified after the OneDrive round-trip | Backup ledger: `created` 10:25:23Z, `verified` 10:31:58Z |
| Pilot state | Untouched by M7D: serial 21, 18 resources | Read-only |
| Marts | Still empty (0 rows, 0 bytes) | REVOKED check 4L |

## Approvals (A1–A11)

| # | Pinned value |
| --- | --- |
| A1 | Commit `b8849f15f9f8dfd222490b6a1730584fdd8cce68` (CI status not re-checked here) |
| A2 | Packet `m7p-a89332976c69d583137f9323`, manifest `7e2fb8b7500a4aaf0652d2f1381359d333011a5f2ff5f92b1b684be0142b540f` (A2 PASS, re-run offline) |
| A3 | Loader identity, applied 2026-11-13: plan `6a6c0d91b6373edbf5ac5009121748d09789f302c41f561c3beb82ac1c7bf843` |
| A4 | Window 2026-11-17T10:05:57Z → **13:05:00Z** (2.98 h) |
| A5 | `20261117T100557Z-loader-access-b8849f1.tfplan` `6ae0c4ca9c1300714d7da2399b621e005b0e54b76ca77610c6eb179d1a19a181` (JSON `b5ca1624…bd8fe`). `--mode loader-access` ok; independent `w_access_verify.py` ok |
| A6 | Bundle `38bff7c6aa1ba858392aeccac592cd993951f15a3e03927cb4dc5051df921e0a` (all 6 bundle checksums verify) |
| A7 | 3 load jobs, 0 query jobs, 0 query bytes billed, 1,097,297 upload bytes |
| A8 | Prefix inventory not approved (not captured). Out-of-prefix probe: no evidence it ran |
| A9 | `20261117T102342Z-loader-revoke-b8849f1.tfplan` `1cbc93ef6464101b1ce725086ba95169590b2ecc4bd0760a983c03ca1602f9b2`. `--mode loader-revoke` ok: 6 deletes, 15 unchanged, base file only |
| A10 | **Blocked** ([below](#a10-blocked)) |
| A11 | `evidence/report.json` `48ce393415e0b827b4a97fa35fc2225464c085c5ae6bd584e660c4841744bc8d` |

An earlier loader-access plan (`20261117T094810Z…`, plan `97b50f4a…4cb1`, expiry 12:48:00Z) was **superseded and not applied**. Its expiry does not match the bundle window. It first exposed a defect in the independent A5 verifier: Terraform's `after_unknown` mirrors the `after` structure, so a fully known `condition` block appeared as a top-level key, and the verifier treated it as unknown. The verifier now inspects `true` leaves and allows only `id` and `etag`, with 40 regression tests. The deployed custody tools are `m7d/tools-a5fix/` (`SHA256SUMS` `9c70f313e8061aaa75982280bdbf86e33c8d1c9a931d9e4b2eac35f392b6d30d`).

## Loaded data (`report.json`)

| Table | Job ID | Object generation | Rows | Logical bytes | Rebuilt SHA-256 (= packet) |
| --- | --- | --- | --- | --- | --- |
| customers | `m7_load_a89332976c69d583137f9323_customers` | `1794910783711891` | 100 | 13,100 | `3c1927b521d1e45997e2633a2dd16eeb6836a2860f5f6d4d3a4a6f010fd9de66` |
| accounts | `m7_load_a89332976c69d583137f9323_accounts` | `1794910788248640` | 150 | 21,750 | `c0ed9882b2fb8bcff3077454a8aba54c61603ccde90385814867aae130871991` |
| curated_transactions | `m7_load_a89332976c69d583137f9323_curated_transactions` | `1794910793118466` | 4,000 | 982,049 | `5e142c1ddb207a7f2d5af47d571a4b865ee0604bee10b888d92033f94b582e5c` |

All jobs are `DONE` in `europe-west2` with no errors. Row identities, every value and the logical byte model match. Per-currency totals for `curated_transactions` (minor units):

| Currency | Rows | Amount |
| --- | --- | --- |
| EUR | 1,061 | 47,972,939 |
| GBP | 874 | 41,044,581 |
| JPY | 813 | 331,566 |
| USD | 1,252 | 54,122,799 |

The object checks (MD5, CRC32C, size) are transfer checks only. SHA-256 equivalence comes from the byte-identical rebuild.

## Gates

| Gate | Outcome |
| --- | --- |
| R1 | Correction committed as `b8849f1` |
| R2 / L0 | No evidence in custody; not claimed |
| R3 | **PASS** on the 2026-11-17T08:08:04Z baseline (re-run offline) |
| R4 | `loader-recovery` plan (2026-11-13, `64edd781…de1d`): ok, 0 creates, 15 unchanged, 0 deletes; the three expiring grants were absent from state |
| R5 | New window and new A5 plan; the failed incident plan was not re-applied |
| L1 | Live baseline: R3 PASS. Budget Console check: no evidence in custody |
| L2 | Loader identity (A3): no keys, exact role permissions (REVOKED checks 5a, 5b) |
| L3 | **WINDOW PASS** (2026-11-17T10:16:58Z capture, re-run offline): two expiring project grants, the expiring token-minting grant, and three **unconditional** input-table members; marts runner-only; tables empty; state 21 addresses |
| L4 | `verify-all` `ok: true`, `problems: []` |
| L5 | **Partial / inconclusive** (see limitations) |
| L6 | Optional; no evidence it ran |
| L7 | **REVOKED PASS** (2026-11-17T10:25:10Z capture, re-run offline: 12 checks) |

Audit mapping (§6.6, `GenerateAccessToken` by operator on the loader `uniqueId`): no evidence in custody, so this closure does not claim it. The M6T unmapped-identity limitation stays open for this window until the operator records the Logs Explorer result.

Backups (bootstrap; each verified after the OneDrive round-trip): serial 49 after the access apply (ciphertext `54c41f37…db88`) and **serial 56** after revocation (ciphertext `c608ffc39ce425e0212116f2c914e951bcf5d4d492a779c76ece0f68a70028f5`, state `277e29cafb6693c32bbf87172cfceb12a61ca38069444bcdb1ee780dfba675e7`).

## Limitations

### A10 blocked

The `pilot-loaded` plan (2026-11-17T10:33Z) failed with **HTTP 403 `iam.serviceAccounts.getAccessToken` denied**. The pilot-dev provider impersonates `bankpilot-dev-provisioner`. The operator can mint that token only inside an M6S provisioner access window, and that window has been closed by design since 2026-11-10 (`provisioner_access_enabled = false`; neither provisioner grant is in state or in the project policy). This is the revoked state working as intended, not a credential or provider fault, and it is unrelated to the loader revocation. The runbook omitted this precondition and now states it (§6.10).

- The failed run left `plans/pilot-dev/20261117T103311Z-pilot-loaded.tfplan` (`2c2019c123045fed68685c809cd4a58a4fded6ccb515b25a61afb3d894f0bc8f`). It is not a passed A10 plan and must never be applied.
- Partial substitute (read-only, not equivalent to A10): REVOKED check 4L shows the three input tables at exactly the `pilot-loaded` expectation (100/13,100; 150/21,750; 4,000/982,049) and the marts at 0, with the table policies runner-only.
- Completing A10 needs a separately approved M6S window, opened after this revocation, per runbook §6.10.

### L5 permission probes inconclusive

The four `testIamPermissions` probes (project and three tables) returned **HTTP 404** HTML pages, not API responses, so they say nothing about the loader's effective permissions. Bucket listing as the loader was **denied (HTTP 403, `storage.objects.list`)**, as expected. Prefix scope is therefore not evidenced by L5. It rests on the W1 condition read-back and on revocation.

### Bucket and dataset policy drift not verified

Runbook §6.9 requires the bucket and dataset policies to be unchanged from M6T. The REVOKED capture covers the project, the loader account and the seven table policies only. **No post-load bucket or dataset policy read-back exists, and this closure does not claim one.**

## Residual risks and follow-ups

1. **A10 outstanding:** needs its own approval and M6S window (runbook §6.10).
2. **Bucket and dataset policy read-back:** a read-only capture under the operator identity is still owed.
3. **L5 re-probe (optional):** fix the probe endpoints and re-run as a read-only check. The loader is now revoked, so that would only show denial.
4. **Audit mapping:** record the §6.6 Logs Explorer result.
5. **Custody hygiene:** `inputs/bootstrap-dev-loader.tfvars` (A4 window values, including the bucket) is still present, although runbook §6.8 step 3 removes it after revocation. The operator should delete it. Its window expired at 13:05:00Z, and it cannot grant anything without a new approved plan.
6. **Loader identity:** stays dormant, keyless and without grants (`loader_identity_enabled = true`).
7. **Data:** the loaded rows remain until Gate D. Marts are M8 scope.
