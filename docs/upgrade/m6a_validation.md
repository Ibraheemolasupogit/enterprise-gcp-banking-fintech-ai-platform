# M6A Terraform provider validation — completed

Date: 2026-10-30. Starting revision: `3b2f91e` (`infra: strengthen GCP pilot IAM and deployment controls`). Initial Git status was clean. After the offline phase, the user explicitly approved official Google 7.31.x provider resolution/download, initialization without a backend, provider-backed validation, necessary defect-only fixes and lockfile preservation. This milestone does not repeat or change M5 architecture, IAM or safeguards.

## Configuration inspected

| Item | Actual configuration |
| --- | --- |
| Root | `infrastructure/terraform/`, containing `main.tf` and `variables.tf` |
| Terraform requirement | `>= 1.9, < 2.0` |
| Provider | `registry.terraform.io/hashicorp/google`, constraint `~> 7.31.0` (stable releases `>=7.31.0,<7.32.0`) |
| Child modules | `./modules/identity`, `./modules/storage`, `./modules/bigquery`; all are local JSON-syntax Terraform modules |
| Child provider requirements | No separate declarations; resource types imply `hashicorp/google`, using the root's configured default provider and version constraint |
| Environment examples | `environments/dev.tfvars.example`, `environments/test.tfvars.example`; neither is used for validation |
| Backend | No `backend` or `cloud` block; implicit local state. Executed initialization explicitly disabled backend initialization |
| Provider authentication configuration | Existing project/region/impersonation input references; no credential values supplied or authentication performed |
| Before initialization | No root `.terraform/`, `.terraform.lock.hcl`, or Google provider in the inspected standard local plugin/cache paths; no Terraform CLI config or cache/data-directory override configured |

The repository contains no additional Terraform root or external module source. Neither offline checks nor provider-backed validation found a configuration defect. No Terraform configuration, permissions, safeguards, interfaces or application code was changed.

## Offline checks performed before any downloads

| Check | Result |
| --- | --- |
| `terraform -chdir=infrastructure/terraform fmt -check -recursive` | Passed, exit 0 |
| `python3 -m pytest -q tests/test_gcp_readiness.py` | **12 passed in 0.51s**, including root/example HCL parsing, module JSON, target schemas, inventory and static policy checks |
| `python3 -m ruff check .` | All checks passed |
| `python3 -m pytest -q` | **299 passed in 28.21s**, no warnings |
| Baseline artifact SHA256 checks before/after execution | **43 unchanged**; full pytest also verifies its baseline session fixture |
| `git diff --check` | Passed |

Existing tooling: Terraform **1.16.5**, platform **darwin_arm64**; Ruff **0.16.4**; pytest **8.4.1**; HCL parser **bc-python-hcl2 0.4.2** (import name `hcl2`). TFLint and Checkov are unavailable and were not installed. An initial metadata-inspection command used the wrong distribution name `python-hcl2`; querying the installed import-to-distribution mapping identified `bc-python-hcl2`. This did not affect any validation check.

Static target-schema tests alone are not Google provider schema validation. The approved initialization and provider-backed checks below completed after this offline phase.

Ignored command logs and machine-readable evidence: `outputs/runs/m6a-provider-validation/offline-checks.json`, `terraform-fmt.log`, `static-policy-schema-hcl.log`, `ruff.log`, `pytest.log`.

## Approved initialization and provider integrity

Only **HashiCorp Google 7.31.0, darwin/arm64** was installed. The original `~> 7.31.0` constraint remains unchanged. The three local modules were registered without external module downloads; no Terraform CLI, Python package or other provider was installed.

Terraform initialization resolved the provider using Registry metadata, downloaded the official release and checked its signed checksums. The provider archive is `terraform-provider-google_7.31.0_darwin_arm64.zip`; its release checksum manifest is `terraform-provider-google_7.31.0_SHA256SUMS`. Terraform reported **`Installed hashicorp/google v7.31.0 (signed by HashiCorp)`** and successful initialization. Only the approved platform archive was installed; checksum entries for other release platforms do not imply additional provider installations.

The generated `infrastructure/terraform/.terraform.lock.hcl` contains exactly one provider selection, version `7.31.0`, constraint `~> 7.31.0`, one installed-package `h1` checksum and 12 signed-release `zh` archive checksums. It is preserved exactly as generated, eligible for version control, and has not been staged or committed. No prior lockfile existed.

Executed commands (without environment tfvars or credential configuration):

```sh
CHECKPOINT_DISABLE=1 TF_IN_AUTOMATION=1 terraform -chdir=infrastructure/terraform init -backend=false -input=false -no-color
CHECKPOINT_DISABLE=1 TF_IN_AUTOMATION=1 terraform -chdir=infrastructure/terraform validate -no-color
```

The first initialization attempt failed at sandbox DNS resolution for `registry.terraform.io`. The approved operation succeeded after sandbox escalation. Initial sandboxed validation then failed during the provider's local plugin handshake; the same validation outside the sandbox succeeded without configuration changes. These were execution-environment restrictions, not Terraform defects. Initial failure logs are retained alongside successful results. No GCP authentication or GCP API calls were made.

| Integrity check | Result |
| --- | --- |
| Provider signing | Verified by Terraform; `signed by HashiCorp` in initialization log |
| Installed package hash | `h1:R+PzXlPKoFadZ/MibOxxy1waEutPTJuXH8gi3oO4Ck8=` |
| Independent package check | Recomputed the installed directory's Go-style `h1` file hash; exactly matches the generated lockfile |
| Lockfile SHA256 | `61cea4a18251f6789ce1c68326cc6f0ec7d3209936c7fb7e913a9fdabc1c0fef` |
| Provider/backend files | Provider and module metadata under ignored `.terraform/`; no Terraform state file created |

## Provider-backed and final checks

| Check | Actual result |
| --- | --- |
| Approved `init -backend=false -input=false -no-color` | Exit 0; Google 7.31.0 installed and lockfile created |
| Provider-backed `validate -no-color` | Exit 0: **Success! The configuration is valid.** No warnings or defects |
| Post-initialization `fmt -check -recursive` | Passed, exit 0 |
| Post-initialization static HCL/schema/policy tests | 12 passed; no safeguards changed |
| Post-initialization Ruff | All checks passed |
| Full pytest before initialization | 299 passed in 28.21s; no application/test code changed afterward |
| Final baseline sample hashes | All 43 unchanged |
| Final `git diff --check` | Passed |

The full suite was not repeated after installing the provider: no source/test/configuration changes required it. Relevant static policy/schema tests and Ruff were rerun after initialization. No defect-only fixes were necessary.

Evidence is ignored under `outputs/runs/m6a-provider-validation/`: pre-initialization checks/logs; `terraform-init-sandbox.log`; successful `terraform-init.log`; `terraform-validate-initial.log`; successful `terraform-validate.log`; `provider-integrity.json`; post-initialization command logs and `post-init-checks.json`; final `verification.json`. The provider-integrity record includes installed filenames, individual SHA256 digests, computed `h1`, lockfile SHA256 and an empty backend-state-file inventory.

## Final scope, Git status and remaining risks

**M6A is complete.** Reviewable additions are this document and `infrastructure/terraform/.terraform.lock.hcl`; both are untracked/unstaged on `main` at completion. No tracked file was modified. Local `.terraform/` and evidence logs remain ignored. Existing interfaces, all 43 samples, safeguards and Git history are preserved. No plan, apply, destroy, GCP authentication/API calls, cloud resources, billing/API changes, unrelated dependencies, later milestones, commit or push occurred.

No M6A blocker remains. Validation establishes syntax, provider schemas and configuration consistency; it does not verify actual environment values, effective IAM, organization policy, API availability, quotas, project/billing state, workload SQL or runtime behavior. Those remain outside this milestone and require separate authorization. Only darwin_arm64 execution was validated; the signed manifest's other platform checksums are not evidence that those platforms were executed. The lockfile constrains subsequent normal initialization to the reviewed selection unless intentionally updated. Stop after M6A.
