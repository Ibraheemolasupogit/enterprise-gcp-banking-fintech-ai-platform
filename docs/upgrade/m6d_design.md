# M6D dedicated pilot project and cost-safety design

2026-10-31; starting revision `4b8072c`. **Design approval only.** Use a dedicated project for the synthetic enterprise banking pilot; leave `virtual-rarity-470405-c5`, its Owner/Editor grants and existing resources unchanged. This design reuses M5's seven-table/25-instance root and M6A's provider validation. [M6C](m6c_validation.md) established an existing nonempty project, broad access, no dedicated provisioner, and an inaccessible budget configuration; those facts are not evidence about a future project.

## Proposed names and state

| Item | Proposed value / status |
| --- | --- |
| Display name | `Enterprise Banking Pilot Dev` |
| Project ID | **`ebank-pilot-dev-261031-k7m4`** — syntax checked, global availability/reserved-name eligibility unverified |
| Workload region | **`europe-west2`** for both Storage and BigQuery; projects themselves are global |
| Initial environment / base prefix | **`dev` / `bankpilot`**; `test` reserved, not part of initial deployment |
| New artifact bucket | **`ebank-pilot-dev-261031-k7m4-ew2-artifacts`** — syntax checked; global availability unverified; never adopt either old bucket |
| Dataset IDs | **`bankpilot_dev`** initially; **`bankpilot_test`** reserved for a separately approved environment/state, not a second initial dataset |
| Provisioner | **`bankpilot-dev-provisioner@ebank-pilot-dev-261031-k7m4.iam.gserviceaccount.com`** — proposed, not created |
| Runtime | **`bankpilot-dev-runner@ebank-pilot-dev-261031-k7m4.iam.gserviceaccount.com`** — created only by future approved pilot IaC |
| Runtime custom-role IDs | `bankpilot_dev_jobs`, `bankpilot_dev_metadata`, `bankpilot_dev_objects`, `bankpilot_dev_readers`, `bankpilot_dev_tables` |
| State | **`infrastructure/terraform/state/dev/terraform.tfstate`**, on a verified encrypted local volume; reserved test path `state/test/terraform.tfstate` under that root |
| Parent | **Unknown:** select an authorized organization/folder with reviewed inherited access. Explicitly approve `No organization` only if available/appropriate; never let tooling silently choose a parent |

All names are proposals, not reservations or proof of permissions. The project ID is 27 characters and bucket name 41; both and the identities/datasets satisfy current input syntax. Names cannot be silently changed on collision. Initial resources remain one bucket, one dataset and `curated_transactions`, `customers`, `accounts`, `transaction_daily`, `customer_daily`, `account_daily`, `operations_daily`.

State preparation requires an approved sole custodian, encryption evidence, umask 077, directories 0700/files 0600, encrypted backup destination and retention, exclusive execution/locking, and recovery procedure. Backup destination remains unspecified and blocks approval. No remote state bucket is proposed. State, backups, plans and environment inputs stay outside Git; the provider lockfile remains reviewable. Local encryption/backup/access controls have not been verified or prepared.

## Permissions and identity separation

Use named operators with scoped/time-limited custom grants where delegation is needed; reuse existing authorized billing-admin authority only for approved billing actions. Old-project Owner access does not establish project-creation authority in a new parent.

| Phase / scope | Required permission design |
| --- | --- |
| Create project, approved parent | `resourcemanager.projects.create`; approved read/operation inspection. Parent entitlement, project quota and creator/default grants must be checked. No project-delete/move rights requested |
| Link previously unbilled new project | Billing-account `billing.resourceAssociations.create`; new-project `resourcemanager.projects.createBillingAssignment`, with `serviceusage.services.list` / `serviceusage.effectivepolicy.get` as required by the chosen route. No old-project unlink or billing-close rights requested |
| Configure alerts, existing billing account | `billing.budgets.create/get/list`; `update` only for approved revisions; account read permissions `billing.accounts.get/getIamPolicy/getSpendingInformation` as needed. No budget-delete or billing authority goes to workload identities |
| Enable exact APIs, new project | Temporary `serviceusage.services.enable/get/list`, `serviceusage.operations.get`; verify any service-side bind requirement for the selected public APIs. No service-disable permission. Optional lower quota needs separately approved `serviceusage.quotas.get/update` |
| Bootstrap identities/IAM, new project | Temporary `iam.serviceAccounts.create/get/getIamPolicy/setIamPolicy`, `iam.roles.create/get`, project `get/getIamPolicy/setIamPolicy`; update rights only if an approved revision needs them. Create only the named provisioner, approved bootstrap roles and explicit bindings |
| Impersonation, named account only | Custom role containing only `iam.serviceAccounts.getAccessToken`; bind approved operator on provisioner account. Runtime-account impersonation is a separate post-apply binding after that account exists. No keys, signing rights or project-wide impersonation |

Project creation, billing association and budget configuration are separate operations. Billing linkage can require sensitive-action reauthentication; stop for explicit user action/approval if demanded. [Project creation](https://docs.cloud.google.com/resource-manager/docs/creating-managing-projects), [billing-link permissions](https://docs.cloud.google.com/billing/docs/how-to/modify-project), [budget permissions](https://docs.cloud.google.com/billing/docs/how-to/budgets), [Service Usage permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/serviceusage).

**Temporary provisioning:** propose `bankpilot_dev_provision` based on the existing [29-permission candidate](../../infrastructure/iam/provisioner.role.json), removing four broad enumeration permissions from the proposed grant: `iam.roles.list`, `iam.serviceAccounts.list`, `storage.buckets.list`, `bigquery.tables.list`. This **25-permission subset is a proposed bootstrap grant**, not a modified file or proven sufficient deployment role; confirm named-resource provider calls before use and return missing permissions to review. No data/job/delete/API-enable/billing/key rights are added. Use a separate account-scoped `bankpilot_dev_impersonate` custom role for token minting. Provisioner IAM-write/custom-role privileges can escalate; dedicated-project scope reduces impact but does not eliminate that risk. Expire/revoke provisioning and bootstrap bindings after the approved window; separately approve temporary initial-load data/job grants and later revoke them. Review any creator Owner/default-service-agent grants; never copy old-project Editor bindings.

**Steady-state runtime:** preserve the five scoped roles: project job-create/service-use, dataset metadata read, bucket metadata/object create/get/list, read-only access to three inputs, and read/data-update on four marts. No project/billing/IAM/schema administration, API activation, keys, impersonation or object deletion. Mart data-update is destructive through DML and requires separate workload approval. Cleanup uses the existing separate deletion candidate, never a standing runtime grant.

Required application/control APIs: `bigquery.googleapis.com`, `storage.googleapis.com`, `iam.googleapis.com`, `iamcredentials.googleapis.com`, `cloudresourcemanager.googleapis.com`, `serviceusage.googleapis.com`. Billing administration may require `cloudbilling.googleapis.com` and `billingbudgets.googleapis.com` on an explicitly selected supported consumer project. Resolve M6C's unrelated-consumer `SERVICE_DISABLED` error before approving that route; do not enable the error's consumer or alter quota-project/default credentials automatically. No Compute, orchestration, monitoring, Pub/Sub or model service is proposed. Audit any automatically enabled services/service agents before data loading.

The **25 pilot Terraform instances exclude** project creation, billing linkage, APIs, budget, provisioner account, bootstrap custom roles/bindings and any Google-managed service agents. Their separate approval inventories must be explicit; do not describe the whole bootstrap as 25 resources.

## Cost governance and isolation limits

Use the **same existing billing account identified privately in M6C**, only after explicit approval of its exact ID and current eligibility. Prior evidence showed an open account and the configured operator as direct billing administrator, not a blanket authorization to add projects. Reverify account/project-link quotas, allowed billing associations, organization constraints, account currency/eligibility and budget access. Keep billing IDs and personal recipients out of Git. Shared billing does not copy old-project resource IAM, but shares financial liability, credits/free allowances and account-level administration.

Propose a **US$5 monthly alerts-only budget**, filtered by the **new verified project number**, with 50/90/100% actual and 100% forecast alerts. Confirm existing billing email recipients and delivery/configuration; add no notification service. If account currency is not USD, obtain approval for a documented converted US$5 equivalent rather than guessing or using an unsupported currency. State credit treatment and exclusions. **Budget alerts cannot enforce a hard spending cap**, can be delayed, and do not stop jobs.

The **US$1 bounded workload allowance** is a manual stop/review rule, covering planned job/storage/API/evidence/cleanup costs; it is not a credit, reservation or guaranteed ceiling. Before launch, approve a worst-case cost envelope, current London rates and all priced metadata operations. Keep a cumulative request/job/bytes/cost ledger; no unattended or concurrent runs and no automatic retries/limit increases. Stop before launching a job whose worst-case cost would exceed the remaining allowance. Retention/delayed billing can continue afterward.

Carry forward existing limits: **5,000 transactions; 10 MiB uploads; 100 MiB retained object versions; 100 MiB maximum billed bytes per applicable query; 20 requests/jobs including dry runs and script parent/children; 10 GiB monthly query allowance; 24-hour execution window; day-seven storage review**. Estimate SELECTs/DML separately, avoid double-counting script parent totals, and do not assume a whole-script cap. Track metadata operations too: M6C's one potentially billed bucket listing is a separate historical cost, not a free-operation precedent.

As an optional, separately approved defense, target lower project and per-user on-demand query quotas (proposed **1 GiB/day**, subject to supported minimum/units and verification). They are daily query limits, not a dollar/month/storage/API cap; they apply across users and cannot uniquely fence one service account. Do not carry M6C's old-project 200 TiB/day default forward as a safety assumption or change old-project quotas. [Custom query quotas](https://docs.cloud.google.com/bigquery/docs/custom-quotas).

Illustrative M5 arithmetic remains **US$0.15364/month (~US$0.16)** for 10 GiB queries, 1 GiB BigQuery storage, 100 MiB versioned Storage and 100 operations of each class, using assumed rates of US$10/TiB, US$0.05/GiB-month and US$0.01/US$0.001 per 1,000 class-A/class-B operations. This excludes transfer/tax/other activity and is **not a verified London quote**. New-project pricing/free-tier status, actual costs and the US$1 envelope remain unverified.

A dedicated project still inherits organization/folder allow/deny/PAB/conditional access and regional/CMEK/domain/service restrictions. Parent identity, inherited IAM and billing constraints are unknown; the old project's parent-less/default-policy evidence does not transfer. Check automatic default-service-account grants and service agents; reject unexplained broad access before provisioning. Inherited rights cannot be removed with project-local bindings. Do not silently weaken organization policy or add KMS/perimeter/other services to resolve a blocker.

## Terraform review only — no changes implemented

- Existing root already accepts a separately bootstrapped project/provisioner. Future ignored `dev.tfvars` would select the proposed names, `prefix=bankpilot`, `environment=dev`, `region=europe-west2`; module/SQL/schema changes are unnecessary for project separation.
- Exact proposed state path needs a later reviewed **explicit local backend** and ignored per-environment backend input (`path = "state/dev/terraform.tfstate"`, resolved from Terraform root). Current root is still implicit local; simply inventing a different path does not configure it. No backend/state directory/input was created now. Reinitialize only after approval; never migrate unrelated state.
- Keep provider **7.31.0**, the existing lockfile, all deletion guards, private bucket policy and seven-table schemas. Project/billing/API/budget/bootstrap remain outside this narrowly scoped root; initial root plan should still contain exactly 25 creates.
- Review local encryption, backups, state locks and input/hash custody before planning. Test remains reserved and requires separate state/resources/budget approvals; it is not included in the initial inventory.

## Exact approval sequence

Each approval names the operator, exact scope, evidence/reviewer and expiry; one gate does not authorize the next.

1. **Read-only prerequisites:** approve checking proposed project/bucket availability, exact existing parent, inherited policies, project-creation quota/authority and private billing-account constraints. Inventory potentially priced metadata calls and their approved allowance first. No calls occurred in M6D; availability is not established.
2. **Project creation:** approve only `ebank-pilot-dev-261031-k7m4`, display name, exact parent or explicit No-organization choice, operator/create permission, labels and creator-access review. Stop on name conflict; no automatic suffix substitution, organization/folder creation or old-project changes.
3. **Billing linkage:** approve the privately identified existing account ID, operator permissions and association of only that new project. Verify account open/link eligibility; no unlinking of the old project, new billing account or payment changes. Handle any reauthentication as a separately approved action.
4. **Budget:** approve the exact project-number filter, US$5/currency equivalent, period, thresholds, credit treatment, confirmed recipients, API consumer and necessary permission/API changes. Verify configuration before paid resources/workloads; alert delivery and US$1 manual allowance remain separate controls.
5. **APIs and optional lower quota:** approve the exact API list/consumer project and temporary activator permissions, including polling. Any quota reduction requires its own exact value/units; never auto-enable unrelated services or increase quotas.
6. **Identity bootstrap:** approve exact provisioner/custom roles, 25-permission candidate, operator account-scoped token-mint binding, expiry/revocation and creator/service-agent review. No runtime/admin role conflation or keys. Runtime account remains a later root resource; its impersonation and initial-load grants need later explicit approval.
7. **State/authentication preparation:** approve backend code/input changes, encrypted path/custodian/backup/permissions/exclusive execution and concrete tfvars. Separately approve a short-lived source-authentication/impersonation route; existing gcloud login does not authorize ADC creation. Review hashes/lockfile and rerun local/provider checks after any implementation change.
8. **Terraform planning only:** after verified prerequisites, approve a private saved plan for the exact inputs/state/code/lockfile; expect 25 creates, no replacements/deletes/adoption, workload jobs or extra services. Capture binary/JSON/SHA256, IAM diffs and cost review. **Apply requires a later approval of that specific plan**, followed by separate workload and cleanup approvals. No plan/apply/destroy was run here.

## Executed checks and stop status

Proposed-name syntax/length/derivation checks passed; no availability check ran. Terraform recursive formatting and Ruff passed; **55 relevant tests passed in 3.94s**. All **43 sample hashes** and the provider lockfile are unchanged. Cost arithmetic independently checked; `git diff --check` passed. No implementation/configuration changed, so M6A's provider validation/full-suite evidence was not repeated.

New file: this design document only. The pre-existing untracked M6C document is preserved unchanged. Ignored local evidence: `outputs/runs/m6d-design/local-checks.json`, command logs and final verification record. No GCP API call, authentication, infrastructure/backend/IAM/billing change, resource creation, workload, Terraform plan/apply/destroy, installation, commit or push occurred. Public documentation was read without cloud credentials. **Stop after M6D.**
