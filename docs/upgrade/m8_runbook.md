# M8 runbook: build the four analytics marts in the GCP pilot

Prepared offline on 2026-11-17 at `a34cce3`. **Nothing in this runbook has been executed.** Live execution needs the separately approved runner access window in §1. It builds `transaction_daily`, `customer_daily`, `account_daily` and `operations_daily` in `bankpilot_dev` from the three input tables that M7D loaded ([M7D closure](m7d_closure.md)). It implements the retained [C-marts design](../gcp_pilot_plan.md#c-marts-mart-processing-m8-prepared-offline-not-authorised). The operator runs every step by hand in Terminal.app, never in Claude Code.

## 1. Runner access window (prepared offline; needs its own approvals)

The design requires **the runner** (`bankpilot-dev-runner`) to execute the marts. It already holds exactly what it needs:
- `bankpilot_dev_jobs` on the project;
- `bankpilot_dev_readers` on the three input tables;
- `bankpilot_dev_tables` on the four marts;
- `bankpilot_dev_metadata` on the dataset.

Until now nobody could mint a runner token ([M6P](m6p_validation.md) P6).

**Mechanism (bootstrap root, same pattern as the M6S provisioner and M7D loader windows):**

| Item | Value |
| --- | --- |
| Switches | `runner_access_enabled` (default `false`) and `runner_access_expiry` (default `null`; later than plan time and at most 8 h after it). Terraform refuses runner access together with provisioner or loader access |
| Grant | `google_service_account_iam_member.operator_runner_impersonation[0]`: on the **runner account resource** only, member `operator_member` (one `user:`), role `bankpilot_dev_impersonate` (`iam.serviceAccounts.getAccessToken` only), condition `runner-expires-<EXP>` / `request.time < timestamp("<EXP>")` |
| Unchanged | Every binding the runner itself holds, its custom roles, the loader and provisioner (both stay revoked), the outputs |
| Repo checker | `bootstrap_plan_check.py --mode runner-access` (1 create, 15 unchanged, exact condition) and `--mode runner-revoke` (1 delete, or already removed). Every other mode rejects the grant, and rejects the switches if they are on |
| Independent check | `scripts/runner_window_verify.py plan`: written separately, re-derives the grant from `main.tf` |
| Live checks | `scripts/runner_window_collect.sh window\|revoked` (read-only, operator identity) then `runner_window_verify.py iam`. Eight checks: R1 runner account policy; R2–R4 the runner's own project, table and role grants unchanged; R5 keyless; R6 loader and provisioner still revoked; R7 a **live token mint** (window: allowed; revoked: `PERMISSION_DENIED`); R8 bootstrap state |

**Do not** run M8 as the operator's own Owner identity instead.

### 1.1 Execution (Terminal.app only; each apply is a separate approval)

```sh
C="$HOME/.secure/ebank-pilot-dev-261031-k7m4"
cd ~/Developer/enterprise-gcp-banking-fintech-ai-platform     # clean tree, approved commit
# 0. Baseline: base inputs unchanged; 15 addresses; serial-56 backup verified; no window open.
env -i HOME="$HOME" PATH="$PATH" python3 scripts/terraform_custody.py preflight --root bootstrap --phase run
bash scripts/runner_window_collect.sh revoked        # before any grant: the mint must be DENIED
python3 -I scripts/runner_window_verify.py iam --phase revoked --capture "<printed dir>" \
  --bootstrap-tfvars "$C/inputs/bootstrap-dev.tfvars"                     # expect ok

# 1. Window input and saved plan (base file + runner file)
EXP="$(date -u -v+3H +%Y-%m-%dT%H:%M:00Z)"
umask 077; printf 'runner_access_enabled = true\nrunner_access_expiry  = "%s"\n' "$EXP" \
  > "$C/inputs/bootstrap-dev-runner.tfvars"
P="$C/plans/bootstrap/$(date -u +%Y%m%dT%H%M%SZ)-runner-access-$(git rev-parse --short HEAD).tfplan"
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" /bin/sh -c '
  GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)" || exit 1
  export GOOGLE_OAUTH_ACCESS_TOKEN
  exec python3 scripts/terraform_custody.py run --root bootstrap -- \
    plan -input=false -var-file="$1" -var-file="$2" -out="$3"' sh \
  "$C/inputs/bootstrap-dev.tfvars" "$C/inputs/bootstrap-dev-runner.tfvars" "$P"
shasum -a 256 "$P"
J="${P%.tfplan}.json"
env -i HOME="$HOME" PATH="$PATH" python3 scripts/terraform_custody.py run --root bootstrap -- \
  show -json "$P" > "$J"; chmod 600 "$J"
python3 scripts/bootstrap_plan_check.py --mode runner-access < "$J"         # ok, 1 create, 15 unchanged
python3 -I scripts/runner_window_verify.py plan --mode access --plan-json "$J" --expiry "$EXP" \
  --bootstrap-tfvars "$C/inputs/bootstrap-dev.tfvars"                     # ok

# 2. After approval of that plan SHA-256: apply, then back up at once
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" /bin/sh -c '
  GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)" || exit 1
  export GOOGLE_OAUTH_ACCESS_TOKEN
  exec python3 scripts/terraform_custody.py run --root bootstrap -- apply "$1"' sh "$P"
python3 scripts/state_backup.py create --root bootstrap      # then OneDrive upload + verify-download

# 3. About 5 minutes later (IAM propagation): live window check
bash scripts/runner_window_collect.sh window
python3 -I scripts/runner_window_verify.py iam --phase window --capture "<printed dir>" \
  --expiry "$EXP" --bootstrap-tfvars "$C/inputs/bootstrap-dev.tfvars"     # ok (8/8 PASS)

# 4. M8 marts (§4), with the bundle window inside the runner window:
#    render-mart-bundle ... --window-start "<now>" --window-expiry "$EXP"

# 5. Revoke straight after verify-all (base file only), apply after approval, back up
R="$C/plans/bootstrap/$(date -u +%Y%m%dT%H%M%SZ)-runner-revoke-$(git rev-parse --short HEAD).tfplan"
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" /bin/sh -c '
  GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)" || exit 1
  export GOOGLE_OAUTH_ACCESS_TOKEN
  exec python3 scripts/terraform_custody.py run --root bootstrap -- \
    plan -input=false -var-file="$1" -out="$2"' sh "$C/inputs/bootstrap-dev.tfvars" "$R"
shasum -a 256 "$R"; RJ="${R%.tfplan}.json"
env -i HOME="$HOME" PATH="$PATH" python3 scripts/terraform_custody.py run --root bootstrap -- \
  show -json "$R" > "$RJ"; chmod 600 "$RJ"
python3 scripts/bootstrap_plan_check.py --mode runner-revoke < "$RJ"        # ok, 1 delete
python3 -I scripts/runner_window_verify.py plan --mode revoke --plan-json "$RJ" \
  --bootstrap-tfvars "$C/inputs/bootstrap-dev.tfvars"                     # ok
#   apply "$R" (same form as step 2), state_backup.py create --root bootstrap, upload, verify
rm "$C/inputs/bootstrap-dev-runner.tfvars"

# 6. About 5 minutes later: live denial
bash scripts/runner_window_collect.sh revoked
python3 -I scripts/runner_window_verify.py iam --phase revoked --capture "<printed dir>" \
  --bootstrap-tfvars "$C/inputs/bootstrap-dev.tfvars"                     # ok: mint PERMISSION_DENIED
```

**Rules:**
- Plan promptly after setting `EXP`. Never extend an approved window.
- At `EXP` − 15 minutes, stop M8 and revoke.
- A token minted before revocation stays valid for up to 1 hour. The runner's grants are not changed by this window, so that token can do only what the runner always could.
- If an apply fails part-way: read back the runner account policy live, then use `runner-revoke`. It accepts the grant as already removed (delete drift) or as a planned delete. Never re-apply the failed plan.
- Audit: in Logs Explorer (Data Access), filter `GenerateAccessToken` over the window. Expect the operator as caller and the runner as resource.
- Keep every capture and saved plan in `$C/m8/` and `$C/plans/`, never in Git. **M7D evidence in `$C/m7d/` is read-only and is not touched by any step.**

## 2. What M8 adds (offline)

| Piece | Source |
| --- | --- |
| SQL | The existing M5 transactional script `infrastructure/sql/marts.v1.insert.proposed.sql` (BEGIN; 4 `DELETE … WHERE TRUE`; 4 explicit-column `INSERT … SELECT`; COMMIT). Unchanged; only the approved identifiers are substituted |
| Reference | The M4 SQLite mart SQL run over the verified M7B packet. The four BigQuery SELECTs are also run in SQLite (only `DATE(x,'UTC')` → `substr(x,1,10)`) and must match it row for row |
| Bundle | `render-mart-bundle`: `m8.sh`, the rendered SQL, 5 dry-run requests, 1 script job request (`maximumBytesBilled=104857600`, stable job ID `m8_marts_<packet>_<sql hash>_a<attempt>`), expected mart CSVs, `SHA256SUMS` |
| Verifier | `verify-mart-evidence --stage {bundle,preflight,dryrun,job,children,readback,post-inputs,all}` |
| Code | `src/financial_crime_ml/deployment/marts_bundle.py`; tests `tests/test_marts_bundle.py` |

**Idempotent:** a re-run replaces the marts with identical rows. Preflight accepts marts holding either 0 rows or exactly the expected count. An identical resubmission returns HTTP 409, which is inspected and never resubmitted. A deliberate re-run needs a new bundle with `--attempt 2`.

## 3. Expected results (packet `m7p-a89332976c69d583137f9323`)

| Mart | Rows | Logical bytes | Expected CSV SHA-256 |
| --- | --- | --- | --- |
| transaction_daily | 1,312 | 80,032 | `30d428d0…9875a` |
| customer_daily | 3,412 | 238,840 | `1bb7d414…93e2` |
| account_daily | 3,428 | 243,388 | `21560df0…901b` |
| operations_daily | 3,762 | 278,877 | `fe98aabf…bb20` |

Every mart reconciles to the packet per currency: EUR 1,061 / 47,972,939; GBP 874 / 41,044,581; JPY 813 / 331,566; USD 1,252 / 54,122,799 (rows / minor units). That is 4,000 transactions in total.

**The verifier rejects any of the following:**
- **Rows and values:** a row that differs in any value; a duplicate grain; a currency total that differs; an anti-join failure (an unknown customer, or an account–customer pair not in `accounts`); an event outside its UTC date; `first > last`; activity counts outside 1..`transaction_count`; non-integer money; a NULL; a sub-second timestamp; an invalid DATE; incomplete pagination.
- **Execution:** a job not run by the runner; changed SQL or byte cap; a child count other than 10, or the wrong statement types; a child billed above the cap; inserted or deleted row counts that differ; a job outside the window.
- **Dry runs:** a dry run that reads a mart or returns a different schema.
- **Inputs:** any change to an input table during M8.

## 4. Commands (inside an open, verified runner window, §1.1 step 3)

```sh
C="$HOME/.secure/ebank-pilot-dev-261031-k7m4"
cd ~/Developer/enterprise-gcp-banking-fintech-ai-platform   # clean tree at the approved commit
# EXP is the runner window's expiry from §1.1 step 1; never recompute it here, so the bundle
# window can never outlast the runner grant.
: "${EXP:?set EXP to the runner window expiry from §1.1 step 1}"
START="$(date -u +%Y-%m-%dT%H:%M:00Z)"
mkdir -m 700 -p "$C/m8"
PYTHONPATH=src python3 -m financial_crime_ml.cli render-mart-bundle \
  --packet-dir "$PWD/outputs/packets/m7p-a89332976c69d583137f9323" \
  --expected-manifest-sha256 7e2fb8b7500a4aaf0652d2f1381359d333011a5f2ff5f92b1b684be0142b540f \
  --window-start "$START" --window-expiry "$EXP" \
  --output-dir "$C/m8/m8-a89332976c69d583137f9323-a1"
# Review m8.sh, sql/marts.sql and requests/*.json; approve the printed bundle_sha256.
export M8_APPROVED_BUNDLE_SHA256=<approved bundle SHA-256>
B="$C/m8/m8-a89332976c69d583137f9323-a1"
bash "$B/m8.sh" verify-bundle
bash "$B/m8.sh" preflight                 # 7 tables.get: inputs exact; marts 0 (or expected)
for d in transaction_daily customer_daily account_daily operations_daily script; do
  bash "$B/m8.sh" dryrun "$d"             # free; schema, referenced tables, bytes <= cap
done
bash "$B/m8.sh" run                       # the one billed job
bash "$B/m8.sh" job                       # repeat until DONE
bash "$B/m8.sh" children                  # jobs.list parentJobId, allUsers=false
for m in transaction_daily customer_daily account_daily operations_daily; do
  bash "$B/m8.sh" readback "$m"
done
bash "$B/m8.sh" post-inputs
bash "$B/m8.sh" verify-all                # evidence/report.json
```

Then revoke at once (§1.1 steps 5–6).

**Stop rules:** the same as [M7D §3](m7d_runbook.md#3-stop-rules-apply-to-every-step):
- any non-success response or verifier problem: stop;
- no retry with different settings, no role widening, no byte-cap increase;
- a `children` 403 (if `jobs.list` on the runner's own jobs is not covered by `jobs.create`) is **recorded as a limitation**, never fixed by a grant.

## 5. Cost (A7 equivalent)

On-demand billing, using the repository's illustrative US$10/TiB (not a verified London quote; see [readiness](../gcp_deployment_readiness.md)), with no free tier:

| Item | Bytes billed | Cost |
| --- | --- | --- |
| 5 dry runs, `tabledata.list`, `tables.get` | 0 | 0 |
| Script, conservative (10 MiB minimum per referenced table; the INSERT destination is counted too) | 140 MiB | about US$0.0013 |
| Script, upper bound (8 DML children, each at the 100 MiB cap) | 800 MiB | about US$0.0076 |
| Mart storage | about 0.84 MB logical | negligible |

Even the upper bound is under 1p, against the £5 **alert** budget (which is not a cap). Jobs: 5 dry runs + 1 parent + 10 children = **16 of the 20** allowed per approved execution.

## 6. Untested assumptions (checked live by the verifier, never relaxed)

- Child statement types are reported as `BEGIN_TRANSACTION`, `DELETE`, `INSERT` and `COMMIT_TRANSACTION`, with `dmlStats`.
- `maximumBytesBilled` on a script bounds each child.
- `jobs.list` with `parentJobId` is allowed for the runner's own jobs.
- Mart `numBytes` follows the logical model (DATE = 8 bytes).

A mismatch stops the run for review. The checks are not loosened to pass.
