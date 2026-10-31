# M6B project and deployment preflight — local preparation complete

2026-10-30; starting revision `c87380b` (`infra: validate GCP Terraform provider configuration`), initially clean. Reviewed existing M5 modules/IAM/controls, seven schemas, transactional SQL, environment examples and approval runbook; reused M6A's successful provider validation. No infrastructure or application changes. This document prepares an approval; it is not a deployment authorization.

## Local observations and unresolved inputs

Read only selected local SDK configuration fields and credential-database **account IDs**, never credential/token columns. No gcloud command, login, credential refresh, ADC discovery through a credential library, metadata-server request or GCP API call was executed. Public reference pages and installed SDK source were consulted for request syntax.

| Prerequisite | Actual observation / decision still needed |
| --- | --- |
| Installed tools | gcloud **536.0.1** and bq under `/opt/homebrew/share/google-cloud-sdk/bin/`; no installation needed |
| Project | Active local `default` configuration names **`virtual-rarity-470405-c5`**. This is a candidate only; existence, ownership, dedicated-sandbox suitability and pilot approval are **unverified**. No concrete tfvars or `TF_VAR_*` inputs found |
| Operator account | Configured and locally cached account ID **`ibraheemolasupo@yahoo.com`**. Credential validity, effective permissions and authorization to act are unknown |
| ADC | No standard ADC file, `GOOGLE_APPLICATION_CREDENTIALS` override or `.bigqueryrc` found. gcloud's cached login is not proof of Terraform ADC availability. Any required ADC setup/impersonation credential minting needs separate explicit approval; no key files proposed |
| Region | Terraform permits only **`europe-west2`** for bucket/dataset. Local compute default is **`us-east1`**, zone `us-east1-b`; do not inherit it or silently change IaC. Confirm London; explicitly name project/location on future calls |
| Billing | Actual billing account ID, existing project link, account-open/billing-enabled state, currency, billing administrator and alert recipients are **unknown**. Require an already authorized existing billing arrangement; missing linkage/billing must return for separate approval |
| Provisioner | Exact existing same-project service-account email, enabled status, bootstrap role/bindings, operator token-mint permission, reviewer and expiry are **unknown**. Example `foundation-provisioner@approved-sandbox-project...` is not a real selected identity |
| Names/environment | `dev` and base `fincrime-m5` are defaults, not confirmed deployment inputs. If accepted: dataset **`fincrime_m5_dev`**; runner **`fincrime-m5-dev-runner@<approved-project>.iam.gserviceaccount.com`**; custom-role IDs **`fincrime_m5_dev_{jobs,metadata,objects,readers,tables}`**. Actual globally unique bucket name is **unknown**. No name availability checked |
| State | No backend/cloud block or state file; M6A only initialized provider/modules. Proposed first-pilot strategy: sole operator, encrypted restricted local volume, root's actual default **`infrastructure/terraform/terraform.tfstate`**, restrictive creation umask, encrypted backups and one exclusive execution window. Approve owner, encryption evidence, backup location and access/retention before planning/apply. Separate state/execution directory required for another environment; no remote state resource is added |

The user was asked to confirm the candidate project and supply provisioner/bucket values during this review. No answer was received before this document was prepared; missing values remain unresolved. Do not use example values as approval.

## Read-only verification package — prepared, not executed

Requires a separate approval naming project, reader account, exact requests and private evidence location. Cached credential use/refresh and any impersonation must be explicitly covered; login/ADC setup, IAM/bootstrap changes, API enablement and Terraform planning are separate permissions. Do not print tokens, use HTTP debug logging, accept enable/install prompts or auto-widen roles. A denied/disabled/unavailable response stops that check; **403 is not proof of absence**.

API prerequisites to inspect: `bigquery.googleapis.com`, `storage.googleapis.com`, `iam.googleapis.com`, `iamcredentials.googleapis.com`, `cloudresourcemanager.googleapis.com`, `serviceusage.googleapis.com`. Billing verification uses `cloudbilling.googleapis.com`; budget listing uses `billingbudgets.googleapis.com` only if already available and separately authorized. These are control-plane checks, not additional application resources. No missing API may be enabled by this preflight.

The following are templates, not a runnable deployment script. All variables must be explicitly supplied after approval; `PROJECT_NUMBER` and `BILLING_ACCOUNT_ID` must come from verified responses. `ROLE_ID` ranges over exactly the five proposed IDs above. Run bq under the expressly selected reader account; do not rely on its default identity.

```sh
# Project existence/ACTIVE state, number/parent and billing linkage.
gcloud projects describe "${PROJECT_ID}" --account="${REVIEW_ACCOUNT}" --format=json
gcloud billing projects describe "${PROJECT_ID}" --account="${REVIEW_ACCOUNT}" --format=json
gcloud billing accounts describe "${BILLING_ACCOUNT_ID}" --account="${REVIEW_ACCOUNT}" --format=json
# Project/ancestor allow policies; authorized security review must also cover deny/PAB/conditions.
gcloud projects get-iam-policy "${PROJECT_ID}" --account="${REVIEW_ACCOUNT}" --format=json
gcloud projects get-ancestors-iam-policy "${PROJECT_ID}" --account="${REVIEW_ACCOUNT}" --format=json
# Enabled services: compare returned names with the explicit prerequisite list.
gcloud services list --enabled --project="${PROJECT_ID}" --account="${REVIEW_ACCOUNT}" --format=json
# Provisioner must exist/enabled; its operator impersonation binding must be account-scoped.
gcloud iam service-accounts describe "${PROVISIONER_EMAIL}" --project="${PROJECT_ID}" --account="${REVIEW_ACCOUNT}" --format=json
gcloud iam service-accounts get-iam-policy "${PROVISIONER_EMAIL}" --project="${PROJECT_ID}" --account="${REVIEW_ACCOUNT}" --format=json
# Fresh-resource conflicts: all proposed names must be reviewed, never adopted/imported.
gcloud storage buckets describe "gs://${BUCKET_NAME}" --project="${PROJECT_ID}" --account="${REVIEW_ACCOUNT}" --format=json
CLOUDSDK_CORE_ACCOUNT="${REVIEW_ACCOUNT}" bq --project_id="${PROJECT_ID}" --location=europe-west2 --format=prettyjson show "${PROJECT_ID}:${DATASET_ID}"
gcloud iam service-accounts describe "${RUNNER_EMAIL}" --project="${PROJECT_ID}" --account="${REVIEW_ACCOUNT}" --format=json
gcloud iam roles describe "${ROLE_ID}" --project="${PROJECT_ID}" --account="${REVIEW_ACCOUNT}" --format=json
# Optional: existing pilot-filtered alerts only, under separately approved billing read access.
gcloud billing budgets list --billing-account="${BILLING_ACCOUNT_ID}" --account="${REVIEW_ACCOUNT}" --filter="budgetFilter.projects=projects/${PROJECT_NUMBER}" --format=json
```

For any existing bucket/dataset, stop the fresh-name workflow; obtain metadata/IAM through a separately approved inspection rather than adopting it. No need to read table data or submit a BigQuery job for conflicts. Require positive authorized absence evidence where available; bucket global uniqueness and hidden resources cannot be proven by a permission-denied response. Service-account/custom-role deleted-name reuse restrictions also require review.

Permission probes are read-only **POST** requests, prepared for an approved authenticated client without printing its token:

```http
POST https://cloudresourcemanager.googleapis.com/v3/projects/APPROVED_PROJECT_ID:testIamPermissions
Content-Type: application/json

{"permissions": ["resourcemanager.projects.get", "resourcemanager.projects.getIamPolicy", "serviceusage.services.list", "serviceusage.quotas.get"]}

POST https://iam.googleapis.com/v1/projects/APPROVED_PROJECT_ID/serviceAccounts/APPROVED_PROVISIONER_EMAIL:testIamPermissions
Content-Type: application/json

{"permissions": ["iam.serviceAccounts.getAccessToken"]}

GET https://serviceusage.googleapis.com/v1beta1/projects/VERIFIED_PROJECT_NUMBER/services/bigquery.googleapis.com/consumerQuotaMetrics?view=FULL
```

Repeat quota GET for Storage and IAM, retain dimensions/overrides and follow **all** pagination. Record query-byte and concurrent-job limits, load/DML limits, table updates, bucket/object operations, custom-role/service-account headroom and relevant London dimensions. Not every service limit or live usage counter is exposed by this endpoint: obtain administrator evidence for missing limits/remaining usage; absence of a metric is not unlimited capacity. It requires `serviceusage.quotas.get`. SDK 536.0.1 exposes the corresponding CLI under **`gcloud alpha services quota list --consumer=projects/NUMBER --service=SERVICE`**; do not install an alpha component for this task. [Quota API](https://docs.cloud.google.com/service-usage/docs/reference/rest/v1beta1/services.consumerQuotaMetrics/list).

Permission tests evaluate the **calling identity**, not an arbitrary email in the request. Test the operator's token-mint permission on the named service account without minting a token; test the full `includedPermissions` array from [the provisioner candidate](../../infrastructure/iam/provisioner.role.json) only under separately approved provisioner credential use. Require the complete requested grant set, not merely HTTP success. Project-level tests do not establish future resource-local rights, resolve every IAM condition or prove provisioner operations will succeed. Security review must include inherited access, deny/PAB policies and custom-role permission support. [Project probe](https://docs.cloud.google.com/resource-manager/reference/rest/v3/projects/testIamPermissions), [service-account probe](https://docs.cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/testIamPermissions). Billing-info GET requires `resourcemanager.projects.get`; account/budget/ancestor reads can require separate scoped read permissions that are not default provisioning grants. [Billing reference](https://docs.cloud.google.com/billing/docs/reference/rest/v1/projects/getBillingInfo).

## Inventory and findings

| Existing Terraform resource blocks | Instances |
| --- | ---: |
| `module.identity.google_service_account.runner` | 1 |
| `module.identity.google_project_iam_custom_role.{jobs,metadata,objects,readers,tables}` | 5 |
| `module.identity.google_project_iam_member.jobs` | 1 |
| `module.storage.google_storage_bucket.artifacts`, `google_storage_bucket_iam_policy.runner` | 2 |
| `module.bigquery.google_bigquery_dataset.foundation`, `google_bigquery_dataset_iam_policy.metadata` | 2 |
| `module.bigquery.google_bigquery_table.products[...]` | 7 |
| `module.bigquery.google_bigquery_table_iam_member.runner[...]` | 7 |
| **Total** | **25** |

Tables: `curated_transactions`, `customers`, `accounts`, `transaction_daily`, `customer_daily`, `account_daily`, `operations_daily`; REQUIRED-field counts respectively **20, 8, 9, 8, 9, 8, 10**. Source schemas/column order, INT64 money, UTC dates/timestamps and all four explicit INSERT targets remain aligned. SQL still contains four whole-mart DELETEs inside a transaction and unrendered `PROJECT.DATASET` identifiers: workload authorization is separate from infrastructure creation.

- **No configuration defect found or fixed.** M6A provider/schema validation remains the applicable evidence; local checks confirm unchanged safeguards. No broad predefined project role, public grant, API/billing resource or credential is in the 25-instance configuration.
- **High privilege:** provisioner `resourcemanager.projects.setIamPolicy` combined with custom-role creation/update can grant or amplify privileges. Resource-local IAM write permissions also permit escalation. Necessary IAM automation is not a security boundary against the provisioner. Require a verified dedicated sandbox, exact expiring grants, audit/reviewer and revocation; do not approve these rights for a shared project without redesign review.
- **Candidate excess to challenge:** project-wide `iam.roles.list`, `iam.serviceAccounts.list`, `storage.buckets.list`, `bigquery.tables.list` expose broader metadata than exact-name checks. Retain only if provider/bootstrap calls demonstrate a need. No cloud trace exists to justify removing or asserting necessity today; no roles were changed. Preflight-reader quota/billing/ancestor access must not be added automatically to the provisioner.
- **Destructive access despite guards:** runner can replace all four marts through `updateData`, create bucket objects and submit chargeable jobs project-wide. Its three inputs are read-only. IAM does not enforce job/byte/upload allowances. Temporary provisioner upload/load/data grants are broader than normal runtime and require separate expiry/revocation evidence.
- **Authoritative policies:** bucket/dataset policies replace local bindings and can remove others' access; fresh dedicated resources only. They do not cancel inherited grants. Creation may temporarily install default/creator access before authoritative policy convergence; load no data until effective IAM is verified.
- **Deletion/retention:** `prevent_destroy` covers data resources/account/custom roles, table deletion protection is on, `force_destroy=false`, `delete_contents_on_destroy=false`, no destructive lifecycle/expiry. These do not block administrator APIs, DML or every IAM change. Versioning and seven-day soft delete preserve potentially chargeable bytes; cleanup deletion permissions remain a separate candidate add-on.

## Bounded approval checklist and next gate

1. **Confirm inputs:** candidate project or another existing ID; `europe-west2`; environment/base; exact bucket/provisioner/derived names; billing administrator/account/currency/recipients; state owner/encryption/backup/reviewer/access expiry. Select short-lived authentication; missing ADC does not authorize creating it.
2. **Approve only named read-only preflight:** reader identity, allowed project/billing/metadata/IAM/quota requests and credential use, output redaction/storage, inheritance review and timing. No data reads, jobs, bootstrap grants, API enablement or plan yet. Stop on missing permissions, disabled services or name conflicts.
3. **Review prerequisites/cost:** verify project ACTIVE, existing billing enabled/account open, no conflicting names, supported custom permissions, effective IAM and quota headroom. Carry forward the illustrative **~US$0.16/month** scenario from M5, not a London quote. Propose **US$1 workload allowance** and **US$5 monthly project-filtered alerts** at 50/90/100% actual and 100% forecast with verified existing billing recipients. Regional rates, currency/tax, egress/free-tier eligibility require manual repricing. **Budget alerts are not hard spending caps.** Any budget creation/modification or missing API/bootstrap change requires a separate enumerated approval.
4. **Separate saved-plan approval:** after successful preflight/authentication approval, separately authorize producing a saved plan using exact inputs, existing Google **7.31.0** lockfile, approved state custody and code/schema hashes. Review binary and JSON, SHA256/expiry, identity, IAM diffs and exactly **25 creates, zero changes/replacements/deletes** for the fresh pilot. No project/API/billing/backend/new services, imports or data jobs. Plan/state/JSON evidence stays private; stop on drift or additional resources. No plan was run by M6B.
5. **First real deployment approval:** approve that specific saved-plan SHA256, full resource/IAM inventory, cost/billing safeguards, operator and execution window. Infrastructure apply and data/workload approval remain separate. Future workload limits: **5,000 transactions; 10 MiB uploads; 100 MiB retained object versions; 100 MiB per applicable query; 20 load/dry-run/parent/child jobs; 10 GiB monthly query allowance; 24 hours**, sequential execution and no automatic increases. Script caps require statement-level accounting. Preserve exact M3/M4 input/SQL/object hashes and seven-table typed reconciliation before any cloud-success claim.
6. **Cleanup approved in advance, executed separately:** identify owner, evidence retention and all live/noncurrent generations; inspect day seven; revoke temporary IAM; separately approve deletion add-on, guard changes and exact saved destroy plan after exports. Keep `force_destroy`/dataset recursive deletion disabled. Account for Storage soft delete and BigQuery time-travel/fail-safe/delayed charges; retain alerts until reconciled. Never delete the project or disable shared APIs/billing as a shortcut. [Existing operational gates](../gcp_pilot_plan.md).

## Executed local checks and stop status

Terraform recursive formatting **passed**; Ruff **passed**; relevant HCL/schema/IAM/inventory/reconciliation tests **55 passed in 3.67s**; all **43** baseline artifact hashes unchanged. Provider/lockfile remain Google **7.31.0**, lockfile SHA256 **`61cea4a18251f6789ce1c68326cc6f0ec7d3209936c7fb7e913a9fdabc1c0fef`**. M6A's provider validation and 299-test full-suite result were not repeated because no implementation/configuration changed. `git diff --check` passed.

Only this M6B evidence document is new/untracked; no tracked file, SDK configuration, Terraform input, state or lockfile changed. Ignored evidence under `outputs/runs/m6b-preflight/`: sanitized `local-gcloud-status.json`, `inventory-review.json`, `local-checks.json`, command logs and final `verification.json`. Cached-account identifiers are evidence, not authorization; tokens/secrets were not read or printed.

**M6B stops at input confirmation and separate read-only cloud-preflight approval.** No authentication, credential mint/refresh, GCP API calls, Terraform plan/apply/destroy, API/billing changes, cloud/workload execution, tool installs, commit or push occurred. Project/billing/IAM/quota results remain unknown. A concrete deployment approval cannot yet be requested because names, identities, state/billing decisions, preflight evidence and saved plan are missing.
