# M5 validation and deployment readiness evidence

M5 continuation completed locally on 2026-10-30, starting from `63a16d9` (`infra: prepare secure GCP analytics pilot infrastructure`). The committed M5 foundation was inspected and hardened in place. M1–M4 source, legacy CLI/SQL interfaces, Git history and all 43 baseline artifacts are preserved. No commit, push, repository rename, cloud authentication, GCP API call, tool/provider installation or paid service invocation occurred. Public documentation was read without project credentials. Work stops after M5.

See the [readiness design](../gcp_deployment_readiness.md), [operational gates](../gcp_pilot_plan.md) and [ADR-002](../architecture_decisions/ADR-002-minimal-gcp-pilot.md) for the complete decisions.

## Changed files

| Files | Result |
| --- | --- |
| `infrastructure/terraform/main.tf`, `variables.tf` | Environment-isolated identity/custom-role names; distinct same-project provisioner; account-length guard; Terraform >=1.9 for cross-variable validation; environment in inventory. |
| `infrastructure/terraform/environments/dev.tfvars.example`, new `test.tfvars.example` | Base namespace and separate environment placeholders. Separate fresh buckets/state required. |
| New `infrastructure/iam/provisioner.role.json`, `cleanup.role.json` | Expanded provisioning candidate without data/job/delete/token/API-enable access; six deletion permissions isolated in a separate add-on. Review files only, not applied by Terraform. |
| `infrastructure/pilot_controls.json` | USD/project budget scope, forecast threshold, pilot allowance, uncertain pricing and inclusive job accounting. Controls remain manual/future-executor proposals. |
| New `src/financial_crime_ml/deployment/__init__.py`, `reconcile.py` | Offline seven-table CSV comparison to verified M3/M4 references: typed rows/keys/grains/currency totals, UTC instants, schema order and volume guards. No cloud calls or writes. |
| `tests/test_gcp_readiness.py`, new `tests/test_gcp_reconciliation.py` | 46 additional cases for permission separation, HCL/environment/inventory checks and functional reconciliation/negative cases. |
| `Makefile`, `pyproject.toml` | Offline check target; pytest resolves local `src` without package installation. |
| `README.md`, `docs/index.md`, `docs/architecture.md`, `diagrams/m5_deployment_readiness.mmd` | Current architecture and implemented/proposed boundaries. |
| `docs/gcp_deployment_readiness.md`, `gcp_pilot_plan.md`, `architecture_decisions/ADR-002-minimal-gcp-pilot.md`, `upgrade/capability_matrix.md`, this file | Semantic differences, permissions, load-version limitations, gates, job ledger, costs and current evidence. |

The three Terraform modules, M3 target schemas, M4 SQL/contracts and M5 transactional INSERT proposal remain in place. No tracked sample changed.

## Executed checks

| Command / check | Result |
| --- | --- |
| `python3 -m ruff check .` | All checks passed |
| `python3 -m pytest -q` | **299 passed in 27.40s**, no warnings |
| `make gcp-offline-check` | **55 passed in 3.35s** plus recursive Terraform formatting check |
| `terraform version` | 1.16.5, darwin_arm64 |
| `terraform -chdir=infrastructure/terraform fmt -check -recursive` | Passed, exit 0 |
| Installed Python HCL parser / module JSON / policy / schema checks | Passed in focused/full suites; root and both environment examples parsed |
| `terraform -chdir=infrastructure/terraform validate -no-color` | **Blocked**, exit 1: local modules not initialized |
| `terraform -chdir=infrastructure/terraform/modules/storage validate -no-color` | **Blocked**, exit 1: Google provider unavailable |
| `PYTHONPATH=src python3 -m financial_crime_ml.cli final-check` | 79 checks, zero issues |
| `PYTHONPATH=src python3 -m financial_crime_ml.cli validate-docs` | 6 historical documents, 5 diagrams, zero issues |
| `git diff --check` | Passed |
| Independent baseline SHA256 verification | **43 checked, zero mismatches**; pytest also checks before/after the suite |

Initial focused collection failed because the source-layout package was not installed. Pytest's local `src` configuration resolved it without installation. All final checks used that configuration; installed-package/CLI behavior is unchanged.

The HCL parser was already installed; TFLint/Checkov were unavailable. Formatting/parsing/policy tests do not substitute for provider validation. No `terraform init`, plan or apply ran; no provider lock exists. Provider-backed validation and an authenticated reviewed plan remain deployment blockers. The legacy documentation validator only covers its historical configured list, not every M5 page.

Ignored evidence: `outputs/runs/m5-readiness-review/checks.json`, six command logs and `verification.json` containing baseline checks and infrastructure/source hashes. The storage-module validation failure is recorded separately. These current results supersede the previous M5 document's 253-test result and earlier infrastructure fingerprint.

## Contract review and functional evidence

Inspected M3 `CURATED_FIELDS`, entity schemas and lineage; M2 money/time validators; M4 `COLUMNS`/`GRAINS`, SQLite SQL, proposed GoogleSQL and target schemas. Documented integer precision/overflow, currency exponents, canonical amount STRINGs, UTC DATE/TIMESTAMP conversion, NULL/empty distinctions, exact distinct counts, non-enforced cloud keys, join loss, casts, collation and deterministic typed reconciliation. `source_row` changes from SQLite TEXT to BigQuery INT64. At most 5,000 × 1e9 minor units = 5e12 stays inside INT64. All six supported currencies remain separate; there is no FX or cross-currency total. Beneficiary/device validation remains local M3; labels and unrelated dimensions are not added to the cloud pilot.

Reconciliation tests cover reordered rows, common BigQuery UTC formats/offsets, integers above 2^53, INT64 endpoints, empty snapshots, missing/extra tables, nulls, malformed fields, duplicate grains, per-grain corruption with unchanged currency totals, manifest/source tampering, traversal/symlinks, volume guards and nonzero CLI failure. Test exports are **simulated local CSVs**, not downloaded cloud data. A match always reports `cloud_execution_verified=false`; hashes do not authenticate fabricated export/source provenance.

Public documentation confirms that generation-qualified Storage load URIs fail in BigQuery. The plan requires unique create-only names, no concurrent writers and before/after generation/checksum checks. Versioning alone cannot pin a load. Export acquisition is separately approved paginated table reads with exact typed serialization; no downloader/executor is implemented. The 20-request/job allowance counts loads, dry runs, script parent/children. Whole-script scan caps must not be assumed. Temporary provisioner upload/load/data access requires approval and revocation before runtime mart execution.

## Inventory and permissions

**25 Terraform instances**: one private versioned STANDARD bucket; one regional LOGICAL-billed dataset; seven REQUIRED-schema tables; one runtime service account; five custom roles; two authoritative resource IAM policies; one project jobs membership; seven table memberships. Tables are curated transactions, customers, accounts and four M4 daily marts. Storage and BigQuery are the only application services. Existing provisioner, bootstrap grants, state custody and billing budget are separate prerequisites outside this inventory.

Runtime has project job-create/service-use, dataset metadata read, bucket metadata/object create/get/list, three input-table read grants and four mart read/data-update grants. No object-delete, schema/admin, billing, API-enable, key or impersonation grants are included. DML data-update can erase mart contents and requires explicit workload approval. Provisioning enumerates resource/schema/custom-role/IAM management rights; project IAM writes are privileged and can escalate. Require a dedicated sandbox, expiry and audit. Resource-local policies do not remove inherited project/folder/org IAM. Cleanup deletion rights are a separate approval/add-on.

## Costs, risks and readiness status

Public primary pricing/location, load/script, table-read, budget and provider IAM pages were inspected on 2026-10-30. Both data services list London. No project, effective IAM, quota, allocation, organization policy or billing/free-tier eligibility was queried. Dynamic price selectors did not establish a London quote; account-specific values require manual verification.

Illustrative rates: US$10/TiB queries, US$0.05/GiB-month storage for either service, US$0.01/1,000 class-A and US$0.001/1,000 class-B operations. At 10 GiB queries/month, 1 GiB BigQuery storage, 100 MiB versioned Storage and 100 operations of each class, arithmetic is **US$0.15364/month**, allowing approximately **US$0.16**, excluding transfer/tax/other account activity and without free allowances. This is a planning scenario, not a regional quote. Propose a US$1 pilot allowance and US$5 monthly project-filtered alerts-only budget at 50/90/100% actual and 100% forecast, with confirmed existing billing email recipients. **Budget alerts are not automatic spending caps.** Data/job/retention limits are not enforced by Terraform or IAM.

Deletion guards, versioning, seven-day soft delete and no destructive lifecycle rules protect evidence while retaining costs. Cleanup reviews every generation, guard removal, exact deletions, soft-deleted objects and BigQuery time-travel/fail-safe data. Delayed charges can continue after visible resources disappear.

- **Implemented locally:** M1–M4 plus M5 offline policy/schema/HCL checks and CSV reconciliation.
- **IaC prepared for deployment review:** modules/resources/IAM/schemas pass offline checks; provider validation, lock file, concrete inputs and saved plan remain required. Fully verified deployment-ready infrastructure is not claimed.
- **Proposed cloud functionality:** initial loads, transactional SQL, effective permissions, export acquisition and reconciliation remain cloud-untested.
- **Actual GCP deployment:** none; no resources created, APIs enabled or paid services invoked.

Separate explicit approvals required:

1. **A — prerequisites/planning:** named project/billing account/region/names, identities/expiring bootstrap grants, state, provider download and read-only planning/API access. Enumerate any API enablement separately.
2. **B — infrastructure:** reviewed saved-plan SHA256, provider lock/tfvars/IAM/resource/cost inventory, billing safeguards and apply window.
3. **C — workloads:** synthetic manifests/hashes/counts, schemas/rendered SQL/object names, temporary upload/load grants and revocation, 24-hour bounded execution and evidence acquisition.
4. **D — cleanup:** named inventory, retained evidence, deletion grants/guard changes and reviewed saved destroy plan.

Approval for one gate does not authorize the others. Current authorization covers only local M5 preparation. Remaining risks are privileged provisioning IAM writes, inherited access, temporary load privileges, manual spend/jobs/concurrency enforcement, untested cloud/provider behavior, unsigned evidence, retention/delayed billing and synthetic-data validity. Stop after M5.
