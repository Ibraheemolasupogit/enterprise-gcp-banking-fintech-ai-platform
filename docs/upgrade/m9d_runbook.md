# M9D runbook: score the pilot packet in BigQuery (spec v2)

Prepared offline on 2026-11-18. **Phase 1 only: nothing in this runbook has been executed.** The operator runs every step by hand in Terminal.app, never in Claude Code. M9D:
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
  --packet-dir "$PKT" --expected-manifest-sha256 "$A2" < "${Q%.tfplan}.json"
#   expect ok: creates 2, unchanged 18, accepted_load_drift = 7 tables  (this IS the A10 record)
tfrun pilot-dev apply "$Q"                                                      # after G2
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
3. **A10 or `pilot-scores` failure:** do not apply the pilot plan. Revoke the provisioner and record the problem. A drift on a mart or input that is not the exact expected load means the data changed: stop and investigate.
4. **Job ID already exists (HTTP 409):** inspect with `job` and never resubmit. A deliberate re-run needs a new bundle (`--attempt 2`) and a new approval.
5. **`jobs.list` (children) denied:** record it as a limitation and never add a grant.
6. **Parity mismatch** (any row or column): stop. Do not overwrite or re-run until reviewed. The score table can be left in place; it is deletion-protected and holds synthetic scores only.
7. **Partial apply failure:** follow M6T/M7D §6.8.1. Read back live, then use the strict revoke modes. Never re-apply the failed plan.

## 7. Remaining gaps

- **GoogleSQL is first executed in M9D.** `EXTRACT … AT TIME ZONE`, `DIV`, `LEAST` with NULL and `CAST(STRING AS FLOAT64)` are proven only by the dry runs and the parity read-back. Parity covers the stored INT64 outputs and codes; cloud logits and probabilities are not stored.
- **Read-back NULLs:** NULL in an INT64 score field fails closed. Only the NULLable STRING reason codes are accepted as NULL.
- **Carried-over items:** bucket and dataset policy read-back, billing evidence, and the audit-log mapping (M7D/M8) remain open. M9D adds them to the closure checklist but does not perform them.
- **Approvals:** production approval is pending, and the G0–G7 approvals are needed in order.
