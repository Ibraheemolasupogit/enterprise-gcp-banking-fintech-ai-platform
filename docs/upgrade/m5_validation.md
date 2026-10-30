# M5 validation and deployment readiness evidence

M5 prepares a narrow synthetic data pilot without deploying it. Starting revision was `7a14be52105c0c9d371ec7845826dc8220c09365` (`feat: add reconciled financial analytics data marts`). No commit or push was made. The [readiness design](../gcp_deployment_readiness.md), [operational gates](../gcp_pilot_plan.md) and [ADR-002](../architecture_decisions/ADR-002-minimal-gcp-pilot.md) contain the complete resource, identity, cost and migration decisions.

## Files and scope

Added modular Terraform root/variables/example environment and three JSON-syntax modules under `infrastructure/terraform/`; typed M3 source schemas under `infrastructure/schemas/v1/`; proposed explicit-column transactional BigQuery mart SQL; bounded pilot controls; nine offline policy/schema tests; readiness/operational/evidence documents; ADR-002 and a minimal diagram. Updated `.gitignore` for local Terraform state/plans/inputs, README, documentation index, architecture summary and inherited reference architecture scope notice. No M1–M4 source, SQL or CLI interfaces changed. There were no dependency installations or inherited sample changes.

The proposed inventory is one versioned STANDARD Storage bucket, one regional BigQuery dataset, seven tables (curated transactions, customers, accounts, four daily marts), one runtime service account and five custom roles, with scoped policies/members. It expands to 25 Terraform resource instances including IAM. Provisioner is pre-existing and separately bootstrapped. There is no budget resource, project, billing link, API activation or unrelated runtime service in Terraform.

## Actual local checks

| Command / check | Result |
| --- | --- |
| `python3 -m ruff check .` | All checks passed |
| `python3 -m pytest -q` | **253 passed in 23.91s**, no warnings |
| Focused M5 tests | 9 cases passed; included in full suite |
| `terraform version` | 1.16.5, darwin_arm64 |
| `terraform -chdir=infrastructure/terraform fmt -check -recursive` | Passed, exit 0 |
| `terraform -chdir=infrastructure/terraform validate -no-color` | **Not passed**: local modules not initialized |
| `terraform -chdir=infrastructure/terraform/modules/storage validate -no-color` | **Not passed**: Google provider unavailable |
| Legacy `final-check` | 79 checks, zero issues |
| Legacy `validate-docs` | 6 configured documents, 5 diagrams, zero issues |
| `git diff --check` | Passed |
| Independent baseline artifact hash verification | All 43 unchanged |

Formatting checks apply to native HCL; module JSON syntax is independently parsed and checked by pytest. The new tests inspect the permitted resource inventory, exact role permission sets and binding scopes, authoritative local policies, no public/admin grants, private/versioned/deletion-protected storage, co-location, schema correspondence with actual M3 fields and M4 marts, explicit-column SQL compatibility, and bounded pilot controls. They are repository policy/schema tests, **not** Terraform provider validation or cloud permission tests. Specialist tools TFLint/Checkov and Python HCL parsing were unavailable. No tools or providers were downloaded; `terraform init`, plan and apply were not run. A provider lock file therefore does not yet exist and must be generated/reviewed at the first approved initialization.

The final permission review added bucket metadata read required by the documented load path and split runtime input-read permissions from mart-write permissions. Initial CSV loads require a separately approved temporary provisioning/load phase because Google's documented load-job permissions include table create/update. Those permissions are deliberately absent from the normal runner. This phase is a design/runbook, not an implemented cloud loader. Temporary grants and revocation require explicit future approval/evidence.

## Evidence and verification gaps

Ignored local evidence: `outputs/runs/m5-readiness/verification.json` plus six command logs. Infrastructure files are hashed there; combined infrastructure fingerprint is `83cd8f61864a1c08af6d399f12f0e7e159c674d15c3ab639005f20e3bc7498e8`. The full pytest session additionally verifies the 43 baseline sample hashes before and after tests. Outputs are ignored, while provider lock files are intentionally eligible for future review/versioning. The documentation validator covers only its configured historical documents, not every new M5 page.

Public primary documentation was inspected on 2026-10-30 for BigQuery/Storage pricing and London availability, data types, IAM/load permissions, quotas, budgets, cost controls and Terraform dataset policy semantics. Links are embedded next to design claims. No authenticated project/API inspection was performed. Selected-region billing rates, account free-tier eligibility, permissions/custom-role support, quotas and organization restrictions remain manual prerequisites. Failed raw-provider-document fetches were not used as verified evidence; full provider schema compatibility remains outstanding.

Illustrative pricing assumptions produce approximately **US$0.16/month** for 10 GiB query processing, 1 GiB BigQuery storage, 100 MiB total versioned object storage and 100 class-A/100 class-B operations, excluding transfer/tax/other activity and without free allowances. This is planning arithmetic, not a verified London quote. Proposed US$1 pilot allowance and US$5 monthly alert budget require approval and current regional repricing. **Budget alerts are not automatic spending caps.** Volume/job/retention controls are proposals for the future operator/executor and are not enforced by Terraform/IAM.

## Status and next approvals

- **Implemented and locally executed:** M1–M4, offline M5 policy/schema checks and Terraform formatting.
- **Authored and offline checked:** modular IaC and source target schemas; provider validation/plan remain outstanding. Do not describe these as validated deployment-ready resources yet.
- **Proposed, not cloud tested:** BigQuery load configurations, transactional mart SQL, runtime IAM effectiveness, reconciliation and operational controls.
- **Cloud deployed:** none. No GCP resources, API enablement, paid workloads, provisioning commands or cloud authentication were invoked.

Deployment requires separate explicit gates: (A) named project/region/identities/state and bootstrap changes, provider download and authenticated read-only plan; (B) reviewed saved-plan hash, apply window and billing safeguards; (C) exact synthetic run/hash/schema/SQL inputs, temporary load grants/revocation and bounded runtime jobs; (D) named-resource cleanup, evidence retention, deletion-guard changes and saved destroy-plan approval. M5 grants none of these approvals.

Residual risks include privileged project IAM writes during provisioning; inherited IAM outside resource-local policies; temporary load access; unauthenticated source manifests; future manual enforcement of data/job limits; untested GoogleSQL/BigQuery NULL/key/timestamp behavior; evidence storage and soft-delete/time-travel charges; no concurrency protocol or cloud executor; and synthetic-data validity limits. Work stops after M5.
