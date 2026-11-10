# M6T BigQuery dataset IAM partial-apply recovery

Date 2026-11-10. Baseline `main` at `dba6d0c`. Project `ebank-pilot-dev-261031-k7m4`, region `europe-west2`.

**Offline implementation only. The pilot deployment is NOT complete:** 17 of 18 resources exist, and the dataset IAM policy has not been applied. M6T did none of the following:
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

## Validation

| Check | Result |
| --- | --- |
| Full `pytest` | **1036 passed, 1 skipped** |
| `make gcp-offline-check` | **792 passed, 1 skipped**; `terraform fmt` passed |
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

## Residual risks

1. **Unresolved live verification:**
   - whether `roles/bigquery.dataOwner` set through the provider's dataset IAM path satisfies BigQuery's direct-owner requirement (expected: the provider maps it to dataset `OWNER`);
   - whether BigQuery accepts the **custom** metadata role in dataset access.

   Neither can be proven offline. They are observed only at G5 apply and G6 read-back.
2. **A dormant creator OWNER** (the provisioner) remains on the dataset until G5 succeeds.
3. **Personal owner identity:** a named personal account owns the dataset, so a change of operator requires a reviewed apply.
4. **The owner email is recorded in this repository document** at the operator's request. Terraform code and examples use only a variable and placeholders; the real value lives in the custody inputs.
5. **Checker trust:** the checker relies on the `--dataset-owner` string. The approval record above fixes it, and the checker also requires the plan variable to match.
6. **Carried over:** wrapper bypass, text-based checks, an unrestricted Owner (P8).

**Stopped after M6T implementation.** The deployment is incomplete (17 of 18). Next: G2.
