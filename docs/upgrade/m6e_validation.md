# M6E dedicated-project eligibility verification

2026-10-31; starting revision `0d29ed6`, initially clean. Authorized read-only eligibility checks used the existing configured identity and cached session without renewal, impersonation, ADC or default changes. **Creation eligibility is only partially verified; neither global name availability nor a permissible creation parent is established.**

## Exact reads and outcomes

Executed local command: `python3 outputs/runs/m6e-eligibility/verify_read_only.py`. The first sandbox attempt failed DNS before an HTTP response; approved network execution then completed **eight requests: five 200, two 403, one 404**. Seven were GETs; the only POST was the non-mutating billing permission test. No `gcloud projects create`, authentication command, write API, Terraform plan or job was used.

For privacy, `BA` below means the same existing `billingAccounts/{id}` verified privately in M6C; `PN` is the existing project's verified number. Exact resolved URLs are retained only in ignored private evidence. Authorization headers/tokens are omitted entirely.

| Method and URL executed | Outcome |
| --- | --- |
| `GET https://cloudresourcemanager.googleapis.com/v3/projects/ebank-pilot-dev-261031-k7m4` | **403 PERMISSION_DENIED**: `resourcemanager.projects.get` denied **or the project may not exist**. No accessible project established; existence/conflict/global availability unresolved |
| `GET https://cloudresourcemanager.googleapis.com/v3/organizations:search?pageSize=100` | **200**, zero visible organizations, complete single-page response |
| `GET https://cloudresourcemanager.googleapis.com/v3/folders:search?pageSize=100` | **200**, zero visible folders, complete single-page response |
| `GET https://cloudbilling.googleapis.com/v1/BA` | **200**, account **open**, currency **GBP**; no parent organization or master-account reference returned. Payment/invoice data was not requested |
| `POST https://cloudbilling.googleapis.com/v1/BA:testIamPermissions` | **200**, all eight requested permissions returned; body listed below |
| `GET https://billingbudgets.googleapis.com/v1/BA/budgets?pageSize=100` | **403 PERMISSION_DENIED**, reason **SERVICE_DISABLED** in the request's consumer project, which differs from the existing project. Stopped; no API activation, consumer rerouting or permission changes |
| `GET https://serviceusage.googleapis.com/v1beta1/projects/PN/services/cloudresourcemanager.googleapis.com/consumerQuotaMetrics?view=FULL&pageSize=200` | **200**, 58 metrics, complete single-page response; API request quotas, not proof of remaining project-creation allocation |
| `GET https://storage.googleapis.com/storage/v1/b/ebank-pilot-dev-261031-k7m4-ew2-artifacts?fields=name,location,storageClass` | **404**. No bucket metadata returned. Not a reservation or guarantee of global availability/creation eligibility |

The permission-test body was exactly:

```json
{"permissions":["billing.accounts.get","billing.accounts.getIamPolicy","billing.accounts.getSpendingInformation","billing.resourceAssociations.create","billing.budgets.create","billing.budgets.get","billing.budgets.list","billing.budgets.update"]}
```

The single bucket metadata GET was deliberately bounded: it is a potentially priced Class B operation, not a bucket/object listing or data read. Actual small API charges/allowances were not verified. No material-cost operation or workload was attempted; no additional Storage probe followed. No 403 was treated as absence and no 404 as global availability. Name reservations/deleted-ID restrictions and races remain unresolved.

## Parent, project-creation authority and inheritance

Organization/folder searches expose only resources the caller can view. Empty results do **not** establish that no such resources exist, that no hidden parent could grant creation, or that organization/folder policy permits creation. No parent ID was invented. [Organization visibility](https://docs.cloud.google.com/resource-manager/reference/rest/v3/organizations/search), [folder visibility](https://docs.cloud.google.com/resource-manager/reference/rest/v3/folders/search).

No organization/folder is currently a verified permissible parent. **No organization remains a candidate requiring explicit selection and eligibility confirmation**, not an approved/default choice. With no real visible or supplied parent, there was no correct parent resource on which to test `resourcemanager.projects.create` or read inherited IAM/organization policies. Those checks remain **unverifiable**, rather than clean or denied. Existing-project Owner rights and its parent-less policy snapshot do not transfer to the new project. No fictitious `organizations/0` or unrelated-project permission test was used as a substitute.

The quota response includes `cloudresourcemanager.googleapis.com/project_v3_create_requests`: **240 requests/minute/project**, with no additional per-user request-rate limit in that consumer context. It did **not** expose `cloudresourcemanager.googleapis.com/projects_count` or the account's remaining No-organization project allocation. A request-rate limit cannot demonstrate capacity to create another project. User/admin confirmation of at least one remaining project allocation and the available parent choice is still needed. Google's documentation describes a user-account quota for creation outside an organization and a console quota notification; no creation RPC was used as a probe. [Project creation and quotas](https://docs.cloud.google.com/resource-manager/docs/creating-managing-projects).

For an organization/folder path, supply the exact parent and authorized read access, then verify parent/ancestor allow/deny/PAB/conditional policies and regional, service-account, billing-association and CMEK restrictions. For No organization, confirm that option is actually available to this identity; no organization/folder inheritance should be assumed until the resulting parent is verified. London remains the intended **workload** region; projects themselves have no workload-region setting.

## Billing/API/budget eligibility and risks

**Account-side billing eligibility is verified:** the existing account is open and the current identity has `billing.resourceAssociations.create`. This does not establish the new project's required `resourcemanager.projects.createBillingAssignment` permission, which must be checked on an accessible created project before any separately approved link. Account openness and an empty returned parent/master reference do not prove all association limits/organization restrictions are satisfied. No new account, payment method or billing link was created. [Permission test semantics](https://docs.cloud.google.com/billing/docs/reference/rest/v1/billingAccounts/testIamPermissions).

Budget create/get/list/update permissions are granted, but the Budget API is inaccessible through the current consumer route. Existing budget amounts, notifications and delivery remain unknown. The 403 is not a missing-budget finding or authorization to enable the consumer mentioned by the error. Resource Manager, Billing and Service Usage read endpoints worked; **the proposed project's API states could not be inspected** after its 403. M6D's six application/control API prerequisites still require verification/explicit activation approval after creation; no old-project API state was assigned to the proposed project.

The confirmed **GBP** account changes the implementation of the US$5 alert proposal: any specified budget currency must match the account. Obtain approval of a documented GBP equivalent of US$5, including rate/date, rather than silently creating GBP5 or USD5. Reconcile the US$1 manual workload allowance similarly. No FX rate was invented or budget configured. Alerts-only budgets do not impose a hard spending cap. [Budget currency rules](https://docs.cloud.google.com/billing/docs/reference/budget/rest/v1/billingAccounts.budgets#BudgetAmount).

Least-privilege concerns remain: the operator's existing billing administration should not be delegated to runtime/provisioner; parent creation/IAM authority is unverified; future provisioner IAM-write/custom-role rights permit escalation even in a dedicated project; any creator/default/inherited grants require review. This eligibility check grants no project-wide role, service-account impersonation or bootstrap permission. [M6D design](m6d_design.md) remains a proposal, not deployed infrastructure.

## Precise next approval gate

**Not yet a verified green light to create.** Resolve the parent/creation-quota evidence first:

1. Provide an exact organization/folder with verified `resourcemanager.projects.create` and inherited-policy review, **or** explicitly select No organization after confirming it is offered to the configured identity and at least one user project allocation remains. No automatic parent selection or quota increase is authorized.
2. Then a separate approval may authorize **one conditional project-creation operation only** for `ebank-pilot-dev-261031-k7m4`, display name `Enterprise Banking Pilot Dev`, the exact approved parent/No-organization route, approved labels, named operator, reviewer and execution window. Availability is still conditional on server validation; stop on conflict/permission/quota failure with no suffix substitution, alternate parent or adoption of an inaccessible project. Confirm the supported creation route honors the selected parent and does not bundle extra API activation/billing operations; record platform-created defaults for review.
3. That creation approval must exclude billing linkage, budget setup, API enablement, identity bootstrap, state/backend changes and Terraform planning. Verify the resulting project/parent/creator grants and project-side billing-assignment permission before requesting a **separate** link approval referencing the private existing billing account.
4. Before paid resources/workloads, separately resolve the Budget API consumer, approve GBP thresholds/recipients and the bounded allowance, and complete the remaining M6D API/identity/state/planning gates. No change or workaround is authorized by this report.

## Offline checks and stop status

No implementation/configuration changed, so M6D's successful formatting/Ruff/**55 relevant tests** were not repeated. Performed targeted evidence checks: proposed-name syntax, HTTP read-method whitelist and status counts, complete discovery/quota pagination, local document links/redaction/whitespace, unchanged infrastructure/lockfile, **43 baseline artifact hashes** and `git diff --check`. Provider lockfile remains Google 7.31.0, SHA256 `61cea4a18251f6789ce1c68326cc6f0ec7d3209936c7fb7e913a9fdabc1c0fef`.

Private evidence is ignored under `outputs/runs/m6e-eligibility/`, with directory mode 0700/files 0600: metadata, exact request ledger/responses, one-off verification helper and final `verification.json`. Tokens were used only in process memory; cached expiry and configured identity/defaults are unchanged. No tokens, full billing identifiers or personal payment details are recorded here.

Only this M6E document is new/untracked on `main`. Prior evidence, tracked sources/samples, Terraform and Git history are preserved. No project/resource creation, billing link, API/IAM change, authentication/ADC change, BigQuery job, installation, Terraform plan/apply/destroy, commit or push occurred. **Stopped after M6E.**
