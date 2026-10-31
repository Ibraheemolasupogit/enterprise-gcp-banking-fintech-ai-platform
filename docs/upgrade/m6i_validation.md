# M6I controlled API bootstrap

2026-10-31, revision `c46184a`; explicit target **`ebank-pilot-dev-261031-k7m4` / `203569506404`**. This milestone implements the user's approval for exactly three API activations. It does not authorize identity bootstrap or deployment. [M6H](m6h_validation.md) remains historical pre-activation evidence.

## Actual activation and verification

The existing configured operator matched the previously approved account. Its existing cached access token was valid and used only in process memory, including a child-process token override for gcloud to prevent credential refresh. No token was printed, written to evidence or passed on the command line. File/HTTP logging, update checks, usage reporting and interactive prompts were disabled for that command. No login, ADC, impersonation or configuration/default change occurred. The unchanged default project names the old project; no request targeted it.

Executed at **07:26:41–07:26:47 UTC**, exit **0**, operation reported finished successfully:

```sh
gcloud services enable \
  cloudresourcemanager.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  --project=ebank-pilot-dev-261031-k7m4 \
  --billing-project=ebank-pilot-dev-261031-k7m4 \
  --account=<existing-approved-operator> --quiet --format=json
```

`--billing-project` selects this request's quota consumer; it does not link or modify billing. The installed GA command submits only these three service IDs to Service Usage batch-enable. No other enable command, fallback consumer or force option was used.

| API | Before | After: individual status GET |
| --- | --- | --- |
| `cloudresourcemanager.googleapis.com` | DISABLED | **ENABLED** |
| `iam.googleapis.com` | DISABLED | **ENABLED** |
| `iamcredentials.googleapis.com` | DISABLED | **ENABLED** |

Complete enabled-service listings changed from **23 to 26**: exactly these three additions and **zero removals**. Other pre-existing enabled APIs were left unchanged; their presence is not deployment authorization. BigQuery, Storage and Service Usage remained enabled. Cloud Billing and Billing Budgets were absent from both enabled lists; neither was activated.

Verification completed at **07:28:44 UTC**. Two preflight and fifteen post-activation direct HTTP metadata/permission reads all returned **200**. Gcloud's batch-enable and internal operation polling are separate from those 17 reads. Each direct request carried `x-goog-user-project: ebank-pilot-dev-261031-k7m4` and a resource path targeting this project ID/number. POSTs were limited to non-mutating `getIamPolicy`, `testIamPermissions` and `getEffectiveOrgPolicy`. A sandbox DNS failure before network-approved execution made no successful GCP call.

## Identity, policy and provisioner findings

| Check | Verified result / limit |
| --- | --- |
| Project metadata | **ACTIVE**, number **203569506404**, display name `Enterprise Banking AI Pilot`; no parent field, consistent with **No organization**. No organization/folder ancestors apply to this project |
| Project IAM | One binding: **`roles/owner`**, one user matching the configured operator, **unconditional**. No Editor, public/group/domain/service-account binding observed in this project policy |
| Service accounts | Complete single-page inventory: **zero**. Proposed `bankpilot-dev-provisioner@ebank-pilot-dev-261031-k7m4.iam.gserviceaccount.com` and `bankpilot-dev-runner@ebank-pilot-dev-261031-k7m4.iam.gserviceaccount.com` are absent |
| Custom roles | Complete single-page inventory: **zero**; proposed bootstrap/runtime roles do not exist |
| Bootstrap permission probe | **29 of 37** requested permissions returned, including project IAM read/write, custom-role create/get/list/update, service-account create/get/list/update/getIamPolicy/setIamPolicy, API enable/status/poll, service-use, quota read, deny list and effective-policy read |
| Probe limitations | Eight resource-level permissions were not returned at the **project** test scope: `bigquery.tables.{create,get,list,update}` and `storage.buckets.{get,getIamPolicy,setIamPolicy,update}`. This is not a resource-specific denial finding or proof that the proposed provisioner role will work. No bucket/table exists for an account/resource-scoped execution test here |
| Effective location policy | `constraints/gcp.resourceLocations`: **ALLOW all**. No policy location restriction observed against intended **europe-west2**; this does not establish capacity, allocations or runtime behavior |
| Other effective policies | `iam.allowedPolicyMemberDomains` and `gcp.restrictNonCmekServices`: **ALLOW all**. `iam.disableServiceAccountCreation` and `iam.automaticIamGrantsForDefaultServiceAccounts`: empty boolean policies (enforcement defaults false); neither safeguard was observed enforced |
| Deny / other restrictions | Complete project deny-policy listing: **zero**. Principal access boundaries and other unqueried constraints/restrictions remain unverified; no broad absence claim |

The current operator is capable of the named bootstrap actions but **is not a least-privilege steady-state provisioner**. Owner access remains unchanged. It can override resource access boundaries; later bucket/dataset policies do not remove project-inherited Owner access. Project IAM/custom-role write permissions are escalation-capable, including alteration of grants; an expiry is not a hard security boundary against a malicious privileged principal. No account-scoped token-mint test was attempted against the absent provisioner, and no identity was impersonated. Service accounts have no verified expiry here; future binding expiry and revocation ownership must be explicit.

## Costs, evidence and local checks

Only the approved API activation and metadata/permission reads ran. **No Storage list/object request, BigQuery job/query/load, workload, state or deployed pilot resource was created.** No usage-priced workload was invoked; actual incremental charges/account spend were not independently measured. Enabling APIs grants service availability, not a spending cap, and broad Owner access can still authorize future spend.

Billing linkage and the **GBP £5** project-specific monthly budget with **50/80/100%** alerts remain **user Console-verified in M6G**, not newly API-verified. Budget scope internals, recipients/delivery, forecast rules, spend/credit treatment and current billing-account status remain unverified. The local USD5/50-90-100 control mismatch and separate proposed **US$1** manual workload allowance remain unresolved; infrastructure/control metadata was not changed. **Budget alerts cannot enforce a hard spending cap.** [M6H alignment recommendations](m6h_validation.md) still apply.

Private command results, complete service diffs, raw metadata, request scope ledger and local verification are under ignored `outputs/runs/m6i-api-bootstrap/`, directory **0700**, files **0600**. This repository document includes neither tokens nor full billing identifiers/personal billing details. Provider **Google 7.31.0**, lockfile and infrastructure remain unchanged; no initialization/download occurred.

Local checks: recursive Terraform formatting **passed**; **12 readiness/schema/IAM policy tests passed in 0.49s**; **Ruff passed**; all **43 baseline artifact hashes matched**; configuration/default hashes and cached expiry remained unchanged, with ADC absent. Documentation whitespace/link checks and `git diff --check` passed. No source/configuration defect was identified or corrected; resource-specific permission sufficiency remains a validation gap.

## Next precise approval gate — stop

Approve a **named identity-bootstrap package only**, after reviewing the exact role/binding payloads and operator window:

1. Create only `bankpilot-dev-provisioner` on this project, keyless. Do not create the runtime account yet.
2. Create project custom roles **`bankpilot_dev_provision`** and **`bankpilot_dev_impersonate`**. The former is the [29-permission candidate](../../infrastructure/iam/provisioner.role.json) minus `iam.roles.list`, `iam.serviceAccounts.list`, `storage.buckets.list`, `bigquery.tables.list` (**25 proposed permissions**); the latter contains only `iam.serviceAccounts.getAccessToken`. These proposed changes are not implemented; review sufficiency without automatically widening permissions.
3. Approve exactly two additive bindings: the provisioner role on this project to the named provisioner; the token-mint role **on that service account only** to the explicitly named operator. Specify binding start/end timestamps, revocation owner and verification/revocation procedure. Preserve current project IAM/etag and unrelated bindings; no authoritative project-policy replacement or Owner/Editor/admin/key/billing/API-enable/data/job/delete grant to the provisioner. Do not modify existing Owner access under this gate.
4. Include bounded post-bootstrap reads to verify identities, custom-role definitions, scoped/expiring grants and effective account permissions. **Token minting/impersonation is a separate action requiring explicit authorization**, not implied by reading its policy.

State preparation remains a separate local approval: custodian, verified encryption, encrypted backup/retention/recovery, exclusive execution/locking, explicit local backend and proposed private path `infrastructure/terraform/state/dev/terraform.tfstate`, with directories 0700/files 0600. That path/directory still does not exist; Terraform authentication is not established by this gcloud session. Then approve environment inputs/control alignment and saved-plan generation separately. Any future apply requires approval of the specific saved-plan hash/IAM diff/cost window; workload and cleanup approvals remain separate. No further API activation or Billing/Budget API activation is needed for this minimum bootstrap.

Only `docs/upgrade/m6i_validation.md` was added by M6I; the pre-existing untracked M6H report is preserved. No other cloud change, authentication/ADC/default change, IAM/billing modification, Terraform plan/apply/destroy, state, workload, install, commit or push occurred. **Stopped after M6I verification; no automatic progression.**
