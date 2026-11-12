# M6T BigQuery dataset IAM partial-apply recovery

Date 2026-11-10. Baseline `main` at `dba6d0c`. Project `ebank-pilot-dev-261031-k7m4`, region `europe-west2`.

**Status (closed 2026-11-12): M6T is complete.** The operator applied the recovery on 2026-11-10. **All 18 pilot resources are deployed** and the temporary access has been revoked. See [Operational closure](#operational-closure-operator-verified-2026-11-10) for the evidence and the limitations that remain. The pilot holds **empty tables only**. No data has been loaded, no workload has run, and nothing here claims production readiness.

The sections from the incident up to the closure record the offline implementation **as it stood before the recovery**, when 17 of 18 resources existed. They are kept as historical evidence. The assistant's M6T work, including this closure update, did none of the following:
- call gcloud or any cloud API;
- run Terraform `init`, `plan`, `apply`, `state` or `refresh`;
- read or modify custody files, private tfvars, state, saved plans, backups or OneDrive;
- change IAM;
- open an access window;
- commit or push.

## Incident (as reported by the operator)

| Item | Detail |
| --- | --- |
| Action | M6S pilot apply of the approved saved plan (18 creates) inside the access window |
| Created (17) | Bucket and its runner-only bucket policy; dataset `bankpilot_dev`; 7 tables; 7 runner table grants |
| Failed (1) | `module.bigquery.google_bigquery_dataset_iam_policy.metadata`: **HTTP 400 "No owners specified"** |
| Afterwards | Bootstrap grants revoked. Partial pilot state backed up at **serial 19**, and post-revocation bootstrap state at **serial 20**; both encrypted, downloaded, authenticated and verified |

## Root cause

1. **The resource is authoritative.** `google_bigquery_dataset_iam_policy` replaces the dataset's **entire** access policy with `policy_data`. It is not additive.
2. **The policy contained only the runner.** The M6S module rendered a single binding: the runner's custom metadata role.
3. **BigQuery requires an owner.** A dataset must keep at least one directly assigned owner. The runner-only policy had none, so the API rejected the write with HTTP 400.
4. **The checker encoded the same mistake.** The M6S `pilot` checker required the dataset policy to bind *only* the runner, so it accepted the defective plan.
5. **Two goals conflicted:** M6S step C5 expected the creator OWNER entry to be removed, while BigQuery requires *some* direct owner. The conflict is resolved only by naming a different, approved direct owner: decision D1 below.

**Likely live state (inferred, not observed by M6T):** the dataset still carries BigQuery's creation-default access, including the **creator (provisioner) as OWNER** and the project special groups. This is a **dormant standing privilege**: the provisioner cannot currently be impersonated, and it has no keys. It persists until the recovery is applied.

**Provider distinction:**
- `google_bigquery_dataset_iam_policy` is authoritative, for the whole policy;
- `google_bigquery_dataset_iam_member` and `_binding` are additive;
- the dataset `access` blocks and `google_bigquery_dataset_access` manage the legacy access list;
- the provider forbids mixing the IAM resources with `access` management on the same dataset.

The module uses only the authoritative policy resource. Tests forbid the others and forbid any `access` block on the dataset.

## Decision: D1 (approved by the operator)

The dataset `bankpilot_dev` gets **exactly two intended bindings, both unconditional:**

| Role | Member |
| --- | --- |
| `roles/bigquery.dataOwner` (the direct owner) | `user:ibraheemolasupo@yahoo.com` |
| `projects/ebank-pilot-dev-261031-k7m4/roles/bankpilot_dev_metadata` | `serviceAccount:bankpilot-dev-runner@ebank-pilot-dev-261031-k7m4.iam.gserviceaccount.com` |

- **The provisioner's creator OWNER entry and the special-group defaults are removed** by the authoritative replacement.
- **No new project IAM grants.** The owner is already the project Owner, so the binding adds no new effective capability; it satisfies BigQuery's direct-owner requirement.

Alternatives considered and not adopted:
- D2: another named user;
- D3: an additive runner member, which would keep the provisioner as OWNER;
- D4: the `projectOwners` special group, which relies on inherited ownership and is unverified;
- D5: the provisioner as owner, which is a service-account owner;
- a group owner, which is unavailable without an Organization.

## Implementation

| File | Change |
| --- | --- |
| `infrastructure/terraform/variables.tf` | New **required** `dataset_owner_member` (no default). Validation: exactly one `user:` email (`^user:[^@\s,]+@[^@\s,]+\.[^@\s,]+$`), **not** ending in `.gserviceaccount.com` (case-insensitive). Groups, domains, special groups, public principals and lists are rejected |
| `infrastructure/terraform/main.tf` | Passes `dataset_owner = var.dataset_owner_member` to the BigQuery module. No other wiring change |
| `infrastructure/terraform/modules/bigquery/main.tf.json` | New required module variable `dataset_owner`. `policy_data` = `{role = "roles/bigquery.dataOwner", members = [var.dataset_owner]}` and `{role = var.metadata_role, members = ["serviceAccount:${var.runner_email}"]}`, with no conditions. **Same resource address** |
| `infrastructure/terraform/environments/{dev,test}.tfvars.example` | Placeholder `dataset_owner_member = "user:approved-dataset-owner@example.com"` |
| `scripts/bootstrap_plan_check.py` | `pilot` and `pilot-recovery` require `--dataset-owner`. The owner pattern mirrors the Terraform validation exactly (a test asserts they are identical). **New:** the plan variable `dataset_owner_member` must equal the approved `--dataset-owner` |
| `tests/test_pilot_recovery_plan_check.py` | 83 tests (below) |
| `tests/test_access_window_plan_check.py` | The M6S pilot fixture includes the owner binding and variable |
| `Makefile` | `gcp-offline-check` includes the M6T tests |
| `docs/upgrade/m6s_validation.md` | Step C5 marked as superseded by M6T |

The private pilot inputs file and the custody tree were not touched. **Operator action** (manual, in the protected custody file `$C/inputs/pilot-dev.tfvars`, mode 600), add exactly:

```hcl
dataset_owner_member        = "user:ibraheemolasupo@yahoo.com"
```

## Bounded recovery checker

`python3 scripts/bootstrap_plan_check.py --mode pilot-recovery --dataset-owner=user:ibraheemolasupo@yahoo.com` passes only if all of the following hold:
- **The owner:** the approved owner is valid, as above, and equals the plan's `dataset_owner_member` variable. It is never printed; the report shows only `dataset_owner_approved`.
- **The changes:** exactly **one create**, `module.bigquery.google_bigquery_dataset_iam_policy.metadata`, and the **17 created resources as `no-op`**.
- **The dataset policy:** exactly `{"bindings": [...]}` with exactly the two D1 bindings, in any order, with no conditions or extra keys.
- **Rejected policy contents:**
  - missing ownership, the M6S runner-only policy, an owner-only policy;
  - extra owners: two users, the creator kept, a duplicate binding;
  - a service-account owner: runner or provisioner;
  - a different user, primitive `OWNER`, `roles/bigquery.admin`;
  - privilege widening: the runner given `tables` or `dataEditor`, the owner added to the metadata role;
  - an extra binding, a condition, an extra top-level key, invalid or missing JSON.
- **Rejected plans:** any drift, update, replace (a tainted policy) or delete, a missing or extra resource, dataset IAM member or access resources, project IAM, deferred data reads, an errored plan, and wrong variables (`prefix`, provisioner, bucket name, a differing or missing owner).
- **The other 17 keep all their checks:** a private, protected bucket and runner-only bucket policy; dataset location and protection; deletion-protected tables; runner table grants with their approved roles.

The live dataset's current default access, including the creator OWNER entry, may appear in the no-op dataset's `access` attribute. That is expected: it is what the authoritative policy replaces.

## Tests and mutation

`tests/test_pilot_recovery_plan_check.py`, **83 tests**:
- **The D1 design in Terraform:**
  - the module policy template is exactly the two bindings, with no condition;
  - no additive, binding or access resources, and no dataset `access` block;
  - the module variable has no default;
  - the root variable is required and validated, and is wired to the module;
  - the examples carry placeholders only, with no real address;
  - the template, rendered as Terraform would render it, passes the recovery check.
- **The HTTP 400 defect, reproduced:** the M6S runner-only template, rendered, is rejected for missing the owner.
- **Terraform and the checker agree on owner validation** for 16 principals: valid users; runner, provisioner and upper-cased service-account emails; group, domain, `allUsers`, lists, malformed and special-group values.
- **Positive:** 1 create and 17 unchanged; binding order doesn't matter; neither the owner nor the bucket name is printed.
- **Rejected:** 15 invalid owners, 18 non-compliant policies and 26 adversarial recovery plans.
- **Mode isolation and CLI:** as before.

**Mutation check:** 9 new mutations across Terraform and the checker, all detected. The files were restored byte-identical.
- **Terraform:**
  - the module owner binding removed (the M6S defect);
  - the owner role widened to `bigquery.admin`;
  - the runner added as an owner;
  - the service-account exclusion removed;
  - the owner given a default;
  - groups accepted;
  - the root wiring removed.
- **Checker:**
  - the variable cross-check removed;
  - commas allowed in the owner.

The earlier M6T checker mutations remain covered.

## Access-mode audit-config etag drift (M6T)

**Observed (operator):** the bootstrap access plan for the recovery window (commit `c5e89fb`) was rejected by `--mode access`. Besides the already-accepted `runner_jobs` etag drift, `resource_drift` held `google_project_iam_audit_config.iamcredentials[0]` with `actions = ["update"]`. Independent inspection confirmed **only `etag` differed**. The grants remain revoked, and no recovery deployment has occurred.

**Cause (consistent with M6S analysis):**
- both resources store the **project IAM policy** etag;
- the M6S revoke apply deleted `provisioner[0]`, rewriting the project policy after the audit config's etag was last recorded;
- the next refresh therefore reports a new etag although the audit configuration is unchanged.

The revoke mode already tolerated exactly this; access mode did not.

**Change:**
- `ETAG_DRIFT_BY_MODE["access"]` is now `{runner_jobs, iamcredentials[0]}`, the same set as revoke;
- the exception reuses the **identical** strict check, `etag_drift_problems`;
- the access report's `accepted_etag_drift` is now sorted;
- nothing else changed.

The pilot-recovery requirements are unchanged: one dataset-policy create, 17 no-ops, strict drift rejection.

**What the audit-config exception guarantees (shared with revoke):**
- **Identity:** the exact address, managed, with type `google_project_iam_audit_config`, name `iamcredentials` and index `0`; actions exactly `["update"]`.
- **Shape:** `before` and `after` have exactly `audit_log_config`, `etag`, `id`, `project` and `service`. Each log-config entry has exactly `exempted_members` and `log_type`.
- **Change:** every non-etag attribute is identical. Even `exempted_members: []` versus `null` is rejected. Both etags are non-empty and differ, and there is no `true` leaf in `after_unknown`.
- **Approved values on both sides:**
  - service `iam.googleapis.com`;
  - this project;
  - log types exactly `ADMIN_READ` and `DATA_READ`;
  - no exemptions;
  - a non-empty `id`.
- **At most one entry per address.** Any other drift address is rejected, including the conditional grants, roles and service accounts.
- **Everything else in access mode still applies:**
  - the audit config and `runner_jobs` stay `no-op` in `resource_changes`;
  - exactly 2 conditional creates, ≤ 8 h, for the single named operator;
  - the variables are checked;
  - forbidden text is rejected.
- **Modes:** `initial`, `recovery` and the pilot modes still tolerate no drift.

**Tests:** 55 new tests in `tests/test_access_window_plan_check.py`, replacing the old "access rejects audit drift" test.
- **Positive:** access plans with no drift, runner only, audit only, or both in either order.
- **36 revoke-suite audit and drift-set mutations, applied to access plans:** service, log types, exemptions, keys, `id`, project, etag, null sides, nested unknowns, actions, index, name, type, mode, duplicates, runner changes, `provisioner[0]` or impersonation drift, role drift, deletes in the changes.
- **10 access-specific cases with both drifts present:**
  - access disabled;
  - an unconditional grant;
  - the impersonation member changed, or its role widened;
  - the audit config updated in the changes;
  - an audit service, log-type or exemption change;
  - the wrong index;
  - a third drift entry.
- **No regressions:** all 20 earlier access adversarial cases still fail with both drifts present.
- **`[]` versus `null`:** rejected in both access and revoke.
- **Other modes:** the tolerance is confirmed absent from initial and recovery.

**Mutation check:** 10 mutations, all detected:
- access reverted to runner only;
- `provisioner[0]` tolerated in access;
- audit tolerated in initial;
- duplicates accepted;
- exemptions unchecked;
- log types unchecked;
- audit values unchecked;
- a log-config change ignored;
- index unchecked;
- an unchanged etag accepted.

"Log-config change ignored" survived at first, because both sides were already pinned to approved values. The `[]` versus `null` test now covers it. The file was restored byte-identical.

**Remaining risks of this exception:**
- **A new etag proves only that the project policy was rewritten.** Pre-check A2 (the live project policy baseline) and the post-apply read-backs remain mandatory.
- **The exception widens access-mode acceptance** only for etag churn on two resources whose security-relevant values are pinned on both sides. Any schema change fails closed.
- **Regenerate the plan.** The rejected access plan is not reused; approve the regenerated plan by its new SHA-256.

## Recovery-checker corrections for observed refresh representations (M6T)

**Observed (operator saved-plan inspection):** the pilot recovery plan proposed exactly 1 create and 17 unchanged resources, but `--mode pilot-recovery` reported 12 problems. The access window is active until 2026-11-10 14:50 UTC. This task made no cloud or Terraform call and did not touch custody files.

### 1. Table IAM `table_id` representation (3 problems)

**What the plan showed:** after refresh, the provider reports `google_bigquery_table_iam_member.table_id` in its fully qualified form, `projects/ebank-pilot-dev-261031-k7m4/datasets/bankpilot_dev/tables/<table>`.

**Why the checker failed:** it derived the runner's role from `table_id`. A fully qualified id never matched the reader-table names, so `accounts`, `customers` and `curated_transactions` were expected to carry the `tables` role instead of `readers`. The other four tables passed by coincidence.

**Fix:**
- the table is now taken from the **instance key** in the address (`runner["<table>"]`), which must be one of the 7 pilot tables;
- `table_id` must be exactly that table, bare (the configured value, on create) or as that exact fully qualified path for **this** project and dataset;
- `dataset_id`, if present, must be `bankpilot_dev`;
- the member must be the runner, with the role derived from the key (`readers` for the 3 reader tables, `tables` otherwise), and with no condition.

### 2. Refresh metadata drift (9 problems)

**What the plan showed:** `resource_drift` held exactly 9 `update` entries:
- the dataset and the 7 tables, where only `etag`, `last_modified_time` and `resource_tags` (`null` → `{}`) differ;
- the bucket, where only `updated` differs.

**Fix:** `pilot_metadata_drift_problems`, used **in `pilot-recovery` mode only.** An entry is accepted only if all of the following hold:
- **Address:** exactly one of those 9 managed addresses, with the matching type, managed mode and at most one entry per address. Any other address is rejected, including the table IAM members, the bucket policy, the dataset policy and unknown tables.
- **Shape:** actions exactly `["update"]`; `before` and `after` are objects with **identical attribute sets**; no `true` leaf in `after_unknown`; at least one attribute differs.
- **Only these attributes may differ:**
  - BigQuery dataset and tables: `etag`, `last_modified_time`, `resource_tags`;
  - bucket: `updated`.

  Schema, labels, location, deletion protection, dataset `access`, bucket security settings and everything else must be **identical**, so a security change cannot ride along with the metadata.
- **Approved transitions:**
  - `etag`: non-empty strings. For BigQuery, `etag` and `last_modified_time` must change **together**, since a modification bumps both. `resource_tags` changing alone (`null` → `{}`, a provider representation change) is accepted.
  - `last_modified_time`: positive integer epoch milliseconds (booleans and strings rejected), strictly increasing, and not later than the plan `timestamp` plus 5 minutes.
  - `resource_tags`: exactly `null` → `{}`. Any tag, or `{}` → `null`, is rejected.
  - bucket `updated`: an RFC 3339 timestamp with a zone, strictly increasing, with the same upper bound. A missing plan timestamp is rejected.
- **Unchanged rules:** the 17 resources must still be `no-op` in `resource_changes`, and their refreshed values must still pass every existing check, alongside the dataset-policy D1 rule and variable checks. The report lists the accepted entries as `accepted_metadata_drift`.
- **Mode isolation:** `pilot` (fresh deployment) and all bootstrap modes still reject this drift. The bootstrap etag exceptions are unchanged and disjoint from these addresses.

**Evidence still limited:** the transitions are modelled on the operator's description of the saved plan, not on the plan file, which this task could not read. If the real values use another representation, the checker **fails closed** and names the attribute. Examples: `last_modified_time` as a string, `updated` with nanoseconds or no zone, or another attribute differing. Report that message rather than relaxing the rule.

### Tests

80 new tests in `tests/test_pilot_recovery_plan_check.py` (163 in the file). The recovery fixture now uses the observed fully qualified `table_id`.
- **Table identity, positive:** for 3 reader tables and 1 writer table, both the fully qualified and the bare forms pass.
- **Table identity, 16 rejected:**
  - another project's, dataset's or table's path;
  - another table's bare id;
  - a path suffix, a `//bigquery…` prefix, an upper-cased path, a missing `table_id`;
  - another `dataset_id`;
  - a reader widened to `tables` or `dataEditor`, or a writer narrowed;
  - a conditional binding;
  - a provisioner or user member, another project.
- **Metadata drift, positive:** all 9 observed entries pass, with `accepted_metadata_drift` = 9, 1 create and 17 unchanged. Subsets, no drift, and a `resource_tags`-only change also pass.
- **Metadata drift, 41 rejected:**
  - **hidden security changes:** table schema, protection or labels; dataset location, `access` or labels; bucket public access, uniform access, versioning, location or labels;
  - **bad transitions:** tags added, `{}` → `null`, timestamps backwards, an etag without a timestamp change or the reverse, string or bool timestamps, timestamps after the plan, an empty etag, bucket `updated` backwards, in the future, malformed or without a zone;
  - **shape and identity:** an attribute added or missing, unknown values, a null `after`, delete, replace, create or no-op actions, the wrong type, data mode, no actual change, a duplicate entry;
  - **wrong addresses:** drift on a table member, the bucket policy, the dataset policy or an unknown table;
  - a missing plan timestamp.
- **Isolation:** `pilot` and every bootstrap mode reject the drift, and the address sets are disjoint from the bootstrap exceptions.
- **Direct tests:** the helper's messages, and the epoch-millisecond parser rejecting bool, 0, negative, float, string and null values.

**Mutation check:** 18 mutations, all detected. The files were restored byte-identical.
- **Table identity:**
  - any `table_id` accepted;
  - reader role ignored;
  - table-member dataset unchecked.
- **Drift:**
  - drift tolerated in `pilot` mode;
  - duplicates accepted;
  - non-metadata changes accepted;
  - attribute sets unchecked;
  - unknowns accepted;
  - empty drift accepted;
  - etag and timestamp decoupled;
  - any `resource_tags` change accepted;
  - timestamps allowed backwards;
  - future timestamps accepted;
  - bool timestamps accepted;
  - any drift action accepted;
  - type and mode unchecked;
  - an empty etag accepted.

"Bool timestamps" survived the first pass because `True` behaves as 1 ms and already fails the forward check; a direct parser test now catches it.

### Remaining risks

- **The checker models the described plan, not the file.** A differing real representation fails closed and needs a new reviewed change, not a relaxed rule.
- **A timestamp upper bound only proves plausibility,** not why the resource changed. The metadata churn is consistent with the failed dataset-policy write and the provider refresh, but the cause is not proven offline. All non-metadata attributes being identical is the security guarantee.
- **The plan must be regenerated** and approved by its new SHA-256. With the window ending at 14:50 UTC, do not start an apply that cannot finish, back up and revoke in time. Expiry remains the hard stop.

## Validation

| Check | Result |
| --- | --- |
| Full `pytest` | **1170 passed, 1 skipped** |
| `make gcp-offline-check` | **926 passed, 1 skipped**; `terraform fmt` passed |
| `ruff check .` | Passed |
| `terraform fmt -check -recursive` | Passed |
| Docs validator, `git diff --check` | Passed |

`terraform validate` was **not** run, because no Terraform operations were authorised for this step. The HCL is checked by `fmt`, by an `hcl2` parse (an existing test) and by the static tests above. The first operator `validate` or `plan` after commit will confirm it.

## Recovery-only gates (none executed)

1. **G1, D1 decision:** **approved.**
2. **G2:**
   - review and commit this change, with CI green;
   - the operator adds the `dataset_owner_member` line to the custody `inputs/pilot-dev.tfvars`;
   - run `preflight --root pilot-dev --phase run` and `run --root pilot-dev -- validate` (offline).
3. **G3, read-only pre-check:**
   - the dataset's current access (expect the creator OWNER and the special-group defaults, nothing unexpected);
   - no bucket, dataset, table or grant change since serial 19;
   - the M6S pre-activation checks A1–A5.
4. **G4, access window:** M6S runbook B, with a short expiry (one operation).
5. **G5, recovery plan:**
   - the plan uses the pilot inputs with the new line;
   - Terraform reports **`1 to add, 0 to change, 0 to destroy`**;
   - the checker, run as `show -json "$P" | python3 scripts/bootstrap_plan_check.py --mode pilot-recovery --dataset-owner=user:ibraheemolasupo@yahoo.com`, gives `ok: true`, `creates 1`, `unchanged 17`, `dataset_owner_approved: true`;
   - approve the SHA-256, then apply;
   - immediately back up the pilot state (upload and `verify-download`).
6. **G6, verify:** read back the dataset access. It must hold exactly `ibraheemolasupo@yahoo.com` as OWNER and the runner with the metadata role: no provisioner, no special groups. The tables and table grants are unchanged.
7. **G7, revoke:** M6S runbook E (`--mode revoke`, bootstrap backup, E5 read-back).

**Failure handling:**

| Situation | Action |
| --- | --- |
| The recovery apply fails again | Authoritative writes are atomic, so the dataset keeps its prior access. Back up, revoke, and stop. A second error (for example, the custom metadata role rejected in dataset access) needs a new design review; never widen roles in the window |
| The plan shows a policy replace (tainted), any drift, or any other change | Do not apply. Investigate against serial 19 |
| Applied, but the read-back differs | The owner corrects it in the Console. Re-plan: drift then shows, and must be resolved before any further apply |
| The window expires mid-task | The hard expiry ends provisioner access. Revoke and plan a new window |

**Rollback:** never `destroy` the dataset policy. An empty authoritative policy would also have no owner, and the wrapper refuses `destroy` anyway. Roll back with a reviewed corrected policy, or by owner correction in the Console. Serial 19 is the pre-recovery restore point.

## Residual risks (pre-recovery, historical)

These were the risks before the recovery. Items 1 and 2 were resolved by the live result; see [Operational closure](#operational-closure-operator-verified-2026-11-10).

1. **Unresolved live verification:**
   - whether `roles/bigquery.dataOwner` set through the provider's dataset IAM path satisfies BigQuery's direct-owner requirement (expected: the provider maps it to dataset `OWNER`);
   - whether BigQuery accepts the **custom** metadata role in dataset access.

   Neither can be proven offline. They are observed only at G5 apply and G6 read-back.
2. **A dormant creator OWNER** (the provisioner) remains on the dataset until G5 succeeds.
3. **Personal owner identity:** a named personal account owns the dataset, so a change of operator requires a reviewed apply.
4. **The owner email is recorded in this repository document** at the operator's request. Terraform code and examples use only a variable and placeholders; the real value lives in the custody inputs.
5. **Checker trust:** the checker relies on the `--dataset-owner` string. The approval record above fixes it, and the checker also requires the plan variable to match.
6. **Carried over:** wrapper bypass, text-based checks, an unrestricted Owner (P8).

At the time of implementation the deployment was incomplete (17 of 18), and the next gate was G2. Both are superseded by the closure below.

## Operational closure (operator-verified 2026-11-10)

Everything in this section was **performed and reported by the operator**; the assistant did not observe it. This update was written offline on 2026-11-12, with no cloud, Terraform, IAM or custody-file access.

### Gate outcomes

| Gate | Evidence reported by the operator | Result |
| --- | --- | --- |
| G2 | Commit `96b51d7` on `main`; GitHub CI successful | Passed |
| G5, recovery apply | Saved recovery plan applied: **1 BigQuery dataset IAM policy created, 0 changed, 0 destroyed** | Passed |
| Pilot inventory | **All 18 pilot Terraform resources deployed**: bucket and bucket policy; dataset `bankpilot_dev` and its policy; 7 tables; 7 runner table grants | Complete |
| G6, dataset access read-back | Only the **named user as OWNER** and the **runner with the custom metadata role**. No provisioner creator OWNER and no special-group defaults | Passed |
| G7, revoke | Bootstrap-root revoke apply: **2 destroyed**, both successfully: `google_project_iam_member.provisioner[0]` (the expiring provisioner role grant) and `google_service_account_iam_member.operator_impersonation[0]` (the operator's token-minting grant on the provisioner). A subsequent live IAM read-back confirmed **both are absent** | Passed |
| Post-revocation check | Impersonation of the provisioner **denied** | Passed |
| Audit | **8 successful `GenerateAccessToken` IAM Credentials audit events** observed during the access window | Observed; target identity not mapped (limitation 1) |
| Pilot state backup | Encrypted backup at **serial 21** authenticated and verified after a OneDrive round-trip | Verified |
| Bootstrap state backup | Encrypted backup at **serial 26** authenticated and verified after a OneDrive round-trip | Verified |
| Access-window input | The temporary access-window input was removed | Done |

### Pre-recovery risks resolved by the live result

- **Direct ownership:** `roles/bigquery.dataOwner` set through the provider's dataset IAM policy **satisfied** BigQuery's direct-owner requirement; the read-back shows the named user as `OWNER`.
- **Custom role:** BigQuery **accepted** the custom metadata role in dataset access.
- **Dormant creator OWNER:** the provisioner's creation-default OWNER entry was **removed** by the authoritative policy.
- **Audit event generation ([M6R](m6r_validation.md) open item 1):** token-minting entries now **appear** in the Data Access logs. Only the mapping of the target identity remains open (limitation 1).

### Outstanding limitations

1. **Audit identity not mapped.** The 8 records are **successful IAM Credentials `GenerateAccessToken` audit events**. They identify the target service account only by a **numeric resource identifier**, which has **not been independently matched** to the provisioner account. They are therefore not, on their own, evidence of which account the tokens were minted for, and the count is not reconciled against the operations in the window.
2. **Operator-reported evidence.** The apply output, read-backs, denial and backup verifications were reported by the operator. Raw plans, state and logs stay in custody and are deliberately not in this repository.
3. **No data and no workload.** The 7 tables are empty. No object upload, load job, query, mart build or cloud reconciliation has run (Gate C has not started). **No cloud data ingestion is claimed.**
4. **Not production-ready.** This is one synthetic, single-region dev pilot in a project with no Organization. It has no VPC Service Controls, CMEK, organisation policies, group-based ownership, alerting or incident response, and no hard spending cap: **budget alerts are not caps**.
5. **Personal owner identity:** a named personal account owns the dataset, so a change of operator requires a reviewed apply. The owner email is recorded in this document at the operator's request; Terraform code and examples use only a variable and placeholders.
6. **Checker trust:** the checker relies on the approved `--dataset-owner` string and on the refresh representations observed on 2026-11-10. A different representation fails closed and needs a reviewed change.
7. **Carried over:** wrapper bypass, text-based checks, an unrestricted project Owner (P8), and the resource-IAM escalation paths of the provisioner role, which is revoked but still defined.
8. **Standing resources:** the bucket, dataset and tables persist, under deletion protection, until a separately approved Gate D cleanup.

### Closure checks (offline, 2026-11-12)

| Check | Result |
| --- | --- |
| Full `pytest` | **1170 passed, 1 skipped** |
| `make gcp-offline-check` | **926 passed, 1 skipped**; `terraform fmt` passed |
| `ruff check .` | Passed |
| `terraform fmt -check -recursive` | Passed |
| Docs validator (`scripts/validate_docs.py`) | Passed: 6 documents, 5 diagrams, 0 issues; `final_project_check.py` 79 checks, 0 issues |
| `git diff --check` | Passed |

No Terraform `init`, `validate`, `plan` or `apply` was run for this documentation update.

## Next milestone: M7

**M7: offline preparation for a controlled synthetic GCS-to-BigQuery ingestion** (Gate C, steps 1–3 of the [pilot runbook](../gcp_pilot_plan.md)). M7 prepares, but does not execute:
- the exact M2/M3 synthetic run manifests and hashes, row counts and per-currency totals for the three input tables;
- create-only, run-prefixed object names and an upload inventory within 5,000 transactions and 10 MiB;
- three strict load-job configurations (explicit schemas, no autodetection, `CREATE_NEVER`, `WRITE_EMPTY`, max bad records 0) with idempotent job IDs;
- the temporary load-grant design and its plan checker, the job and byte ledger, and post-load verification against M3;
- tests and an approval packet.

Any upload, load or query needs a separately approved access window. M7 itself starts no cloud work.

**M6T closed.** All 18 pilot resources are deployed with empty tables, temporary access is revoked, and both state backups are verified. Next: M7 offline preparation.
