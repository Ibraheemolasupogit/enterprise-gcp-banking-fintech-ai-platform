# M6K least-privilege provisioning remediation

2026-10-31; starting revision `7ec60d5` (M6J committed by the user). This implements **Option A** from [M6J](m6j_review.md), approved by the user, as an **offline code and configuration change only**. No cloud API call, authentication change, impersonation, IAM grant, service-account or role creation, API enablement, billing change, Terraform plan/apply/destroy, state creation, commit or push occurred. Decision record: [ADR-003](../architecture_decisions/ADR-003-separated-iam-bootstrap.md).

## What changed

| Area | Change |
| --- | --- |
| `infrastructure/terraform/bootstrap/` (new root) | Created by the bootstrap administrator, with its own state. Manages the runner and provisioner SAs (keyless, prevent-destroy); five runtime roles plus `*_provision` and `*_impersonate`; the runner jobs project member; and two grants: the provisioner project grant and the operator's account-scoped token-minting grant. Both grants are `count`-gated (off by default) and carry a `request.time` expiry validated as no more than 8 hours after plan time. Also manages IAM Credentials ADMIN_READ/DATA_READ audit logging. Uses additive `*_iam_member` only. The provider does not impersonate. The operator input must be a single `user:` principal. Prefix is limited to 13 characters so `<prefix>-test-provisioner` fits 30 |
| Pilot identity module | All `resource` blocks removed (runner SA, 5 custom roles, project member). Now **read-only data sources** with plan-time postconditions: runner enabled; runtime and provision role definitions equal the repository JSON and are not deleted; runner holds the jobs role unconditionally and no other project role; provisioner holds **only** its provision role at project scope; that binding has a condition |
| Pilot `main.tf` | Reads role definitions from `infrastructure/iam/`, passes the provisioner member for verification, and exports `jobs_role`. No Owner/project-policy resources. Provider impersonation is unchanged |
| `iam/provisioner.role.json` | 29 → **20** permissions. Removed: `resourcemanager.projects.setIamPolicy`, `iam.roles.create/list/update`, `iam.serviceAccounts.create/list/update`, `storage.buckets.list`, `bigquery.tables.list` |
| `iam/runtime_roles.json`, `iam/impersonate.role.json` | New single sources of truth (runtime permissions unchanged; impersonation = `iam.serviceAccounts.getAccessToken` only) |
| `iam/cleanup.role.json` | Now `storage.objects.list/delete`, `storage.buckets.delete`, `bigquery.tables.delete`, `bigquery.datasets.delete`. **Added** object listing, needed to enumerate noncurrent generations. **Removed** `iam.roles.delete` and `iam.serviceAccounts.delete`, because identity removal is a bootstrap-root concern. Cleanup execution is **not** approved |
| `pilot_controls.json` (v1.1.0) | GBP, `monthly_alert_budget_amount: 5`, thresholds `[0.5, 0.8, 1.0]`, budget name, source `user_console_M6G`, independent verification blocked (M6H), forecast `null`/unverified, `budget_is_spending_cap: false` and an explicit no-cap note. The `proposed_monthly_budget_usd` key is removed. The separate `pilot_allowance_usd: 1` is preserved with a "not a conversion" status. No consumer of the removed key exists in the repository |
| Tests | New `tests/test_gcp_iam_separation.py` (12 tests). `test_gcp_readiness.py` updated for the new layout, the 18-instance count, the GBP controls and the cleanup set. `make gcp-offline-check` now includes the new suite |
| Docs | Readiness identities/escalation/budget text, pilot-plan Gate A/B/D, capability matrix, index, diagram node, ADR-003 |

## IAM separation evidence

- **Administrative and provisioning rights are disjoint.** The provisioner role contains none of the 23 tested administrative permissions: project `setIamPolicy`/update/delete; role create/update/delete/undelete; SA create/update/delete/enable/disable/IAM/actAs/token/sign; API enable/disable. It also contains no key, billing, org-policy, logging or deny-policy permission.
- **Different identities.** Bootstrap runs as the administrator, and its provider block contains no impersonation or credential settings. The pilot root impersonates only the provisioner. The two roots have separate states by construction.
- **Pilot root cannot create identities or project IAM.** It contains no `google_project_iam_*`, service-account, key, `google_project_service` or Owner/Editor references.
- **Tamper detection.** Before any pilot change, the plan fails if the provisioner gains another project role, loses its expiry, or its role or the runtime roles drift from the repository JSON.
- **Mutation check.** Each injected escalation was caught by the suite, and all files were restored byte-identically (SHA256 verified):
  - provisioner role gains `iam.roles.update` and project `setIamPolicy`;
  - bootstrap provider impersonates;
  - provisioner binding loses its condition;
  - pilot root gains a project IAM member.

## Remaining privilege-escalation risks (documented, not eliminated)

1. **Provisioner, data plane.** `storage.buckets.setIamPolicy/update` and `bigquery.datasets/tables.setIamPolicy/update` at project scope can grant data access on pilot resources to itself or outside principals. Domain restriction is ALLOW-all with no organisation. `buckets.update` can turn off public-access prevention. The provisioner cannot gain project, identity or role administration, keys or impersonation. Narrowing by resource name was **not implemented**, because condition support for BigQuery and for create operations is unverified, and BigQuery datasets ignore role-grant limits. Authoritative bucket/dataset policies expose out-of-band grants as drift; table members are additive.
2. **Bootstrap administrator = sole Owner.** Unrestricted. Separation limits the provisioner token's blast radius, not the human. The Owner binding is unchanged and must not be removed (no organisation, sole recovery path).
3. **Token lifetime.** Removing or expiring a grant leaves already-minted tokens valid for up to 1 h. Expired conditional bindings stay in the policy until removed.
4. **Audit configuration.** The Owner can change `auditConfigs`. Admin Activity logs remain always-on.
5. **`serviceusage.services.use` / `resourcemanager.projects.getIamPolicy`** on the provisioner. Neither escalates; they allow quota attribution and policy visibility (needed for the postconditions).
6. **Sufficiency is unproven** (M6J F8). The provider's call pattern for dataset IAM and the need for `serviceusage.services.use` are unverified. Missing permissions must return to review, never be widened automatically.

## Validation (this session, offline)

| Check | Result |
| --- | --- |
| Full `pytest` | **311 passed** |
| `make gcp-offline-check` (readiness, reconciliation, IAM separation; fmt) | **67 passed**; fmt passed |
| `terraform fmt -check -recursive` (v1.16.5) | Passed (both roots) |
| `terraform validate`, pilot root | **Valid** (existing initialisation; no re-init needed) |
| `terraform validate`, bootstrap root | **Valid**. Initialised with `-backend=false -plugin-dir=../.terraform/providers -lockfile=readonly` from the existing local provider cache: **no download, no backend, no state** |
| Provider lockfiles | Bootstrap lockfile is a byte copy; both SHA256 `61cea4a1…0fef`, unchanged |
| `ruff check .` | Passed |
| 43 baseline sample artifacts | **43/43 SHA256 match** |
| State/plan/tfvars files | None (only `.example` placeholders); ADC file absent |
| `git diff --check` | Passed; `bootstrap/.terraform/` is git-ignored |

The Terraform variable validation that uses `plantimestamp()` (the 8-hour expiry cap) is unknown during `validate`, so it is only **statically tested**, not executed. Running it would need a plan (a mocked `terraform test` plan counts), which was not approved.

## Secure state prerequisites (review only; no state created)

There are **two** states, pilot and bootstrap. Each needs the controls in [M6J](m6j_review.md#terraform-state-custody-encryption-and-backup): a path outside the repository through an explicit `backend "local"` with partial `-backend-config=path=…`; umask 077, directories 0700 and files 0600; single-operator locking and never `-lock=false`; encrypted post-apply backups with SHA256/serial recorded; and a tested restore. Bootstrap state reveals the identity/IAM topology and is integrity-critical. Keep it no less protected than the pilot state, and in a separate directory. The backend block is **not yet added**; it belongs to the state gate.

Open approval gates:
1. **FileVault is still Off** (M6J F5); it must be On and verified first.
2. The encrypted backup destination is unnamed.
3. A custodian must be named for both states.
4. Restore verification must be done.

## Precise bootstrap prerequisites (next gates, none approved)

1. **Local security:** FileVault On (verified); state paths, backup destination and custodian approved for both roots.
2. **Inputs** (ignored `bootstrap/*.tfvars`): `project_id = ebank-pilot-dev-261031-k7m4`, `environment = dev`, `prefix = bankpilot`, `operator_member = user:<named operator>`, `provisioner_access_enabled = false` for the first bootstrap, `token_audit_logging_enabled` decision.
3. **Authentication route for the administrator:** a per-session `GOOGLE_OAUTH_ACCESS_TOKEN` from the existing operator gcloud session; no ADC file. Run fresh read-only verification first (the M6I baseline may be stale).
4. **Bootstrap saved plan** reviewed and approved by SHA256. Expect 11 creates (with access disabled) and the audit config if approved; no Owner change, no authoritative policy.
5. **Bootstrap apply**, then read-back verification: SAs have only system-managed keys; role definitions match the JSON; the policy diff is additive only.
6. **Access window:** re-plan the bootstrap with `provisioner_access_enabled = true` and an exact UTC `access_expiry` (≤ 8 h), approved per gate. Token minting is a separate explicit authorisation.
7. Only then: pilot-root inputs/state, a saved plan (18 instances, identity postconditions passing) and a later apply approval.

**Revocation:** set `provisioner_access_enabled = false` and apply through an approved bootstrap plan. In an emergency, remove both conditional members with gcloud using an exact `--condition` match, and disable the provisioner SA for an immediate cut-off. Allow up to 1 h for issued tokens to expire. Verify by policy read-back and the IAM Credentials audit logs.

**Stopped after M6K.** No bootstrap or other progression.
