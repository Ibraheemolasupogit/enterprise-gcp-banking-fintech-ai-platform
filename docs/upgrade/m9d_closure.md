# M9D closure: governed fraud scoring of the pilot packet in BigQuery

Date 2026-11-18. Code at `0e470f2` (CI run 44337434322 successful). Project `ebank-pilot-dev-261031-k7m4`, region `europe-west2`. Procedure: [M9D runbook](m9d_runbook.md).

**Status: G7 approved (2026-11-18). The controlled synthetic-data M9D pilot is complete, with documented evidence gaps carried forward ([G7 decisions](#g7-decisions-approved-2026-11-18)). Production approval remains pending.**
- The runner scored the 4,000-row M7B pilot packet in BigQuery with model spec v2 (`38180198803761ee…39e0`).
- Python/BigQuery parity is **exact**: 0 mismatches in every column; 198 alerts (4.95%).
- The temporary windows were opened, verified, used and revoked: the provisioner window for the score table and A10, and the runner window for scoring. Final REVOKED capture: 8/8 PASS.
- The overdue **M7D A10 is closed** by G2″ ([runbook §2c](m9d_runbook.md#2c-g2-deployed-g3-revoked-a10-closed-2026-11-18)).
- **Approval scope.** DR-M9B (`configs/approvals/DR-M9B-m9a-eval-1.json`) covers only offline development and this controlled synthetic-data pilot. **Production approval is still pending, and this closure does not claim it.**

The operator ran every cloud and Terraform step in Terminal.app. The assistant re-ran the offline verifiers on custody evidence, read-only. It made no GCP call, ran no Terraform command, and changed no evidence, state, plan or backup. The operator principal and bucket name are omitted.

## Outcome

| Item | Result |
| --- | --- |
| Rows scored | 4,000 (packet `m7p-a89332976c69d583137f9323`, manifest `7e2fb8b7…b540f`) |
| Alerts | 198 (4.95%; reference 5.00%) at `score_ppm >= 463905` |
| Parity (cloud read-back vs `score_python`, every row) | 0 mismatches in each of `transaction_id`, `model_spec_sha256`, `input_valid`, `score_ppm`, `alert` and `reason_code_1`–`3` |
| Expected-score file | `5f5b284506b23f432631ba58db9878a0f7843cd578736031c6918513c5aa9b32` (inside the approved bundle) |
| Score table after the job | 4,000 rows, 414,970 logical bytes; deletion-protected |
| Inputs and marts | Unchanged before and after (rows, bytes, `lastModifiedTime`, schema): `post` stage ok |
| Monitoring (against the M9C reference) | Score PSI 0.0052 (stable, cloud scores); all 22 features stable (largest `acct_history_count_30d` 0.089, from the packet's Python features, equal to the cloud features by the M9C parity contract) |
| Labels | None in any cloud artifact |

## Approvals and hashes

| Gate | Pinned evidence | SHA-256 | Offline re-check at `0e470f2` |
| --- | --- | --- | --- |
| G0–G3″ | See [runbook §2a–§2c](m9d_runbook.md#2a-g2-no-go-2026-11-18-and-recovery): two NO-GO pilot plans (never applied), then G2″ applied (A10) and G3″ revoked | n/a | n/a |
| G0‴ | Commit `0e470f2` (score-table policy added to R3), CI green | n/a | n/a |
| **G4** | `plans/bootstrap/20261118T174013Z-runner-access-0e470f2.tfplan` | `0d19629142b756ff163b9346034ad81c76a39abaf5a008d9fdf716bf5a6da1be` | `runner-access` ok (1 create, 15 unchanged; window 2.98 h; etag-only drift on the 2 tolerated addresses). Independent verifier ok (2.979 h) |
| | `….json` | `fe284c6121f1ca69744cb9a4c998f6b67cceddfe4786a30702ca33930a5972dc` | |
| **G5** | Bundle `m9d/m9d-a89332976c69d583137f9323-a1` (`SHA256SUMS`) | `9446aeb3724588d189a0cccc59e50d42faf668f01906e8f9d41a36baf60dd525` | All bundle files match `SHA256SUMS` |
| | `evidence/report.json` | `ed575c23901a0b6bf1aa95ab09cff71f915ee72efd6cfbbae93203371d074668` | `verify-scoring-evidence --stage all` re-run offline: ok, no problems, **byte-identical** to `report.json` |
| **G6** | `plans/bootstrap/20261118T180030Z-runner-revoke-0e470f2.tfplan` | `9ed5b71d121adec51fa8913e6f4b6a718c5e2e0408322d632a386c9fd09b2f43` | `runner-revoke` ok (1 delete, 15 unchanged). Independent verifier ok |
| | `….json` | `c41071872e5f56dd7fccfdbc6c5097316c4fe610a9935a94b12146934668f427` | |
| **G7** | This record, over `report.json` above | `ed575c23901a0b6bf1aa95ab09cff71f915ee72efd6cfbbae93203371d074668` | **Approved** 2026-11-18 |

## Execution (2026-11-18, UTC)

| Time | Step | Evidence |
| --- | --- | --- |
| 15:45:31 | Fresh baseline capture (8 table policies), REVOKED 8/8 PASS | `m8/runner-revoked-20261118T154531Z` |
| 17:40:16 | G4 runner-access plan; expiry 20:39:00Z | G4 above |
| 17:44:01 | Bootstrap backup serial 80 (window open) | Backups below |
| 17:50:03 | Window capture: R1–R8 PASS, mint allowed, R3 with the score table, 16 state addresses | `m8/runner-window-20261118T175003Z` (re-verified offline: ok) |
| 17:53:00 | Bundle window start (bundle window ends 20:39:00Z; refusal from 20:24:00Z) | `bundle.json` |
| 17:57:35–17:57:44 | Scoring script job `m9d_scores_a89332976c69d583137f9323_38180198803761ee_a1` run by the runner: `DONE`, no error, 4 children | `evidence/jobs/` |
| 18:00:32 | G6 runner-revoke plan | G6 above |
| 18:02:15 | Bootstrap backup serial 82 (revoked) | Backups below |
| 18:04:10, 18:04:52 | REVOKED captures, both 8/8 PASS (mint denied, 15 addresses). The record is **18:04:52** | `m8/runner-revoked-20261118T180452Z` (both re-verified offline: ok) |

The window was revoked 2 h 34 min before its expiry. The runner window file was removed from custody inputs, which now hold only `bootstrap-dev.tfvars` and `pilot-dev.tfvars`.

**Before the job:**
- **Preflight query:** 4,000 rows, 0 duplicate IDs, 0 invalid rows, 0 sub-second timestamps; the score table was empty.
- **Dry runs:** the SELECT read only `curated_transactions`, and the refresh was a SCRIPT. Each processed 542,049 bytes, under the cap.

**Child jobs, in order:**
- `BEGIN_TRANSACTION`, 0 bytes billed;
- `DELETE`, 0 rows affected, 0 billed;
- `INSERT`, 4,000 rows affected, 20,971,520 billed;
- `COMMIT_TRANSACTION`, 0 billed.

All four finished `DONE` without errors. `jobs.list` was allowed, so stop rule 5 did not apply.

## Security controls evidenced

| Control | Evidence |
| --- | --- |
| No new IAM permission, role or API | The score-table grant uses the existing `bankpilot_dev_tables` role. The plans were checked strictly (G2″, G4, G6) |
| Time-bound, single-purpose windows that never overlap | Provisioner G1″ → G3″ (revoked 15:14Z) before runner G4 (17:40Z) → G6 (18:00Z). Both window expiries were 3 h or less (2.98 h each) |
| Two independent plan checks | `bootstrap_plan_check.py` and `runner_window_verify.py` (or the G2″ independent verification) agreed at every gate |
| Live IAM read-back | Window and REVOKED captures, R1–R8. R3 now proves the score-table policy is exactly the runner's `tables` binding |
| Runner dormant afterwards | R7: token minting `PERMISSION_DENIED`. R1: runner account policy empty. R5: keyless |
| Cost controls | `maximumBytesBilled = 104857600` on the preflight and the script. 8 jobs of the 20 allowed |
| Approved bundle only | Every `m9d.sh` step checked the bundle SHA-256, the model spec SHA-256 and the packet bytes. Evidence was written once, never overwritten |
| No secrets in evidence | The token was never on argv or disk. The operator and bucket are never printed |

## State backups

| Root / serial | Created | Ciphertext SHA-256 | State SHA-256 |
| --- | --- | --- | --- |
| bootstrap 75 (G1″ open) | 15:03:43Z | `46c23d29a2a2518dc5bfd4acfcc6e2068bd28c32fe6ba659d78611364893e80e` | `4a23adc7fb2b86e5…4bbb` |
| pilot-dev 24 (after G2″) | 15:08:48Z | `ab803db5b22826ce06b7424f49a31b585673e2ca52d624500c213cbac7e57ab6` | `47a51fb56b397aaa…4957` |
| bootstrap 78 (G3″ revoked) | 15:14:32Z | `237dee23d17afd343a9c19e6e601c47c8272d05e0af76ab40c471254773e8012` | `dfb9c9376bb2e192…4372` |
| bootstrap 80 (runner window open) | 17:44:01Z | `3ba95c998f048d2d8905bd87bb251c9ce6d13b136c5e08a41d774dfd11b683b2` | `f8d0832797d833bb…8e34` |
| **bootstrap 82** (runner revoked) | 18:02:15Z | `991b03e2ca0a587b3a56518664f5ba4074c83fa2910726372b51620418372204` | `e0180acf808428de…6ead` |

The operator uploaded all five to OneDrive and confirmed each downloaded copy was byte-identical in Terminal.app. As in M8, the ledger records them as `created` only, with no `verified` event, because `state_backup.py verify-download` was not used.

## Billing

| Item | Bytes billed | Notes |
| --- | --- | --- |
| Preflight query | 10,485,760 (10 MiB) | The 10 MiB minimum |
| Scoring script (parent = sum of children) | 20,971,520 (20 MiB) | `INSERT` only. `DELETE` on the empty table billed 0 |
| Dry runs, `tables.get`, `tabledata.list`, `jobs.get`/`jobs.list` | 0 | Not billed |
| **Total** | **31,457,280 (30 MiB)** | Equal to the pre-run estimate (upper bound 300 MiB). About US$0.0003 at the repository's illustrative US$10/TiB |

These are BigQuery job statistics, not an invoice. The £5 budget only alerts.

## Evidence gaps and carried-forward items

| # | Item | Origin | Status after M9D | Needed to close |
| --- | --- | --- | --- | --- |
| 1 | Actual charges (Billing Console or export) | M8 gap 1 | **Carried forward** (owner: Ibraheem Adeleke). M9D adds 30 MiB of job statistics only | Review month-to-date spend after the 24–48 h billing delay, and record it |
| 2 | Audit mapping (`GenerateAccessToken` in Logs Explorer) | M7D §6.6, M8 gap 2 | **Carried forward** (owner: Ibraheem Adeleke), now covering the M9D windows too: provisioner G1/G1′/G1″, runner G4 | Record the Logs Explorer result per window |
| 3 | Backup ledger has no `verified` events (serials 58/60 and 75/78/80/82, pilot-dev 24) | M8 gap 3 | **Downloads passed** a byte-for-byte comparison with the originals (operator, Terminal.app). **`verify-download` ledger entries remain absent** | Optional: run `state_backup.py verify-download` to record it |
| 4 | Bucket and dataset policy read-back | M7D §6.9, M8 gap 4 | **Partly addressed.** The G2″ plan refresh (15:06:11Z) read the live bucket IAM policy, dataset IAM policy, bucket and dataset. All four plan no change, with no drift, and the checker confirmed each policy is exactly the approved binding (bucket: runner `objects` only; dataset: approved owner + runner `metadata`). This is Terraform-refresh evidence under the provisioner, taken before the runner window; it is not an independent operator capture. Scoring cannot change IAM, and R3 covers the tables. **Accepted at G7 as supporting evidence, not as an independent read-back** | An independent read-only operator capture is still not on record |
| 5 | L5 probes inconclusive | M7D | **Optional** (G7) | Re-probe read-only; the loader is revoked, so it would only show denial |
| 6 | A10 `pilot-loaded` blocked | M7D | **Closed** by G2″ (`pilot-scores`) | None |
| 7 | Custody hygiene: leftover window or loader files | M7D/M8 | **Closed.** Inputs hold only the two base files | None |

## Residual state

- **Runner:** dormant, with its unchanged grants. Bootstrap state holds 15 addresses at serial 82.
- **Provisioner and loader:** revoked and keyless. The loader identity remains, dormant.
- **Data:**
  - `transaction_risk_scores` holds 4,000 synthetic score rows (`prevent_destroy`, deletion-protected). Inputs and marts are unchanged. All remain until Gate D.
  - A re-score needs a new bundle (`--attempt 2`), a new runner window and new approvals; job ID `…_a1` must never be resubmitted.
- **Evidence:** the bundle, failure evidence and every plan stay in custody and must never be edited or deleted. `outputs/runs/m9c-export-3` (git-ignored) must stay unchanged; the bundle verifies its hashes.

## G7 decisions (approved 2026-11-18)

1. **The controlled synthetic-data M9D pilot is complete.** G7 approves `report.json` SHA-256 `ed575c23901a0b6bf1aa95ab09cff71f915ee72efd6cfbbae93203371d074668`, the bundle `9446aeb3724588d189a0cccc59e50d42faf668f01906e8f9d41a36baf60dd525`, and this record.
2. **Production approval remains pending.** DR-M9B covers offline development and this pilot only. Nothing here approves production use.
3. **Carried forward, owner Ibraheem Adeleke:** billing evidence (gap 1) and the `GenerateAccessToken` audit-log mapping (gap 2).
4. **The G2″ Terraform refresh is accepted as supporting bucket and dataset IAM evidence**, not as an independent read-back (gap 4).
5. **The OneDrive backup downloads passed a byte-for-byte comparison.** `verify-download` ledger entries remain absent (gap 3).
6. **Optional:** the L5 probes (gap 5) and these future enhancements:
   - running `verify-download` so the ledger records the verifications;
   - storing cloud logits and probabilities for full numeric parity;
   - scheduled scoring with drift alerting;
   - pinning the mart modification time in the checker to the M8 evidence;
   - clearing the existing mypy issues in the plan checker.
