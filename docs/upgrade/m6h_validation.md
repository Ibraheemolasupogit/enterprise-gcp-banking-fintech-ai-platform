# M6H dedicated-project verification — resumed results

2026-10-31; revision `c46184a`. The initial attempt stopped on an expired cached token. The user manually renewed the login; the resumed local check at **06:49 UTC** found a valid existing token. This report supersedes the credential-blocked result. No assistant authentication, renewal, ADC or default changes occurred.

## Scope and verified results

Target **`ebank-pilot-dev-261031-k7m4` / `203569506404`**, intended workload region **`europe-west2`**. Every API request explicitly carried `x-goog-user-project: ebank-pilot-dev-261031-k7m4`; project resource paths used that ID or number. The unchanged gcloud default still names the old project, but **no request targeted it**. Tokens were used only in memory, never printed/persisted; redirects and automatic refresh were disabled.

There were **21 HTTP requests: 14 successful, six 403, one 404**. Eighteen GETs and three non-mutating POSTs (`getIamPolicy`, `testIamPermissions`, `getEffectiveOrgPolicy`) performed metadata/permission reads only. A local sandbox DNS failure preceded approved network execution and is not an additional GCP response. Private request/response evidence is retained under ignored `outputs/runs/m6h-verification/resumed/` with mode 0700/0600; tokens and personal billing identifiers are absent from this document.

| Check | Result and boundary |
| --- | --- |
| Project ID/number mapping | **API-confirmed** by Service Usage: requesting `projects/ebank-pilot-dev-261031-k7m4/services/serviceusage.googleapis.com` returned canonical name `projects/203569506404/services/serviceusage.googleapis.com` |
| Lifecycle state, parent | Resource Manager project GET returned **403 SERVICE_DISABLED**. No-organization parent remains **user Console-verified in M6G**; lifecycle state not freshly verified |
| Billing linkage | Project billing-info GET returned **403 SERVICE_DISABLED**. Existing GBP linkage remains **user Console-verified**, not a new API finding. A disabled Billing API is not a finding that billing is disabled |
| Project IAM / bootstrap permission probe | Both returned **403 SERVICE_DISABLED**. Operator roles, broad/default access, API-enable authority and IAM/bootstrap permissions remain unverified; no IAM permission denial or absence of grants is established |
| Service accounts / custom roles | Account listing returned **403 SERVICE_DISABLED**; further role enumeration was stopped. Provisioner/runtime presence, disabled state, bindings and expiry are unknown, not absent |
| Storage inventory | One complete project-scoped bucket-list page returned **zero buckets**. No object/data reads or bucket IAM changes |
| BigQuery inventory | Complete metadata listing returned **zero accessible datasets**. Exact `bankpilot_dev` metadata GET returned **404 NOT_FOUND**. No query/job/table-data/load operation |
| Location policy / deny / PAB | First effective-location-policy read returned **403 SERVICE_DISABLED**; remaining policy reads stopped. Deny/PAB access was not established; none were assumed absent |
| Budget rules | Project-targeted Budget API is disabled. No account-scoped fallback or consumer rerouting was attempted. The budget remains Console-sourced as below |

All six 403s were service-availability blockers, **not diagnoses of insufficient IAM grants**. API-disabled checks were not retried through the old project or a different consumer.

## Actual API states and quota limits

Individual Service Usage GETs verified these states on project **203569506404**:

| API | State / action boundary |
| --- | --- |
| `bigquery.googleapis.com` | ENABLED |
| `storage.googleapis.com` | ENABLED |
| `serviceusage.googleapis.com` | ENABLED |
| `cloudresourcemanager.googleapis.com` | **DISABLED**, required by existing pilot |
| `iam.googleapis.com` | **DISABLED**, required by existing pilot/bootstrap |
| `iamcredentials.googleapis.com` | **DISABLED**, required for approved provisioner impersonation |
| `cloudbilling.googleapis.com` | DISABLED; optional API-level billing verification, separate from the existing linkage |
| `billingbudgets.googleapis.com` | DISABLED; optional budget automation/verification, not a reason to recreate the Console budget |

Complete quota summaries returned **30 BigQuery**, **13 Storage** and **39 IAM** metrics even though IAM's execution API is disabled. Quota visibility is not service activation or permission to create:

- BigQuery query usage: **209,715,200 MiB/day = 200 TiB/day** project limit; per-user value effectively unbounded. This does not protect a 10 GiB/month pilot allowance or £5 alerts.
- IAM: configured custom-role limit **300/project**, service-account-create rate **3/minute/project**. Current role/account counts and remaining allocation are unknown because IAM listing is blocked.
- Storage quota data includes London dimensions; live usage/headroom, service-account count ceiling and relevant BigQuery load/DML/concurrency restrictions were not fully established. No quota changed.

Public documentation lists London for BigQuery/Storage. This is service availability evidence, not a test of this project's effective location policy, allocation or runtime behavior. [BigQuery locations](https://docs.cloud.google.com/bigquery/docs/locations).

**Pre-disclosed metadata cost:** complete bucket inventory required one Storage Class A list; exact-name GETs cannot reveal unexpected buckets. Execution was bounded to one request, with the published Standard flat-namespace example **US$0.005/1,000 (~US$0.000005 for one)** disclosed before the call. Actual GBP billing/allowances were not inspected. No other Storage request or data read occurred; no query/load/workload ran. [Storage pricing](https://cloud.google.com/storage/pricing).

## Budget/control alignment recommendation — no changes

M6G user Console verification remains authoritative for the supplied values: monthly project budget **`Enterprise Banking AI Pilot - Dev`**, **GBP £5**, thresholds **50/80/100%**, and existing portfolio budget unchanged. Because the target Budget API is disabled, spend basis, credit treatment, forecast rules, internal filter and recipient/delivery settings are **not independently verified**. No budget was read through an alternative consumer or modified.

A separately approved local configuration patch should preserve volume/job limits and make these exact alignments:

| Current `infrastructure/pilot_controls.json` | Recommended local change |
| --- | --- |
| `budget_currency: "USD"` | `"GBP"` |
| `proposed_monthly_budget_usd: 5` | Replace USD-specific key with currency-neutral `monthly_alert_budget_amount: 5`; do not put GBP under a USD-named key |
| `alert_thresholds: [0.5, 0.9, 1.0]` | `[0.5, 0.8, 1.0]` |
| `forecast_alert_thresholds: [1.0]` | `null`/explicit **unverified** status until Console/API evidence establishes the rule; not a claim that forecast alerts are disabled |
| Missing provenance | Record `budget_verification_source: "user_console_M6G"` and `independent_budget_verification: "blocked_API_disabled_M6H"`; do not invent spend basis |
| `pilot_allowance_usd: 1` | Retain as separate manual proposal pending agreed currency/cost envelope, without treating £5 as a USD conversion |
| `budget_is_spending_cap: false` | Keep `false`; **alerts do not enforce a hard spending cap** |

No tracked executable budget consumer was found in the prior source/scripts/tests inspection; external consumers remain unknown. Review key migration/nullable fields and update tests/docs with any later approved patch. **No control metadata, infrastructure or cloud billing changed in M6H.** Existing deployment documents already record GBP5/50-80-100; retain historical source boundaries.

## Provisioner, state and smallest bootstrap approval

The named proposal remains `bankpilot-dev-provisioner@ebank-pilot-dev-261031-k7m4.iam.gserviceaccount.com`; **existence, rights and expiration are unverified**. Do not create it until IAM inventory works. Current file has 29 candidate provisioning permissions; M6D proposes 25 excluding `iam.roles.list`, `iam.serviceAccounts.list`, `storage.buckets.list`, `bigquery.tables.list`. Sufficiency is not proven, and project IAM/custom-role writes can escalate. No automatic widening or Owner/Editor/admin/billing/API-enable/data/job/delete/key grants are justified.

Future account access must be keyless and limited to `iam.serviceAccounts.getAccessToken` on the **named provisioner account**, for a named operator with explicit start/end and revocation owner. No project-wide token minting or standing cleanup rights. Proposed runtime remains separate with five scoped roles; no runtime identity or grant was observed/created.

The state proposal `infrastructure/terraform/state/dev/terraform.tfstate` and its directory **do not exist**; current backend is still implicit local. Custodian, local encryption, encrypted backup destination/retention/recovery and exclusive execution are unapproved. Require explicit backend/path preparation, umask 077, directories 0700/files 0600 and locking under a separate local-preparation approval. ADC/source authentication is not established by this cached gcloud session. No state/input/credential was created.

**Smallest justified next bootstrap milestone: API preparation only.** A separate approval must explicitly name **only the three required, verified-disabled APIs** on `203569506404`: Resource Manager, IAM and IAM Credentials. An operator must already have `serviceusage.services.enable` and relevant status/poll reads; that authority was not verified because the Resource Manager permission probe is blocked. Stop on denial; do not grant a broad role. BigQuery/Storage/Service Usage are already enabled and need no activation. No API was enabled by this report.

After an approved activation, rerun the already-approved project/IAM/policy/permission reads. Only then request approval for absent named bootstrap resources: provisioner, `bankpilot_dev_provision`, `bankpilot_dev_impersonate`, one expiring project grant and one account-scoped operator binding. Runtime remains a later 25-instance-root resource. Billing/Budget API activation is **optional separate administration**, not part of the minimum M5 bootstrap or budget recreation. Local control alignment, secure state/authentication preparation, saved-plan generation, apply, workload and cleanup approvals remain separate. **No automatic progression.**

## Local validation and stop status

Terraform recursive formatting **passed**; **12 policy/schema tests passed in 0.60s**, with logs in ignored `local-checks.json` and command logs. All 43 sample hashes, tracked infrastructure/control files and provider lockfile remain unchanged (Google 7.31.0; lock SHA256 `61cea4a18251f6789ce1c68326cc6f0ec7d3209936c7fb7e913a9fdabc1c0fef`). Local evidence/method/scope/redaction/link checks and `git diff --check` passed. No implementation changed or full-suite rerun was needed.

Only this M6H document is new/untracked on `main`. Private evidence is restricted to directory 0700/files 0600. Cached expiry, identity/default configuration and ADC absence are unchanged from the resumed start. No authentication/refresh/default/ADC change, API activation, IAM/billing/cloud-resource modification, new state, job/load/workload, installation, Terraform plan/apply/destroy, commit or push occurred. **Stopped after the M6H report; API blockers remain.**
