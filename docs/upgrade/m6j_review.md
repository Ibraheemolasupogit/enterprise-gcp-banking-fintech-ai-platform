# M6J identity, privilege and state security review

2026-10-31; revision `5b4d423`; target **`ebank-pilot-dev-261031-k7m4` / `203569506404`**, `europe-west2`. **Review only.** No cloud request, authentication, service-account/role creation, IAM grant, impersonation, API enablement, billing change, Terraform init/plan/apply/destroy, state creation, commit or push occurred. Cloud facts below come from [M6I](m6i_validation.md) and [M6H](m6h_validation.md) evidence. They were not re-queried and may have changed since M6I.

## Inputs reviewed

The reviewed sources are [provisioner.role.json](../../infrastructure/iam/provisioner.role.json) (29 permissions) and [cleanup.role.json](../../infrastructure/iam/cleanup.role.json) (6). Also reviewed: the Terraform root/modules, `pilot_controls.json`, [readiness](../gcp_deployment_readiness.md), the [pilot plan](../gcp_pilot_plan.md), [M6D](m6d_design.md), M6H and M6I. Public references read: provider 7.31.0 `bigquery_dataset_iam` docs and Google's [limits on granting roles](https://docs.cloud.google.com/iam/docs/setting-limits-on-granting-roles).

Verified baseline from M6I:
- One project IAM binding: unconditional **`roles/owner`** held by the human operator.
- Zero service accounts, zero custom roles and zero deny policies.
- No organisation.
- `iam.disableServiceAccountCreation` and the automatic default-SA grant safeguard are not enforced.

## Findings

| # | Severity | Finding |
| --- | --- | --- |
| F1 | **High** | **`resourcemanager.projects.setIamPolicy` without a condition makes the provisioner effectively Owner-equivalent.** It can grant itself any predefined role, including `roles/iam.serviceAccountTokenCreator` and the admin roles, or remove its own expiry. Terraform needs this permission for exactly **one** resource: `google_project_iam_member.jobs`, which adds the runner to `bankpilot_dev_jobs`. |
| F2 | **High** | **`iam.roles.update` and `iam.roles.create` let the provisioner rewrite its own custom role** (`bankpilot_dev_provision`) and add any permission that custom roles support. IAM conditions cannot restrict this by role name. Google's documentation also warns that a grant limit is defeated when the limited admin can modify the custom roles in its allowed list. While these permissions are held, **no condition on any other binding is a hard boundary.** |
| F3 | Medium | **Resource IAM writes allow data-plane self-grants and possible exposure.** `storage.buckets.setIamPolicy/update` and `bigquery.datasets/tables.setIamPolicy/update` let the provisioner grant itself or others object/table data access. `buckets.update` can turn off public-access prevention. With domain restriction ALLOW-all, outside principals could be added. The impact is limited because the data is synthetic, but "no data access" in the role description is **not** a technical guarantee. BigQuery datasets ignore `modifiedGrantsByRole` (Google documentation). |
| F4 | **High (context)** | **The Owner binding dominates every control.** The operator is unconditional Owner and the only principal. Owner can grant itself token-creator rights, so the proposed account-scoped `bankpilot_dev_impersonate` binding is an **auditable record of intent**, not a technical restriction for this operator. The binding should not be removed: in a no-organisation project the sole Owner is the only recovery path. The provisioner reduces **token blast radius and accidental change**. It does not limit the human. |
| F5 | **High** | **This workstation has FileVault Off** (`fdesetup status`, read-only local check). The M6D state design requires a "verified encrypted local volume", so that condition **fails**. Data already at rest without full-disk encryption: the existing gcloud user credential store (an Owner refresh token), the private M6H/M6I evidence under `outputs/runs/` and any future state, plans or backups. |
| F6 | Medium | **Revoking a binding does not revoke tokens already issued.** Access tokens from `generateAccessToken` remain valid for up to 1 h by default. Without an organisation, the 12 h lifetime-extension constraint cannot apply. An expired time-conditioned binding remains in the policy until it is removed. |
| F7 | Medium | **Token minting is not logged by default.** `iamcredentials` `GenerateAccessToken` calls are Data Access audit logs, which are off by default. Admin Activity logs (always on) record IAM, role and SA changes. Enabling Data Access logs is itself a project-policy (`auditConfigs`) change and needs approval. A `setIamPolicy` holder can also change `auditConfigs`. This review believes the `modifiedGrantsByRole` limit does not cover that, but it is **unverified**. |
| F8 | Medium | **Required permissions are not fully proven.** The provider documentation does not say whether `google_bigquery_dataset_iam_policy` uses dataset `getIamPolicy/setIamPolicy` or `datasets.get/patch` on the `access` field. It does accept full-form project custom roles, and the candidate role contains both permission sets. M6I's probe could not test the 8 bucket- and table-scoped permissions at project scope. The proven gap remains unproven. **Resolve it from a failed plan or apply, not by adding permissions in advance.** |
| F9 | Low | **The cleanup add-on (Gate D) cannot list object generations.** Deleting "all live and noncurrent generations" needs `storage.objects.list` (with versions), which neither the provision role nor the cleanup role contains. Record this for Gate D. No change is proposed now. |
| F10 | Info | **`pilot_controls.json` still records USD 5 and 50/90/100%,** while the Console budget is GBP 5 at 50/80/100%. This remains unaligned; the [M6H recommendation](m6h_validation.md) stands. It does not affect IAM design. |

Positive findings:
- The provider uses keyless impersonation through `impersonate_service_account`.
- A variable validation enforces that the provisioner and runner are different same-project identities.
- No candidate role contains key-creation, `actAs`, token-minting, API-enable, billing, job or data-read permissions.
- Deletion is a separate add-on.
- The runner's five roles are narrow, with no IAM or schema administration.
- `*.tfstate*`, `*.tfplan` and `*.tfvars` are git-ignored.
- ADC file absent.

## Proposed revised bootstrap package (supersedes M6I's two-role proposal, pending approval)

Split project-IAM write out of the main role so that it can be both time-limited and grant-limited:

| Custom role (project) | Permissions | Binding |
| --- | --- | --- |
| `bankpilot_dev_provision` | **24**: `resourcemanager.projects.get/getIamPolicy`, `serviceusage.services.use`, `iam.roles.create/get/update`, `iam.serviceAccounts.create/get/update`, `storage.buckets.create/get/update/getIamPolicy/setIamPolicy`, `bigquery.datasets.create/get/update/getIamPolicy/setIamPolicy`, `bigquery.tables.create/get/update/getIamPolicy/setIamPolicy` (M6D's 25 **minus** `resourcemanager.projects.setIamPolicy`) | Project → provisioner; condition `request.time < timestamp("<END>")` |
| `bankpilot_dev_project_iam` (new) | `resourcemanager.projects.getIamPolicy`, `resourcemanager.projects.setIamPolicy` | Project → provisioner; condition `request.time < timestamp("<END>") && api.getAttribute('iam.googleapis.com/modifiedGrantsByRole', []).hasOnly(['projects/ebank-pilot-dev-261031-k7m4/roles/bankpilot_dev_jobs'])` |
| `bankpilot_dev_impersonate` | `iam.serviceAccounts.getAccessToken` | **Provisioner SA resource only** → named operator; condition `request.time < timestamp("<END>")` |
| `bankpilot_dev_plan_reader` (optional, steady state) | **10**: `resourcemanager.projects.get/getIamPolicy`, `iam.roles.get`, `iam.serviceAccounts.get`, `storage.buckets.get/getIamPolicy`, `bigquery.datasets.get/getIamPolicy`, `bigquery.tables.get/getIamPolicy` | Replaces the provision roles after apply, for refresh-only drift checks; time-bound per use |

Bootstrap inventory: 1 SA and 3 roles (4 with the optional reader), plus 3 conditional bindings. **It is not counted in the 25 Terraform instances.** All bindings must be added with additive, etag-checked `add-iam-policy-binding` and policy version 3. Never replace the whole policy authoritatively. No Owner change, keys, `actAs`, runner impersonation, or API/billing/data/job/delete rights.

**Residual risk:** F2 remains while `iam.roles.create/update` are held. During the apply window the provisioner could add `setIamPolicy` to its own unconditioned role and bypass the grant limit. The limit therefore guards against mistakes and bugs. It is a hard boundary only once role-write is gone.

Two options:
- **(A) Hardening (preferred long-term).** The Owner creates the five runtime custom roles out-of-band, and Terraform references them. The provisioner then needs only `iam.roles.get`. This changes the identity module and tests, which needs separate code approval.
- **(B) Pilot (recommended now; no code change).** Accept F2 for a short, single-operator apply window under compensating controls: Admin Activity log review for `UpdateRole`, `SetIamPolicy` and `CreateServiceAccountKey`, with the post-apply IAM diff required to equal the plan.

Under B, the operator is already Owner, so the incremental risk is a stolen provisioner token, capped at 1 h, within that window.

## Temporary vs steady-state permissions

| Phase | Holder | Scope / duration |
| --- | --- | --- |
| Bootstrap (create SA, roles, bindings) | Human Owner, directly; unavoidable | Single session; Admin Activity logged |
| Plan / apply of the 25 instances | Provisioner via impersonation: `provision` + `project_iam` | Window per approved gate (suggest ≤ 8 h); re-grant for each gate rather than one long grant |
| Initial CSV load | Provisioner + existing temporary jobs/objects/tables grants ([pilot plan](../gcp_pilot_plan.md)) | Separate approval; revoke before mart execution |
| Steady state | **No standing provisioner write.** Optional `plan_reader`; runner keeps its 5 roles | Runner impersonation is a later, separate binding |
| Cleanup (Gate D) | Separate cleanup add-on, bucket-scoped where possible (+ F9 gap) | Separate approval |

## Operator impersonation, expiry and revocation

- **Authentication without ADC.** Do not create an ADC file, which would store an Owner refresh token. Per session, set `GOOGLE_OAUTH_ACCESS_TOKEN` in the environment from the operator's existing gcloud session, without printing or persisting it. The provider then impersonates the provisioner through its `impersonate_service_account`. Exact commands need source-authentication approval (M6D gate 7).
- **Expiry.** Bind the impersonate role only on the provisioner SA resource, with an explicit UTC `<START>` (the grant time) and `<END>`. Its end time should be no later than the provision bindings' end. Expected support: Google lists service accounts as accepting conditional bindings. **Verify the binding exists afterwards.** If it is rejected, stop rather than grant it unconditionally.
- **Revocation (owner: the operator, as sole Owner):**
  1. Remove each conditional binding with an exact `--condition` match, including expired ones.
  2. If immediate cut-off is needed, also **disable** the provisioner SA. Expect already-issued tokens to fail once the SA is disabled, but confirm this rather than assume it.
  3. Otherwise allow up to 1 h for issued tokens to expire.
  4. Verify through policy read-back and, under a separate approval, a `testIamPermissions` call as the operator on the SA.
  5. List SA keys: only system-managed keys are acceptable.
- **Audit.** Recommend a separate approval to enable Data Access (`DATA_READ`) audit logging for `iamcredentials.googleapis.com` only (F7). Volume is negligible at pilot scale. Pricing is unverified, and the change modifies project policy.

## Terraform state custody, encryption and backup

- **State contents.** State will hold resource names, emails, schemas, IAM policies and etags, but no keys or tokens. Treat it as confidential and integrity-critical: tampering can mislead a plan.
- **Blocker (F5).** Enable FileVault and verify `fdesetup status` = On **before** any state, plan, tfvars or backup exists. This is a local workstation change performed by the user.
- **Location.** Prefer a path **outside the repository**, for example a 0700 directory under the user's home, rather than M6D's in-repo `infrastructure/terraform/state/dev/`. An in-repo path is git-ignored but exposed to repository tooling and agents. Either way, configure it through an explicit `backend "local"` with partial `-backend-config=path=…`, umask 077, directories 0700 and files 0600.
- **Locking.** The local backend's file lock plus a single-operator rule; never run concurrent Terraform; never use `-lock=false`.
- **Backup.** After each successful apply, write an encrypted copy (for example an encrypted APFS disk image or `age`/`gpg` with an offline key) to a separate encrypted medium. Record SHA256 and serial. Retain every post-apply serial until Gate D completes plus 90 days. Test recovery once by decrypting and comparing the hash before relying on it.
- **Backend choice.** Do **not** use a GCS backend in this project. The provisioner's project-wide `storage.buckets.setIamPolicy/update` would let it alter its own state custody.
- **Custodian:** the operator. A backup destination is still unnamed and **blocks state approval**.

## Safe bootstrap sequence (future; not executed)

1. **Pre-checks (read-only, approved).** Confirm the same operator and project; that SAs, roles and deny policies are 0; and that the project policy is unchanged. Save the version-3 policy and etag privately. Local checks: FileVault On; no ADC file.
2. Create `bankpilot-dev-provisioner` (no keys).
3. Create the three roles from reviewed JSON payloads with exact permission sets.
4. Add three conditional bindings, each etag-checked and additive.
5. **Verify.** Policy diff is exactly +3 conditional bindings with the Owner binding unchanged; role definitions match the payloads; SA keys are system-managed only; Admin Activity entries match the steps.
6. Stop. Token minting and Terraform use are later approvals.

**Rollback**, in reverse order:
1. Remove the bindings.
2. Disable, then delete, the SA. A deleted SA can be undeleted for about 30 days.
3. Delete the custom roles. Deleted role IDs stay reserved for about 7–44 days, so to retry soon prefer `undelete` over new IDs.
4. Confirm the policy equals the saved baseline.

**Stop conditions:** any etag conflict, unexpected binding, rejected condition or denial. **Never widen permissions to get past an error.**

## Required approvals and decisions (none granted by M6J)

1. **Option A or B for F2.** Recommendation: B for the pilot, with A recorded as hardening.
2. **The 3-role (+ optional reader) package**, replacing M6I's 2-role proposal, together with exact `<START>`/`<END>` UTC values and the named operator.
3. **Enable FileVault**, then approve a state path outside the repo and name an encrypted backup destination.
4. **Optional** Data Access audit logging for IAM Credentials.
5. **Later, separate repository change:** update `provisioner.role.json` and its tests to the 24-permission split, plus the `pilot_controls.json` currency alignment. This review **changed no file except this one**.

## Offline validation (this session)

| Check | Result |
| --- | --- |
| `make gcp-offline-check` (readiness + reconciliation tests) | **55 passed** in 2.78 s |
| `terraform fmt -check -recursive` (v1.16.5) | Passed |
| `python3 -m ruff check .` | Passed |
| 43 baseline artifact SHA256 | **43/43 match** |
| Role JSONs | provisioner 29 unique, cleanup 6 unique, disjoint (test-asserted) |
| Provider lockfile SHA256 | `61cea4a1…0fef`, unchanged from M6H |
| State dir / `*.tfstate` / `*.tfplan` / real `*.tfvars` | None present |
| ADC file | Absent |
| FileVault | **Off** (F5) |

**Stopped after M6J.** Bootstrap was not started.
