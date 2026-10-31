# M6C read-only GCP environment verification — resumed report

2026-10-31; starting revision `4b8072c`. The initial M6C attempt stopped on an expired cached token without contacting GCP. The user then manually renewed the existing login and authorized resuming the same read-only checks. This report supersedes the credential-blocked result. **Environment verification proceeded; deployment remains unapproved.**

## Method, boundaries and evidence

The existing configured account/project matched the user-approved identity and `virtual-rarity-470405-c5`. The renewed cached session was valid. A fixed-host HTTPS client used that token in memory only, with no refresh, impersonation, token minting, ADC creation, SDK/default changes or credential writes. No token or refresh credential was logged or persisted. Responses containing account identifiers and billing-account linkage remain in ignored private evidence, outside this document.

There were **26 GCP HTTP requests: 22 HTTP 200, one 400, two 404 and one 403**. All GETs were metadata reads; the seven POSTs were `testIamPermissions`, `getIamPolicy` or `getEffectiveOrgPolicy`, not mutations. Initial local DNS/TLS failures were resolved using approved network access and the already-installed native trust-store support; certificate verification stayed enabled. No dependencies were installed. One permission probe included an invalid added `bigquery.datasets.list` permission; it returned 400, was corrected and succeeded. This was a verification-request defect, not a Terraform defect.

**Cost exception:** the single successful Storage bucket-list request is a priced **Class A operation** and may carry a small API charge. Pricing should have been checked before that call. No further Storage calls were made once this was identified, including bucket-level IAM probes. Actual charged amount/free allowance was not verified; no zero-cost claim is made. No object/data reads, BigQuery jobs, chargeable queries, loads or workload execution occurred. [Storage operation pricing](https://cloud.google.com/storage/pricing).

Private evidence: `outputs/runs/m6c-verification/resumed/`, directory mode 0700 and files mode 0600. It contains the request ledger, bounded paginated metadata responses, permission/policy/API/quota results, billing-access diagnostics, local check logs and final `verification.json`. Credential values are absent. No personal billing identifiers or notification addresses are placed in Git.

## Verified project, billing and identity facts

| Check | Verified result |
| --- | --- |
| Project | `virtual-rarity-470405-c5` exists and is **ACTIVE**. The project response returned no folder/organization parent |
| Billing | Existing billing link present, **`billingEnabled=true`**, linked account **`open=true`**. Account ID retained privately; spending history, balance, currency and invoice/contact details were not inspected |
| Current operator | Direct unconditional **`roles/owner`** grant in project allow policy. This broad existing human authority is not the least-privilege provisioner design |
| Other project access | Two unconditional **Editor service-account grants**, including the Compute default account, plus pre-existing service-agent roles. No `allUsers`/`allAuthenticatedUsers` grant in the returned project allow policy; bucket ACLs, deny/PAB policies and every access path were not exhaustively inspected |
| Billing administrator | Returned billing-account IAM policy lists one direct Billing Administrator principal: the already configured operator. Identity linkage retained privately, not copied as personal billing data into Git |
| Notifications/budgets | Budget list returned **403 `SERVICE_DISABLED`** for the request's consumer project, which differs from the candidate. Stopped that check: no consumer rerouting, activation or permission widening. Existing budget, thresholds, recipients and email-delivery settings are **not verified**; being Billing Administrator does not prove notification configuration |
| Authentication for Terraform | Standard ADC still absent; no ADC or impersonation configuration was created. A valid gcloud session does not establish Terraform ADC readiness |

The corrected project-level permission probe returned **21 of the 29** candidate provisioning permissions plus five requested read permissions. It confirmed project IAM-write/custom-role-management rights of the current operator. Eight resource-level candidate permissions were not established at project scope: `bigquery.tables.create/get/list/update`, `storage.buckets.get/getIamPolicy/setIamPolicy/update`. This is **not** a resource-level denial diagnosis or proof of a missing role; future table/bucket permissions require correct resource context. No table exists to test and further priced Storage permission probes were stopped. No dedicated provisioner was impersonated or tested.

## APIs, inventory and naming conflicts

| API in candidate project | Observed state |
| --- | --- |
| BigQuery, Storage, Cloud Resource Manager, Service Usage | **ENABLED**, from complete enabled-service listing |
| IAM, IAM Service Account Credentials | **DISABLED**, additionally verified by individual service-state GETs |
| Cloud Billing, Billing Budgets | **DISABLED** in candidate project, individually verified |

There were 31 enabled services in total. Some global IAM/billing metadata reads succeeded despite these project service states; that does not prove provisioning/impersonation will work without the design's prerequisite APIs. Cloud Billing reads already worked, so its activation must not be proposed automatically merely to repeat them. Budget access requires review of the actual consumer/quota-project route before approving any activation, never enabling the unrelated consumer from an error message.

- **Storage:** two existing STANDARD buckets, one in `EUROPE-WEST2`, one in `US-CENTRAL1`. One bucket already has the exact candidate project ID as its name; that name is unavailable for a new pilot bucket. The London bucket belongs to existing AI Studio-style usage and must not be adopted. Full inventory names remain private. Both report uniform bucket-level access **false**, public-access prevention **inherited**, versioning **false**, and seven-day soft delete. These settings differ from the proposed new bucket; they do not alone prove public access. Existing bucket IAM/ACL/data was not changed or read.
- **BigQuery:** complete all-dataset listing returned **zero datasets**. Exact metadata GETs for `fincrime_m5_dev` and `fincrime_m5_test` returned **404 NOT_FOUND**. No table/data/query operations were performed.
- **Identities:** two enabled user-managed accounts were listed: Compute default (direct Editor grant) and an AI Studio-style account (no direct project binding in the returned allow policy). Neither was selected as provisioner. No dedicated financial-pilot provisioner or proposed runtime account appeared in the complete listing. Token-mint permission and full service-account policy were not tested for these unrelated identities.
- **Custom roles:** complete active-role listing returned **zero**. Proposed five active role IDs have no observed collision; deleted-name reuse restrictions remain untested.
- The exact new bucket name, provisioner email, environment/base and state custody are still **unselected**. Project-wide listing does not establish global availability of an unspecified bucket name.

This is an **existing project with other resources and broad access**, not evidence of an empty, isolated financial-pilot sandbox. Fresh resource names avoid adoption but do not remove inherited Owner/Editor authority from future resources.

## London restrictions and quota observations

Effective legacy policies returned: resource locations **ALLOW all**, allowed member domains **ALLOW all**, non-CMEK services **ALLOW all**, and service-account-creation boolean policy with no enforced restriction. These selected policies expose no London/CMEK/domain blocker. They are not an exhaustive managed/custom/deny-policy audit. Existing London Storage metadata and the published BigQuery location list support regional suitability; actual BigQuery allocation/runtime behavior was not exercised. [BigQuery locations](https://cloud.google.com/bigquery/docs/locations).

Complete single-page quota summaries were obtained for BigQuery (**30 metrics**), Storage (**13**) and IAM (**39**):

- BigQuery query usage is **209,715,200 MiB/day = 200 TiB/day** per project; the per-user value is effectively unbounded. This is far above the proposed **10 GiB/month** pilot allowance and offers no practical pilot spending cap.
- IAM custom-role configured limit is **300/project**; active listing returned zero. IAM create-service-account rate is **3/minute/project**, general reads **6,000/minute** and writes **600/minute**. These are configured limits, not measured remaining capacity or authorization to use them.
- Storage metrics include `europe-west2` dimensions. Relevant live usage, operation/storage headroom, service-account count ceiling and BigQuery load/DML/concurrency limits were not fully exposed by these summaries. They remain prerequisite checks, not assumed unlimited capacity.

No quota was raised, lowered or otherwise modified. Existing resources may already incur costs; no project spending report was queried. M5's ~US$0.16 planning scenario excludes those resources and is not a verified current project bill or London quote. The **US$1 workload allowance** and **US$5 alerts-only budget** remain proposals; budget existence/delivery is unverified, and **alerts are not hard spending caps**.

## Comparison with the 25-instance pilot and next approval gate

The local design still contains **25 Terraform instances**: bucket/dataset/seven tables, runtime identity/five roles and scoped IAM policies/members. No Terraform/configuration defect was found or fixed. M5/M6B safeguards remain unchanged: new resources only, authoritative local IAM, deletion guards, no destructive lifecycle shortcuts, separate workload/cleanup approval. All seven schemas and SQL remain local proposals; no cloud mart ran.

Readiness blockers and required decisions:

1. **Isolation decision:** explicitly assess/accept this existing project's inherited Owner/Editor/service-agent exposure for fresh pilot resources, or select a separately approved isolated project. Do not apply authoritative policies to either existing bucket. No IAM narrowing or new project was authorized/performed.
2. **Dedicated provisioning identity/bootstrap:** supply the exact dedicated provisioner email and reviewed short-lived role/impersonation grants. The existing accounts are not automatically suitable. If a dedicated account must be created, its account/role/bindings are separate bootstrap resources and approvals **outside the 25 pilot instances**; no creation is implied by this report.
3. **Named API/auth approvals:** separately approve any required IAM/IAM Credentials activation and Terraform short-lived authentication arrangement. Existing gcloud login does not authorize creating ADC or minting provisioner credentials. Resolve budget API consumer configuration and any necessary candidate-project API enablement explicitly; current 403 does not authorize a workaround or enabling the error's consumer project.
4. **Concrete inputs and controls:** select a globally unique fresh bucket, environment/base, state owner/encrypted location/backups, billing recipients and retention/cleanup owner. Review regional pricing and remaining load/DML/account quotas. Establish/verify the proposed US$5 alert configuration and US$1 bounded workload allowance through separately approved billing work; do not claim either is currently enforced.
5. **Then, separate saved-plan authorization:** only after prerequisite approvals/evidence, approve producing a private saved plan with concrete inputs, Google 7.31.0 lockfile and exact code/schema hashes. Expected fresh inventory is 25 creates, no adoption/replacements/deletes or extra cloud services. Apply must later be approved against that specific plan SHA256, IAM diff, cost estimate, state custody and execution window. Workload execution and cleanup remain distinct approvals.

## Local validation and final status

Resumed checks: Terraform recursive formatting **passed**, Ruff **passed**, relevant HCL/schema/IAM/inventory/reconciliation tests **55 passed in 4.50s**, and all **43 sample artifacts unchanged**. Provider lockfile remains Google **7.31.0**, SHA256 `61cea4a18251f6789ce1c68326cc6f0ec7d3209936c7fb7e913a9fdabc1c0fef`. No implementation/configuration changed, so M6A provider validation/full-suite evidence was not repeated. `git diff --check` passed.

Only this M6C document is new/untracked on `main`; no tracked source, sample, Terraform input/lock/state or history changed. Raw read-only evidence and one-off helpers are ignored/private. No authentication change, ADC, API activation, IAM/billing/resource modification, object read, query/load/workload, Terraform plan/apply/destroy, install, commit or push occurred. The one potentially billable Storage metadata listing is disclosed above. **Stopped after the M6C report; no deployment approval is implied.**
