# M6S controlled provisioner-access readiness

Date 2026-11-10. Baseline `main` at `72fb5eb` (M6R committed). Project `ebank-pilot-dev-261031-k7m4`, region `europe-west2`.

**Readiness only; access is not activated.** M6S did none of the following:
- call gcloud or any cloud API;
- run Terraform `init`, `plan`, `apply`, `refresh` or `state`;
- touch the real custody state, inputs, plans or backups, or OneDrive;
- change IAM;
- deploy any resource;
- commit or push.

Starting point (the operator's M6R record):
- 11 bootstrap resources in state, serial 14, with a verified encrypted backup;
- 2 keyless service accounts and 7 custom roles matching the repository;
- 1 runner project grant;
- empty service-account policies;
- `iam.googleapis.com` `ADMIN_READ`/`DATA_READ` configured;
- **provisioner access disabled**.

## Access-control design (existing M6K controls, re-validated offline)

| Control | Where | Offline evidence |
| --- | --- | --- |
| Window expiry after plan time and **≤ 8 h** after it | `access_expiry` validation using `plantimestamp()` | Terraform text test. The checker re-derives the window from the plan JSON `timestamp` |
| **Both** grants expire at the same instant | Shared `local.expiry_condition` (`request.time < timestamp("<expiry>")`) | Checker requires the exact title, description and expression on both grants |
| Provisioner gets only `bankpilot_dev_provision` at project level | `google_project_iam_member.provisioner` (count gated) | `provisioner.role.json`, 20 permissions: **no delete, no project `setIamPolicy`, no service-account, key or role administration** |
| Token minting for **one named user** on the **provisioner account only** | `google_service_account_iam_member.operator_impersonation` | `operator_member` must be a `user:` principal. The impersonate role contains only `iam.serviceAccounts.getAccessToken`. Checker rejects groups, domains, other members, the runner account and Token Creator |
| Keyless | No key resources; provider `impersonate_service_account` | Forbidden in both roots; the custody wrapper refuses key, credential and impersonation variables (M6P P1) |
| Pilot apply refuses to run if the provisioner holds an unconditional or extra project role | Identity module `google_project_iam_policy` postconditions | Module text |
| Bootstrap is never run as the provisioner; pilot always is | Provider blocks | Wrapper `provider_problems` (M6P) |

## Gap found and fixed: no checker for the window's plans

The M6Q checker covered only the `initial` and `recovery` bootstrap plans. The three plans this window needs (grant, pilot deployment, revoke) were left to manual review, so `scripts/bootstrap_plan_check.py` gains three modes. All are offline, use only the standard library, and never print the operator principal or bucket name.

| Mode | Root | Passes only if |
| --- | --- | --- |
| `access` | bootstrap | The 11 existing resources are unchanged and exactly the 2 grants are created, both conditioned on `access_expiry`, with `0 < expiry − plan timestamp ≤ 8 h`. The impersonation member equals `operator_member` (a `user:`), on the provisioner account, with the impersonate role. Access and audit flags are true. There is no drift and no forbidden role or principal |
| `pilot` | pilot-dev | Exactly 18 creates and no other change: the bucket (`europe-west2`, public access prevention enforced, uniform access, versioned, `force_destroy=false`), the dataset (`bankpilot_dev`, `europe-west2`, `delete_contents_on_destroy=false`), 7 deletion-protected tables, and bucket, dataset and table policies binding **only the runner** to its approved roles. Variables: `prefix=bankpilot`, the provisioner email, the region. No project or service-account IAM, no deferred data reads, no drift |
| `revoke` | bootstrap | Access is false and `access_expiry` is null. The 11 resources are unchanged and the 2 grants are deleted, or were already removed outside Terraform (shown as delete drift on those 2 addresses only) |

The existing `initial` and `recovery` modes now also require `access_expiry = null`, and the forbidden list adds `roles/iam.serviceAccount*`. A mutation-testing bug was found and fixed: the revoke mode was modifying a module constant in place, which is now a `frozenset`.

## Approved scope for the window

| In scope | Out of scope (separate gates) |
| --- | --- |
| Bootstrap: +2 conditional grants, later −2 | Data loads, BigQuery jobs or queries, any runner token use |
| Pilot-dev: the 18 resources above | API enablement, budgets, other projects or regions |
| Read-only verification and the audit-event test | Any `destroy`, import, state surgery or key creation |

## Expected Terraform plans

| Step | Root and inputs | Terraform line | Checker |
| --- | --- | --- | --- |
| Grant | bootstrap; base file plus window file | **`2 to add, 0 to change, 0 to destroy`** | `--mode access`: `creates 2`, `unchanged 11`, `window_hours ≤ 8` |
| Deploy | pilot-dev; `pilot-dev.tfvars` | **`18 to add, 0 to change, 0 to destroy`** | `--mode pilot`: `creates 18` |
| Revoke | bootstrap; base file only | **`0 to add, 0 to change, 2 to destroy`** (`0/0/0` after an emergency manual removal) | `--mode revoke` |

## Required approvals

1. **Review and commit M6S.**
2. **Window inputs:**
   - the expiry (recommended: plan time + 3–4 h, never more than 8 h);
   - the globally unique, non-identifying pilot bucket name;
   - the window start time.
3. **Grant:** approve the plan SHA-256, then apply.
4. **Deploy:** approve the pilot plan SHA-256, then apply.
5. **Revoke:** recommended as **pre-approved** with the window, since it only reduces access. It still uses a reviewed saved plan, the checker and a SHA-256.

## Runbook (Terminal.app only; never in Claude Code)

`C="$HOME/.secure/ebank-pilot-dev-261031-k7m4"`. Every Terraform call goes through `scripts/terraform_custody.py run` in the M6P `env -i` form. The token is fetched inside the child shell only for `plan` and `apply`.

### A. Pre-activation checks (all must pass; read-only)

1. **Repository and local custody:**
   - the repository is clean at the approved commit, and CI is green;
   - `preflight --root bootstrap --phase run` and `--root pilot-dev --phase run` print no problems;
   - `run --root bootstrap -- state list` shows exactly the 11 bootstrap addresses;
   - the latest bootstrap backup (serial 14) is verified.
2. **Live baseline:**
   - the project policy has only the Owner binding and runner → `bankpilot_dev_jobs`;
   - `auditConfigs` has `iam.googleapis.com` with `ADMIN_READ`/`DATA_READ` and no exemptions;
   - both service-account policies are empty;
   - both accounts are enabled, with no user-managed keys.
3. **Token-minting baseline:** confirm the operator **cannot already** mint provisioner tokens. Call `testIamPermissions` for `iam.serviceAccounts.getAccessToken` on the provisioner account as the operator, and expect an empty result. If the permission is already held, stop: the impersonation grant would not be the controlling gate.
4. **Budget (Console):**
   - the project budget `Enterprise Banking AI Pilot - Dev` exists: **£5/month**, project-scoped, thresholds **50/80/100%**;
   - the recipients include the operator;
   - month-to-date spend is about £0 (M6G).
5. **Inputs:**
   - create `$C/inputs/pilot-dev.tfvars` (mode 600): `project_id`, `region = "europe-west2"`, `environment = "dev"`, **`prefix = "bankpilot"`** (not the example's `fincrime-m5`), `bucket_name`, and `provisioner_service_account = "bankpilot-dev-provisioner@ebank-pilot-dev-261031-k7m4.iam.gserviceaccount.com"`;
   - leave the base `bootstrap-dev.tfvars` unchanged (access `false`, expiry `null`).

### B. Grant (activation)

```sh
EXP="$(date -u -v+4H +%Y-%m-%dT%H:%M:00Z)"          # ≤ 8 h; plan promptly after this
umask 077; printf 'provisioner_access_enabled = true\naccess_expiry              = "%s"\n' "$EXP" \
  > "$C/inputs/bootstrap-dev-window.tfvars"
P="$C/plans/bootstrap/$(date -u +%Y%m%dT%H%M%SZ)-access.tfplan"
# plan: M6P step 2 form with BOTH files:
#   plan -input=false -var-file="$C/inputs/bootstrap-dev.tfvars" \
#        -var-file="$C/inputs/bootstrap-dev-window.tfvars" -out="$P"
shasum -a 256 "$P"
env -i HOME="$HOME" PATH="$PATH" python3 scripts/terraform_custody.py run --root bootstrap -- show -json "$P" \
  | python3 scripts/bootstrap_plan_check.py --mode access
```

- **Apply after approval** with the M6P apply form.
- **Then, immediately:** `state_backup.py create --root bootstrap`, upload, then `verify-download`.
- **Read back the policies:** the provisioner holds `bankpilot_dev_provision` with the `expires-…` condition, and the provisioner account's policy holds the operator with `bankpilot_dev_impersonate` and the same condition. Nothing else.
- **Wait about 5 minutes** for IAM propagation (P7).

### C. Pilot deployment

1. Run `run --root pilot-dev -- plan` with `-var-file="$C/inputs/pilot-dev.tfvars"` and `-out` in `plans/pilot-dev/`.
2. Run `show -json` piped to `--mode pilot`, and record the SHA-256.
3. Apply after approval.
4. Then, immediately: `state_backup.py create --root pilot-dev`, upload, then `verify-download`.
5. **Verify** with read-only reads:
   - the bucket is private, uses uniform access and sits in `europe-west2`;
   - the dataset access lists only the runner metadata role, and the provisioner keeps no creator OWNER entry. **Superseded by [M6T](m6t_validation.md):** BigQuery requires a direct dataset owner, so the policy must also bind an approved owner;
   - all 7 tables exist and are empty, and the runner has its table roles.

### D. Audit-event test (inside the window)

The pilot plan's impersonation is itself a token mint. In Logs Explorer (Data Access) for the project, filter `protoPayload.methodName="GenerateAccessToken"` over the window.

Expect entries with the operator as caller and the provisioner account as the resource. This is the first test of audit **event generation**: M6R verified the configuration only.

If no entry appears within about 15 minutes, record a finding, complete revocation, and open no further windows until it is resolved.

### E. Revocation (straight after C and D, and no later than expiry)

1. **Plan with the base file only:** `-var-file="$C/inputs/bootstrap-dev.tfvars"` and nothing else.
2. Check with `--mode revoke`, record the SHA-256, and apply.
3. **Then:** back up bootstrap state, upload and `verify-download`.
4. Run `rm "$C/inputs/bootstrap-dev-window.tfvars"`.
5. **Verify:**
   - the project policy is back to the baseline;
   - the provisioner account's policy is empty;
   - a token mint attempt for the provisioner fails with `PERMISSION_DENIED`, and is logged.

`--mode revoke` tolerates only the bounded etag-only drift described in [Revocation readiness](#revocation-readiness-etag-drift-and-partial-failure), and its report lists it as `accepted_etag_drift`.

**The expiry is the hard stop.** IAM evaluates `request.time` on every call, so both grants stop working at `EXP` even if revocation is late. Revocation removes the dormant bindings so the live policy matches the baseline.

### F. Failure handling

| Situation | Action |
| --- | --- |
| Any checker `ok: false`, an unexpected plan line, or `EXP` validation fails | Do not apply. Delete the plan and re-plan only after the cause is understood |
| Pilot plan `403` on token minting or a permission error | Retry once after 5 min (propagation). If it fails again: stop, revoke, record the missing permission (P2/P6). **Never** widen roles inside the window |
| Identity postcondition fails | Stop and revoke. The provisioner's project bindings differ from expectations |
| Pilot apply fails part-way | Back up whatever state exists. Do not retry blindly. Check the dataset access for a leftover creator OWNER entry. Revoke on schedule; repair under a new window |
| Bootstrap grant apply fails part-way | Back up, then revoke. See [partial failure](#revocation-under-partial-failure): the checker stays strict, and a missing grant is handled by the documented manual review |
| Window about to expire mid-task | Stop. Let it expire, revoke, and plan a new window. Never extend an approved plan |
| Suspected misuse, or Terraform unavailable | **Emergency:** in the Console, remove both conditional bindings. If needed, also disable `bankpilot-dev-provisioner`; re-enabling it later shows as an `update` that `revoke` rejects, so review it manually. Then run the Terraform revoke: the checker accepts exactly those two as `already_removed` |
| Budget alert fires | Stop work, revoke, and investigate the bill. Alerts are **notifications, not a spending cap**, and arrive with billing-data delay (hours) |

## Budget safeguards (£5/month)

- **The existing Console budget is the alert mechanism.** It is project-scoped at **£5**, with 50/80/100% thresholds. Nothing in the repository manages budgets.
- **It caps nothing:**
  - spend continues past 100%;
  - alerts lag behind usage;
  - there is no automatic billing disable, which would be a separate design with its own risks.
- **Exposure within this scope is near zero:** an empty versioned bucket, 7 empty tables and no jobs or loads. The material cost drivers (queries, loads, storage growth) are out of scope.
- **Guard:**
  - check the budget before activation;
  - check month-to-date spend after revocation;
  - treat any alert as a stop condition;
  - the separate US$1 workload allowance (`pilot_controls.json`) applies to later workload gates.

## Assumptions that cannot be validated offline

All of these are operator gates. None is contradicted by the repository or tests, and each fails closed or is checked live before it matters.

| Assumption | Gate |
| --- | --- |
| The Owner alone cannot mint provisioner tokens | Pre-check A3 |
| IAM accepts the conditional custom-role grants on the project and on the service-account resource | Grant apply and policy read-back. Failure leaves access disabled |
| The provider reads the project policy with conditions (version 3) | Identity postconditions **fail closed** if not |
| `terraform show -json` includes the plan `timestamp` | The checker **fails closed** if it is missing |
| The 20 provision permissions suffice for the 18 resources | Pilot plan and apply. Failure means stop and revoke, never widen in-window |
| A `GenerateAccessToken` event is logged | Step D |

## Residual risks

1. **Wrapper flags: closed (see [Wrapper hardening](#wrapper-hardening-plan-scope-and-refresh)).** What remains: running `terraform` outside the wrapper bypasses every wrapper check, though the sentinel backend still fails closed. The refusal is by flag name, so a future Terraform flag with a new name that also narrows or skips refresh would need a wrapper review on upgrade (the lockfile and `required_version < 2.0` bound this).
2. **Sessions outlive the plan:** the operator's gcloud session and the 1 h provisioner tokens persist. Project-role calls stop at expiry because of the condition. The tokens remain bearer credentials until they expire.
3. **Dataset creator access:** the authoritative dataset policy removes the creator OWNER entry only if that apply succeeds (step C5 check).
4. **The Owner is unrestricted (P8):** a compromised Owner session bypasses every gate here.
5. **Text checks:** the provider check is regex-based, and the checker's forbidden-text scan is a substring scan. Plan review remains the authoritative gate.

## Finding: runner grant etag drift in the access plan

**Observed (operator):** the approved bootstrap access plan was rejected by `--mode access`. `resource_drift` held exactly one entry: `google_project_iam_member.runner_jobs`, `actions = ["update"]`, with **only `etag`** changed. Independent inspection confirmed that the role and member are unchanged and match the approved runner binding. No apply was performed on that plan.

**Cause (hypothesis, consistent with the evidence):**
- `google_project_iam_member` stores the *project policy* etag at the time it was written, not an etag of its own;
- the M6Q recovery apply rewrote the project policy to add the `iam.googleapis.com` auditConfig, which changed the policy etag;
- refreshing `runner_jobs` therefore reports a new etag although its binding is unchanged.

The checker rejected it correctly as unknown drift. It is benign only if nothing but the etag moved.

**Exception (access mode only, one entry, fully bounded):** `runner_etag_drift_problems()` accepts a drift entry only if all of the following hold:
- **Identity of the entry:** the address is exactly `google_project_iam_member.runner_jobs`, managed, with that type and name and no index, and the actions are exactly `["update"]`.
- **Shape:** `before` and `after` are both objects with exactly the attributes `condition`, `etag`, `id`, `member`, `project` and `role`. Missing or extra attributes are rejected.
- **What changed:** every attribute except `etag` is identical before and after. Both etags are non-empty and differ. There are no unknown values.
- **Values on both sides:** they equal the approved grant: role `bankpilot_dev_jobs`, member `serviceAccount:bankpilot-dev-runner@…`, this project, no condition and a non-empty `id`.
- **At most one such entry:** a duplicate is rejected, and so is drift on any other address.

All other access checks still apply unchanged. `runner_jobs` must still be `no-op` in `resource_changes`, the role permissions must still match the repository JSON, and so on. The report lists accepted entries as `accepted_etag_drift`. **`initial`, `recovery`, `revoke` and `pilot` modes keep rejecting all such drift.**

**Evidence (offline):** 33 new tests in `tests/test_access_window_plan_check.py`.
- **Positive:** the exact etag-only drift passes, with 2 creates, 11 unchanged and `accepted_etag_drift = [runner_jobs]`.
- **27 adversarial variants, all rejected:**
  - role, member, project, condition or `id` changed after;
  - a wrong role, member or condition on both sides, or an empty `id`;
  - etag unchanged, missing or empty;
  - a missing or extra attribute, a null `after`, unknown values;
  - a delete, replace or no-op action;
  - an indexed address, data mode or a different type;
  - the same drift on the audit config;
  - a duplicate entry, extra drift on a role;
  - `runner_jobs` not `no-op` in the changes, or role permissions changed.
- **Other modes:** the exception is refused in `initial`, `recovery` and `revoke`.
- **No regressions:** all 20 earlier access adversarial cases still fail with the drift present.
- **Direct tests of each layer:** a different address, and a role or member change reported by name.

**Mutation check:** 10 mutations, all detected:
- role and member changes ignored;
- attribute set unchecked;
- approved values unchecked;
- unchanged etag accepted;
- any action accepted;
- exception allowed in all modes;
- duplicates accepted;
- index unchecked;
- unknowns accepted;
- any address accepted.

The first pass missed 2, because the approved-value and caller-address checks already covered them. Direct tests now hit each layer. The file was restored byte-identical.

**Residual risks of this exception:**
- **Re-planning:** the access saved plan must be re-generated and its new SHA-256 approved. The rejected plan is not reused.
- **Revoke will see the same pattern:** addressed by the bounded revoke exceptions in [Revocation readiness](#revocation-readiness-etag-drift-and-partial-failure).
- **What the etag can't show:** it changes on *any* project-policy write. The exception proves the runner binding is unchanged, not that nothing else in the policy changed. Pre-check A2 (live project policy baseline) remains required before applying.

## Revocation readiness: etag drift and partial failure

### Expected drift (analysis; not assumed)

The provider documents a project IAM resource's `etag` as "the etag of iam policy". The string is in the cached 7.31.0 provider binary, read without running it. So `runner_jobs`, `provisioner[0]` and the audit config each store the **project-policy** etag. The impersonation grant stores the **service-account** policy etag.

| Event | Project policy etag | Expected effect on the revoke plan |
| --- | --- | --- |
| Access apply writes `provisioner[0]` | Changes | `runner_jobs` and the audit config keep the older etag → **likely etag-only update drift on both**. `provisioner[0]` stores the new etag → normally no drift |
| Access apply writes `operator_impersonation[0]` | Unchanged (service-account policy) | None on project resources |
| Any later project-policy write (Console, another apply) | Changes | The same two, plus **`provisioner[0]` etag drift** (not tolerated: see below) |

The checker does **not** require this drift. A revoke plan with no drift, or with only one of the two entries, still passes.

### Bounded exceptions (`etag_drift_problems`)

| Mode | Tolerated etag-only drift |
| --- | --- |
| `access` | `google_project_iam_member.runner_jobs` (unchanged) |
| `revoke` | `google_project_iam_member.runner_jobs` and `google_project_iam_audit_config.iamcredentials[0]` |
| `initial`, `recovery`, `pilot` | None |

An entry is accepted only if all of the following hold:
- **Identity:**
  - for `runner_jobs`: exact address, managed, type and name, with no index;
  - for the audit config: index `0`;
  - the actions are exactly `["update"]`.
- **Shape:** `before` and `after` are objects with exactly the schema attributes:
  - for `runner_jobs`: `condition`, `etag`, `id`, `member`, `project`, `role`;
  - for the audit config: `audit_log_config`, `etag`, `id`, `project`, `service`, where each log-config entry has exactly `exempted_members` and `log_type`.
- **Change:** every non-etag attribute is identical; both etags are non-empty strings and differ; **no `true` leaf anywhere** in `after_unknown` (deep check).
- **Approved values, on both sides:**
  - for `runner_jobs`: role `bankpilot_dev_jobs`, the runner member, this project, no condition, a non-empty `id`;
  - for the audit config: service `iam.googleapis.com`, this project, log types exactly `ADMIN_READ` and `DATA_READ`, `exempted_members` empty, a non-empty `id`.
- **At most one entry per address.** A duplicate is rejected, and so is any other drift.

The existing revoke rules all still apply:
- access is false and the expiry is null;
- the 11 resources stay `no-op`;
- exactly the 2 grants are deleted, or appear as delete drift (emergency removal);
- no creates, and the forbidden-text scan.

**Fails closed by construction:**
- the attribute sets are exact, so a provider schema change makes the entries **rejected**, never silently accepted;
- the exceptions widen acceptance only for etag changes on two resources whose security-relevant values are pinned on both sides.

**Deliberately still blocked:**
- etag drift on `provisioner[0]` or the impersonation grant, since both are being deleted and need no tolerance;
- any drift on roles or service accounts;
- any attribute change on the tolerated resources.

### Revocation under partial failure

The checker stays strict. A plan that does not match fails, and **the hard expiry still ends access** whatever happens to revocation. IAM evaluates `request.time < timestamp(EXP)` on every request for both grants, so nothing done or left undone after `EXP` extends access. Provisioner tokens minted before `EXP` cannot use the project role after `EXP`.

| Situation | Revoke checker result | Safe procedure |
| --- | --- | --- |
| Normal (both grants in state) | `deletes 2`; `accepted_etag_drift` lists 0–2 tolerated entries | Approve the SHA-256, then apply |
| Access apply created only one grant | `<other grant>: expected delete missing` | Confirm offline (`state list`) that only one grant address is in state, and live that the other binding does not exist. Then apply this reviewed plan under an explicit **manual override approval** that records the single checker problem. Or let it expire and revoke later |
| Emergency Console removal of both grants | `already_removed` lists both, `deletes 0`, plus tolerated etag drift | Apply (it updates state only), then verify the live policies |
| `provisioner[0]` etag drift, or any other unexpected drift | Rejected | Do not apply. Read back the live project policy and compare with the baseline plus the two grants. Remove the conditional bindings in the Console if needed (they expire anyway), then re-plan: the emergency path above |
| Revoke apply fails part-way | — | Back up the state. Re-plan with `--mode revoke`: a grant already deleted is then absent (`expected delete missing` → the manual override row above). Verify the live policies |
| Checker cannot run, or any doubt | — | Do nothing destructive. **Expiry is the safeguard.** Remove the bindings in the Console after review, then reconcile through the emergency path |

**Refresh visibility caveat:**
- If a refresh ever failed to see a still-present conditional binding, the plan would show it as delete drift, and the checker would accept it as `already_removed`.
- The live read-back in step E5 is therefore mandatory after every revoke: the project policy must be the baseline and the service-account policy empty.

### Evidence (offline)

46 new tests:
- **Positive:**
  - revoke with no drift, runner drift only, audit drift only, or both in either order;
  - both together with emergency delete drift;
  - nested `after_unknown` structures with no `true` leaf.
- **37 adversarial revoke cases, all rejected:**
  - the audit config: service, log types, exemptions, keys, `id`, project, etag, null sides, nested unknowns, every wrong action, index, name, type, mode;
  - duplicate audit or runner entries;
  - a runner member or condition change;
  - `provisioner[0]` etag drift, an impersonation update, role drift;
  - the audit config or runner deleted in the changes;
  - access re-enabled.
- **No regressions:** all 8 earlier revoke adversarial cases still fail with the drift present; access mode still rejects audit drift; initial and recovery modes reject all of it.
- **Direct tests of each layer:** the helper's messages for each check.

**Mutation check:** 15 mutations, all detected:
- audit drift tolerated in access mode;
- `provisioner[0]` tolerated in revoke;
- drift tolerated in initial mode;
- duplicates accepted;
- audit values unchecked;
- exemptions unchecked;
- log types unchecked;
- log-config keys unchecked;
- nested unknowns ignored;
- index unchecked;
- attribute set unchecked;
- a service change ignored;
- an unchanged etag accepted;
- any action accepted;
- the `before` side unchecked.

The last one survived at first, because the identical-attributes rule makes the two layers overlap. A direct test now pins both. The file was restored byte-identical.

## Wrapper hardening: plan scope and refresh

Before this change, `scripts/terraform_custody.py` accepted three flags that undermine the saved-plan checker:
- `-refresh=false` hides out-of-band drift, which the checker cannot see;
- `-target` produces partial plans;
- `-replace` forces replacements.

They are now refused in `build_command`, before any preflight, token handling or Terraform execution.

| Flag | Rule |
| --- | --- |
| `-refresh` | Allowed bare, or as `=true`/`=t`/`=1` in any case (Go `ParseBool` true spellings). **Every other value is refused,** including `false`/`f`/`0` in any case, `=` with an empty value, and invalid values such as `no` (fail closed) |
| `-target`, `-replace` | Refused in every form: `=value`, bare (the separate-value form), `--` prefix, any case |
| `-refresh-only` | Refused with any value. It adopts out-of-band changes into state without review |
| `-target*`, `-replace*`, `-exclude*` name prefixes | Refused, so later scoping variants fail closed. Terraform 1.16.5 has none: its help strings, read from the binary without running it, list only `-target`, `-replace`, `-refresh` and `-refresh-only` |

**Bypass resistance:**
- **Argument forms:**
  - every argument is checked wherever it appears: before or after other flags, after the saved plan and after a `--` separator;
  - existing `--flag` normalisation means dash variants hit the same rule;
  - names are compared case-insensitively.
- **Values are not flags:** a flag-like string inside a value (`-var=note=-target=x`) is correctly allowed, because Go assigns it to `-var`.
- **Environment injection:**
  - `TF_CLI_ARGS`, `TF_CLI_ARGS_plan`/`_apply` and `TF_CLI_CONFIG_FILE` are refused by the preflight;
  - they are also absent from the child process's explicit environment allow-list (M6P P1), which is a second, independent layer.
- **Not executed:** a refused invocation never starts Terraform. A fake Terraform proves this, and the control run with `-refresh=true` does execute.

**Unchanged:**
- normal refresh;
- the approved `plan -input=false -var-file=… [-var-file=…] -out=…` and saved-plan `apply` forms;
- `-lock-timeout` and `-parallelism`;
- credential isolation, backend custody, the sentinel backends and the operator-only guard.

**Tests:** 196 new tests in `tests/test_terraform_custody.py`.
- **Refused spellings:** 31 bypass spellings × plan/apply × 3 positions (first, last, after `--`), all refused.
- **Every command:** the four restrictions are refused for `init`, `validate`, `show` and `state list` too.
- **Accepted forms:** 12 accepted forms (normal refresh variants, a value containing `-target`, unrelated flags), each passed through unchanged, plus saved-plan apply.
- **Environment injection:** 5 cases, each refused by the preflight and by `run`, absent from the child environment, with Terraform never executed, and the refusal never echoing the value.
- **Execution:** refused flags never execute Terraform, while the control run does.

**Mutation check:** 8 wrapper mutations, all detected (failures in brackets):
- scope check removed (101);
- exact-case match with no prefixes (36);
- refresh check removed (83);
- refresh deny-list only (42);
- an empty `=value` accepted (6);
- refresh refused entirely, which catches over-blocking (8);
- `--` normalisation removed (38);
- `TF_CLI_ARGS` allowed (5).

The file was restored byte-identical.

## Changes

| File | Change |
| --- | --- |
| `scripts/bootstrap_plan_check.py` | `access`, `revoke` and `pilot` modes; null-expiry rule for disabled modes; `roles/iam.serviceAccount*` forbidden; constant-mutation fix; bounded `etag`-only drift exceptions (`runner_jobs` in access mode; `runner_jobs` and the audit config in revoke mode); duplicate drift entries rejected; deep `after_unknown` check |
| `tests/test_access_window_plan_check.py` (new) | 137 tests (58, plus 33 for the access etag exception and 46 for revocation readiness): good access, pilot and revoke plans; emergency-removal revoke; 20 adversarial access, 8 revoke and 20 pilot plans; consistency with the real bootstrap condition template, the 8 h validation and the 18 module addresses; CLI |
| `Makefile` | `gcp-offline-check` includes the new tests |
| `docs/index.md`, this file | Link and runbook |

| `scripts/terraform_custody.py` | Refuses `-refresh=false` (any non-true value), `-target`, `-replace`, `-refresh-only`, and the `-target*`, `-replace*` and `-exclude*` prefixes, for every command. See below |
| `tests/test_terraform_custody.py` | 196 tests for the wrapper hardening (below) |

No Terraform or IAM JSON change.

**Mutation check:** 12 mutations of the new checks, all detected:
- 9 h window;
- condition not compared, or condition allowed to be absent;
- operator equality dropped;
- grant target unchecked;
- revoke accepting any drift;
- bucket and dataset policy always accepted;
- public access unchecked;
- reader role ignored;
- null expiry unchecked;
- missing timestamp accepted;
- pilot variables unchecked.

The file was restored byte-identical.

## Validation

| Check | Result |
| --- | --- |
| Full `pytest` | **953 passed, 1 skipped** |
| `make gcp-offline-check` | **709 passed, 1 skipped**; `terraform fmt` passed |
| `ruff check .` | Passed |
| Docs validator, `git diff --check` | Passed |

## Next operator action

**Review and commit M6S.** Then do the pre-activation checks (A1–A5). Approval 2 (window inputs) follows only after both.

**Stopped after M6S.** Access was not activated.
