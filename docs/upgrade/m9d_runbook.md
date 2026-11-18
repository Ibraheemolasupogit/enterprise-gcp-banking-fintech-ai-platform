# M9D runbook: score the pilot packet in BigQuery (spec v2)

Prepared offline on 2026-11-18. The operator runs every step by hand in Terminal.app, never in Claude Code.

**Status (2026-11-18):** G0 passed at `dd33113`, and G1 was applied. **G2 was a NO-GO, and the pilot plan was not applied.** G3 revoked the provisioner (8/8 PASS). Recovery runs through G0′ → G1′ → G2′ → G3′ (§2a). M9D:
1. creates the score table;
2. completes the overdue M7D A10 check, inside **one provisioner window**;
3. scores the 4,000-row M7B pilot packet with model spec v2 (`38180198…39e0`), as the runner, inside **one runner window**;
4. proves exact Python/BigQuery parity and records monitoring evidence.

Approval scope (`configs/approvals/DR-M9B-m9a-eval-1.json`): offline development and the **controlled synthetic-data pilot only**. Production approval is pending.

## 1. What Phase 1 adds (offline)

| Piece | Where |
| --- | --- |
| Score table in the pilot root (deletion-protected, `prevent_destroy`, M9C schema). Runner access through the module's existing `bankpilot_dev_tables` grant (`get`, `getData`, `updateData`) | `infrastructure/terraform/main.tf` (`local.score_contract` merged into `local.schemas`); `sql/scoring/v1/transaction_risk_scores.schema.json` |
| Plan-checker mode `pilot-scores` (A10 plus score table) | `scripts/bootstrap_plan_check.py` |
| Runner scoring bundle and evidence verifier | `deployment/scoring_bundle.py`; CLI `render-scoring-bundle`, `verify-scoring-evidence` |
| Tests | `tests/test_pilot_scores_plan_check.py` (22), `tests/test_scoring_bundle.py` (35) |
| Fix found during preparation | `monitoring/reference_profile.compare` paired categorical proportions by position. After a JSON round trip (sorted keys), `__other__` moved ahead of lowercase categories. It now pairs by category, with a regression test. No M9C artifact used `compare` |

**No new IAM permission, custom role or API.** The provisioner already holds the table-creation and table-IAM permissions (`provisioner.role.json`), and the runner's grant uses an existing role. The existing windows are reused unchanged: the M6S provisioner window and the M8 runner window.

**Why `pilot-scores` replaces `pilot-loaded` for A10.** `pilot-loaded` (M7C2) required the four marts to be empty. M8 legitimately populated them, so that mode can no longer pass; a test asserts this. `pilot-scores` keeps every A10 assertion:
- the 18 resources are unchanged;
- each input table holds exactly the packet's rows and logical bytes;
- the only accepted drift is exact loads (inputs to the packet, marts to the M8-verified 1,312 / 3,412 / 3,428 / 3,762 rows and their logical bytes), with `etag` and `last_modified_time` moving forward together.

It also requires exactly two creates:
- the score table, with its ID, deletion protection and the exact M9C schema (names, types, modes, order);
- the runner's unconditional `bankpilot_dev_tables` member.

Everything else is rejected, including the provisioner as member, a reader role, a condition, an update or delete, and a mart or input size drift.

**Corrected after the G2 NO-GO (§2a).** The original rules required every loaded table's etag to change, and accepted no other drift. Live BigQuery showed three legitimate refresh drifts that those rules did not model. `pilot-scores` now also accepts the following, **in this mode only and nothing near them**:
- **Marts written by M8 DML:**
  - `num_rows`, `num_bytes` and `last_modified_time` change, and the **etag stays identical**. BigQuery does not change a table's etag for DML; the etag in state from creation equals M8's live read-back etag.
  - Rows and bytes start at 0 and end at exactly the M8 values.
  - The time moves forward, is not after the plan, and is the same for all four marts (one M8 transaction).
  - The input tables still require an etag change, because they were loaded with load jobs.
- **Dataset access reconciliation** (`google_bigquery_dataset.foundation`):
  - Exactly `access`, `etag` and `last_modified_time` change.
  - The recorded access is exactly BigQuery's creation default: the provisioner as OWNER, plus `projectOwners`, `projectReaders` and `projectWriters`.
  - The live access is exactly two entries: the approved owner as OWNER, and the runner with `bankpilot_dev_metadata`.
  - Only plain user or special-group entries are allowed. A view, routine, dataset, domain, group, IAM member, condition, extra key or duplicate is rejected.
  - The dataset IAM policy resource must plan no change.
- **Etag-only drift on the runner's grants on the three input tables:**
  - Only `etag` changes, from one non-empty value to another.
  - Both sides are the approved unconditional `bankpilot_dev_readers` grant to the runner.
  - The cause: M7D's loader grants on these tables (added, then removed) rewrote their IAM policies.
  - The same drift on a mart grant or the score-table grant is rejected.

The checker reports these as `accepted_load_drift` (7), `accepted_access_drift` (1) and `accepted_etag_drift` (3). Drift that is already recorded in state needs no acceptance. Tests: `tests/test_pilot_scores_g2_drift.py` runs 52 tests against the real G2 drift section, with the owner replaced by a placeholder (`tests/fixtures/m9d_g2_resource_drift.redacted.json`).

## 2. Approval gates (each recorded separately, before its step)

| Gate | Approves | Pinned value |
| --- | --- | --- |
| **G0** | Code: M9C and M9D committed, CI green, clean tree | Commit SHA |
| **G1** | Provisioner window: bootstrap `access` plan (2 creates, 15 unchanged, expiry ≤ 3 h) | Plan SHA-256 |
| **G2** | Pilot plan: `pilot-scores` ok (**A10 result** plus score table and runner grant) | Plan SHA-256 |
| **G3** | Provisioner revocation: bootstrap `revoke` plan (2 deletes); may be pre-approved with G1 | Plan SHA-256 |
| **G4** | Runner window: bootstrap `runner-access` plan (1 create, 15 unchanged), both checkers ok | Plan SHA-256 |
| **G5** | Scoring bundle, with the window inside G4's expiry | Bundle SHA-256 |
| **G6** | Runner revocation: bootstrap `runner-revoke` plan; may be pre-approved with G4 | Plan SHA-256 |
| **G7** | Closure: `report.json`, REVOKED 8/8, backups verified | Report SHA-256 |

Rules:
- The windows never overlap. The provisioner window is fully revoked (G3, read back) before G4.
- An approval covers exactly its pinned value. A changed plan, bundle or window needs a new approval.

## 2a. G2 NO-GO (2026-11-18) and recovery

### What happened

| Step | Result |
|---|---|
| G0 | Passed at `dd331133d3c227455e0ff45141dd4f6045f633c0` |
| G1 | Provisioner access applied; bootstrap state backup serial 63 downloaded and SHA-256 verified |
| G2 | Pilot plan `20261118T103726Z-pilot-scores-dd33113` (plan time 2026-11-18T10:37:27Z): **NO-GO, not applied**. 2 creates, 18 unchanged and 0 deletes as expected, and the 3 input loads were accepted, but the checker reported **8 drift problems** |
| G3 | Provisioner revoke applied; read back 8/8 PASS; bootstrap backup serial 66 verified |

### Preserved failure evidence (custody `plans/pilot-dev/`; never apply, edit or delete)

| File | SHA-256 |
|---|---|
| `20261118T103726Z-pilot-scores-dd33113.tfplan` | `9b41641ee08c57d7eaa919bae77d837b7b85d77f20a4ccc525f934587c780260` |
| `20261118T103726Z-pilot-scores-dd33113.json` | `85af86eeccaf62e51abadcb890bcf4a987460ab3f05e6844d5e60ee9e7e0f4e4` |

The plan is stale, and the window it ran under is revoked. Under the original checker (at `dd33113`), the JSON still fails with the same 8 problems. Under the corrected checker it passes. That pass **does not approve it**: G2′ needs a new plan.

### The eight findings (read-only diagnosis; principals checked as matches only, never printed)

| # | Address | What changed | Cause | Risk |
|---|---|---|---|---|
| 1 | `google_bigquery_dataset.foundation` | `access`, `etag`, `last_modified_time` (last modified 2026-11-10T13:22:29Z) | State still records the creation default access from 2026-11-10T11:51:39Z: the provisioner as OWNER plus three project groups. The M6T authoritative dataset policy (pilot-recovery apply) replaced it live with exactly the approved owner and the runner (metadata role). The policy resource plans no change, so live access equals the configuration. Not seen earlier because the 2026-11-17 A10 plan stopped with a 403 | None. Live access is **narrower** than state |
| 2–5 | `products["account_daily"]`, `["customer_daily"]`, `["operations_daily"]`, `["transaction_daily"]` | `num_rows`, `num_bytes`, `last_modified_time`; **etag unchanged** | M8's transactional DML script; all four last modified at 2026-11-18T02:03:56.723Z, the end of the M8 script job. Rows, bytes, time and etag equal the M8 read-back evidence exactly. The original rule wrongly required an etag change, and its test fixture had assumed one | None. Legitimate M8 data |
| 6–8 | `google_bigquery_table_iam_member.runner["accounts"]`, `["curated_transactions"]`, `["customers"]` | `etag` only | The M7D loader grants on exactly these three tables were added and then removed, rewriting each table's IAM policy. Member, role (`bankpilot_dev_readers`), condition (none) and table are identical | None. Etag only |

No finding is a configuration risk or a widening of IAM. The NO-GO was correct: the checker did not model these changes, so the plan was unverified.

### Recovery sequence (each gate recorded separately; nothing carries over from G1–G3)

| Gate | Approves | Pinned value |
|---|---|---|
| **G0′** | Checker correction committed (`scripts/bootstrap_plan_check.py`, the two test files, the fixture, this runbook); CI green; clean tree. The saved G2 files are unchanged (hashes above), and the corrected checker reports `ok` on the saved JSON | New commit SHA |
| **G1′** | A **new** provisioner window: bootstrap `access` plan (2 creates, 15 unchanged), `window_hours` ≤ 3.00, `access_expiry` equal to the new `EXP` (§3 step 1) | New plan SHA-256 |
| **G2′** | A **new** pilot plan, made after G1′ propagates, with both the checker and the independent verification passing (below). **This is the A10 record** | New plan SHA-256 |
| **G3′** | Provisioner revoke: strict `revoke` plan (2 deletes, 15 unchanged), applied, backed up and read back 8/8 PASS before G4 | New plan SHA-256 |

G4–G7 then follow unchanged.

G2′ requirements:
- It needs a **new SHA-pinned plan and fresh approval**. The failed G2 plan, its approval and its window are never reused.
- The checker result alone is not enough: the independent verification must agree with it.
- Re-check the SHA-256 immediately before apply. A mismatch, or any re-plan, needs a new G2′ approval.

## 3. Commands (Terminal.app; `C="$HOME/.secure/ebank-pilot-dev-261031-k7m4"`)

Token forms are exactly as in M6P/M6S: the operator token is fetched inside `env -i … /bin/sh -c` for `plan` and `apply` only, and never appears in argv.

```sh
C="$HOME/.secure/ebank-pilot-dev-261031-k7m4"; cd ~/Developer/enterprise-gcp-banking-fintech-ai-platform
PKT="$PWD/outputs/packets/m7p-a89332976c69d583137f9323"
A2=7e2fb8b7500a4aaf0652d2f1381359d333011a5f2ff5f92b1b684be0142b540f
tfrun() {  # tfrun ROOT ARGS... (plan/apply with the operator token inside the child shell)
  env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" /bin/sh -c '
    GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)" || exit 1
    export GOOGLE_OAUTH_ACCESS_TOKEN; root="$1"; shift
    exec python3 scripts/terraform_custody.py run --root "$root" -- "$@"' sh "$@"; }
show() { env -i HOME="$HOME" PATH="$PATH" python3 scripts/terraform_custody.py run --root "$1" -- show -json "$2"; }

# ---- 0. Baseline (read-only): runner and loader revoked, no window open
bash scripts/runner_window_collect.sh revoked
python3 -I scripts/runner_window_verify.py iam --phase revoked --capture "<dir>" \
  --bootstrap-tfvars "$C/inputs/bootstrap-dev.tfvars"                            # 8/8 PASS

# ---- 1. Provisioner window (G1), the M6S form
EXP="$(date -u -v+3H +%Y-%m-%dT%H:%M:00Z)"; umask 077
printf 'provisioner_access_enabled = true\naccess_expiry              = "%s"\n' "$EXP" \
  > "$C/inputs/bootstrap-dev-window.tfvars"
P="$C/plans/bootstrap/$(date -u +%Y%m%dT%H%M%SZ)-provisioner-access-$(git rev-parse --short HEAD).tfplan"
tfrun bootstrap plan -input=false -var-file="$C/inputs/bootstrap-dev.tfvars" \
  -var-file="$C/inputs/bootstrap-dev-window.tfvars" -out="$P"
shasum -a 256 "$P"; show bootstrap "$P" > "${P%.tfplan}.json"
python3 scripts/bootstrap_plan_check.py --mode access < "${P%.tfplan}.json"      # 2 creates
tfrun bootstrap apply "$P"                                                      # after G1
python3 scripts/state_backup.py create --root bootstrap      # then OneDrive upload + verify-download
# wait about 5 minutes (IAM propagation)

# ---- 2. Pilot plan: A10 + score table (G2)
OWNER="$(sed -n 's/^dataset_owner_member *= *"\(.*\)"$/\1/p' "$C/inputs/pilot-dev.tfvars")"
Q="$C/plans/pilot-dev/$(date -u +%Y%m%dT%H%M%SZ)-pilot-scores-$(git rev-parse --short HEAD).tfplan"
tfrun pilot-dev plan -input=false -var-file="$C/inputs/pilot-dev.tfvars" -out="$Q"
shasum -a 256 "$Q"; show pilot-dev "$Q" > "${Q%.tfplan}.json"
python3 scripts/bootstrap_plan_check.py --mode pilot-scores --dataset-owner="$OWNER" \
  --packet-dir "$PKT" --expected-manifest-sha256 "$A2" < "${Q%.tfplan}.json"; echo "exit=$?"
#   expect exit 0, ok, problems [] (this IS the A10 record):
#     creates 2 (products["transaction_risk_scores"], runner["transaction_risk_scores"]),
#     unchanged 18, deletes 0, packet_id m7p-a89332976c69d583137f9323,
#     accepted_load_drift 7   (3 inputs + 4 marts),
#     accepted_access_drift 1 (google_bigquery_dataset.foundation),
#     accepted_etag_drift 3   (runner["accounts"|"curated_transactions"|"customers"])
#   Fewer drift entries are fine only if that drift is already recorded in state.
# Independent verification (addresses and actions only; no values printed); must agree:
python3 -I - "${Q%.tfplan}.json" <<'PY'
import collections, json, sys
p = json.load(open(sys.argv[1]))
rc = p.get("resource_changes") or []
print("errored:", p.get("errored"))
print("actions:", dict(collections.Counter("+".join(r["change"]["actions"]) for r in rc)))
for r in rc:
    if r["change"]["actions"] != ["no-op"]:
        print("CHANGE", r["change"]["actions"], r["address"])
for r in p.get("resource_drift") or []:
    print("DRIFT ", r["change"]["actions"], r["address"])
print("data sources:", sum(r.get("mode", "managed") != "managed" for r in rc))
print("output changes:", [k for k, v in (p.get("output_changes") or {}).items()
                          if v.get("actions") != ["no-op"]])
PY
#   expect: errored False; actions {'no-op': 18, 'create': 2}; exactly the 2 CHANGE creates;
#   at most 11 DRIFT ['update'] lines, only on the 7 tables, the dataset and the 3 input
#   runner members (any other address or action: stop); data sources 0; output changes [].
shasum -a 256 "$Q"                                   # the G2′ pinned value; re-check before apply
tfrun pilot-dev apply "$Q"                                                      # after G2′
python3 scripts/state_backup.py create --root pilot-dev      # then upload + verify-download
unset OWNER

# ---- 3. Revoke the provisioner (G3); base file only
R="$C/plans/bootstrap/$(date -u +%Y%m%dT%H%M%SZ)-provisioner-revoke-$(git rev-parse --short HEAD).tfplan"
tfrun bootstrap plan -input=false -var-file="$C/inputs/bootstrap-dev.tfvars" -out="$R"
shasum -a 256 "$R"; show bootstrap "$R" > "${R%.tfplan}.json"
python3 scripts/bootstrap_plan_check.py --mode revoke < "${R%.tfplan}.json"      # 2 deletes
tfrun bootstrap apply "$R"; python3 scripts/state_backup.py create --root bootstrap
rm "$C/inputs/bootstrap-dev-window.tfvars"
# about 5 minutes later: the provisioner account policy is empty (R6 in the next capture)

# ---- 4. Runner window (G4): docs/upgrade/m8_runbook.md §1.1 steps 1–3 exactly
#   (runner-access plan, both checkers, apply, backup, ≥5 min, window capture 8/8 PASS)

# ---- 5. Scoring bundle (G5), window inside the runner window's EXP
: "${EXP:?set EXP to the runner window expiry}"; mkdir -m 700 -p "$C/m9d"
PYTHONPATH=src python3 -m financial_crime_ml.cli render-scoring-bundle --packet-dir "$PKT" \
  --m9c-run "$PWD/outputs/runs/m9c-export-3" --expected-manifest-sha256 "$A2" \
  --window-start "$(date -u +%Y-%m-%dT%H:%M:00Z)" --window-expiry "$EXP" \
  --output-dir "$C/m9d/m9d-a89332976c69d583137f9323-a1"
export M9D_APPROVED_BUNDLE_SHA256=<approved bundle SHA-256>
B="$C/m9d/m9d-a89332976c69d583137f9323-a1"
bash "$B/m9d.sh" verify-bundle
bash "$B/m9d.sh" preflight          # 8 tables.get + 1 preflight query: 4000/0/0/0
bash "$B/m9d.sh" dryrun select; bash "$B/m9d.sh" dryrun refresh
bash "$B/m9d.sh" run                # the one billed scoring script
bash "$B/m9d.sh" job                # repeat until DONE
bash "$B/m9d.sh" children; bash "$B/m9d.sh" readback; bash "$B/m9d.sh" post
bash "$B/m9d.sh" verify-all         # evidence/report.json: exact parity + PSI

# ---- 6. Revoke the runner (G6): m8_runbook.md §1.1 steps 5–6; revoked capture 8/8 PASS
```

`m9c-export-3` is in the git-ignored `outputs/runs/`. Keep it unchanged; the bundle verifies every one of its hashes.

## 4. What the bundle proves

**Inputs and preconditions:**
- Model spec v2 is checked against the M9C manifest, the approval record and its own SHA-256 at every step. The GoogleSQL is regenerated from the spec and must equal the M9C artifacts byte for byte.
- Preflight: the inputs equal the packet, the marts equal M8, and the score table exists with the exact schema and 0 rows (or exactly 4,000 on a deliberate re-run). The preflight query must return 4,000 rows, 0 duplicate IDs, 0 invalid rows and 0 sub-second timestamps.

**Dry runs:**
- the SELECT reads only `curated_transactions`;
- the refresh is a SCRIPT;
- both process ≤ the cap.

**Execution:**
- One script job: the stable job ID `m9d_scores_a89332976c69d583137f9323_38180198803761ee_a1`, submitted by the runner, with the exact SQL, `maximumBytesBilled = 104857600`, and times inside the window.
- Exactly 4 children (BEGIN, DELETE, INSERT, COMMIT). INSERT affects 4,000 rows, DELETE affects the preflight row count, and each child is within the cap.

**Results:**
- **Read-back parity** against `score_python(spec v2, packet)`, comparing every row and column **exactly**: transaction ID, spec hash, validity, `score_ppm`, alert and the three reason codes. Expected: 4,000 rows, **198 alerts** (4.95%). Expected-score file SHA-256: `5f5b284506b23f432631ba58db9878a0f7843cd578736031c6918513c5aa9b32`.
- **Integrity:** inputs and marts are identical before and after (rows, bytes, `lastModifiedTime`, schema).
- **Monitoring:** score-ppm PSI and alert rate from the cloud scores against the M9C reference, plus feature PSI from the packet's Python features (equal to the cloud features by the M9C parity contract). Offline preview: score PSI 0.005 (stable), all 22 features stable (maximum 0.089), alert rate 4.95% against the 5.00% reference.

**Labels:** none in any cloud artifact. The packet has none, and a test scans the whole bundle; the spec only names the excluded columns.

## 5. Cost envelope and limits

| Item | Value |
| --- | --- |
| Jobs | 1 preflight query + 2 dry runs + 1 script parent + 4 children = **8 of 20** |
| Per-job cap | `maximumBytesBilled = 104857600` (100 MiB) on the preflight query and the script |
| Expected billed | About 30 MiB: preflight 10 MiB, INSERT about 20 MiB (source + destination), DELETE on an empty table 0 (M8 evidence) |
| Upper bound | 300 MiB (3 billable statements × cap), about US$0.003 at the illustrative US$10/TiB |
| Free | Dry runs, `tables.get`, `tabledata.list`, the Terraform-created table (empty) |
| Storage | About 0.2 MB of score rows |

The £5 budget only alerts; it is not a spending cap.

## 6. Stop rules

1. Any non-success response, checker or verifier problem: **stop**. Never retry with different settings, widen a role, raise a cap, edit a plan or bundle, or remove a condition.
2. Run the provisioner and runner windows strictly in sequence. If a window comes within 15 minutes of its expiry, stop work and revoke.
3. **A10 or `pilot-scores` failure:** do not apply the pilot plan. Revoke the provisioner and record the problem. A drift on a mart or input that is not the exact expected load means the data changed: stop and investigate. (This rule was applied at G2 on 2026-11-18; see §2a.)
   - Never extend the checker to make a failing plan pass. Any correction is an offline change, backed by evidence and paired with tests, and needs its own G0′ commit plus a new window and plan.
   - A disagreement between the checker and the independent verification is a failure.
4. **Job ID already exists (HTTP 409):** inspect with `job` and never resubmit. A deliberate re-run needs a new bundle (`--attempt 2`) and a new approval.
5. **`jobs.list` (children) denied:** record it as a limitation and never add a grant.
6. **Parity mismatch** (any row or column): stop. Do not overwrite or re-run until reviewed. The score table can be left in place; it is deletion-protected and holds synthetic scores only.
7. **Partial apply failure:** follow M6T/M7D §6.8.1. Read back live, then use the strict revoke modes. Never re-apply the failed plan.

## 7. Remaining gaps

- **GoogleSQL is first executed in M9D.** `EXTRACT … AT TIME ZONE`, `DIV`, `LEAST` with NULL and `CAST(STRING AS FLOAT64)` are proven only by the dry runs and the parity read-back. Parity covers the stored INT64 outputs and codes; cloud logits and probabilities are not stored.
- **Read-back NULLs:** NULL in an INT64 score field fails closed. Only the NULLable STRING reason codes are accepted as NULL.
- **Carried-over items:** bucket and dataset policy read-back, billing evidence, and the audit-log mapping (M7D/M8) remain open. M9D adds them to the closure checklist but does not perform them.
- **Approvals:** production approval is pending, and the G0–G7 approvals are needed in order.
