# M6Q controlled recovery: bootstrap audit configuration

Date 2026-11-10. Baseline `main` at `5af2c1a` (M6P committed). Project `ebank-pilot-dev-261031-k7m4`, region `europe-west2`.

**Offline change only.** M6Q did none of the following:
- call gcloud or any cloud API;
- run Terraform `init`, `plan`, `apply`, `refresh` or any `state` command;
- read or touch the real custody state, saved plans or backups, or OneDrive;
- change IAM or billing;
- commit or push.

## Incident (as reported by the operator)

| Item | Detail |
| --- | --- |
| Action | First bootstrap apply of the approved M6P saved plan (11 creates) |
| Succeeded (10) | 2 keyless service accounts (`bankpilot-dev-runner`, `bankpilot-dev-provisioner`); 7 custom roles (`bankpilot_dev_{jobs,metadata,objects,readers,tables,provision,impersonate}`); the additive `google_project_iam_member.runner_jobs` |
| Failed (1) | `google_project_iam_audit_config.iamcredentials`: the API does not accept an independent service-level auditConfig for `iamcredentials.googleapis.com` |
| Not affected | Provisioner access stayed disabled (no `google_project_iam_member.provisioner`, no `operator_impersonation`); no Owner or other binding changed; no keys exist |
| State | Bootstrap state serial 12, AES-256-GCM backed up to OneDrive and download-verified by the operator (M6M procedure) |
| Root cause | The M6J/M6K design assumed IAM Credentials audit logs could be scoped to their own service entry. Google's documentation (cited by the operator) says Service Account Credentials API Data Access logging is configured through `iam.googleapis.com` or `allServices`. M6P's review and checker repeated the assumption; offline tests could not detect an API-side constraint |

**Security impact while partial:** token minting is not yet Data Access-logged. Nothing can mint a provisioner token yet: provisioner access is disabled and no impersonation grant exists. **The access window stays blocked until the audit configuration is applied.**

## Change (minimum scope)

| File | Change |
| --- | --- |
| `infrastructure/terraform/bootstrap/main.tf` | `google_project_iam_audit_config.iamcredentials`: `service` changes from `iamcredentials.googleapis.com` to **`iam.googleapis.com`**, plus a comment. Still `ADMIN_READ` and `DATA_READ`, no exempted members, still `count = var.token_audit_logging_enabled ? 1 : 0`. **The resource address is unchanged**, so the recovery plan adds exactly this instance and needs no `moved` block |
| `infrastructure/terraform/bootstrap/variables.tf` | `token_audit_logging_enabled` description only (same name, type and default `true`) |
| `scripts/bootstrap_plan_check.py` (new, stdlib) | Offline saved-plan checker with `--mode initial` (11 creates) and **`--mode recovery`** (10 unchanged plus 1 create). Supersedes the M6P inline checker, which hard-coded the rejected service |
| `tests/test_bootstrap_plan_check.py` (new) | 33 tests (below) |
| `tests/test_gcp_iam_separation.py` | The audit block must use `iam.googleapis.com`; neither the rejected service name nor `allServices` may appear in the root |
| `Makefile` | `gcp-offline-check` includes the new tests |
| `docs/upgrade/m6p_validation.md` | Supersession note on the audit row and the step 4 checker |
| `docs/index.md`, this file | Link and evidence |

Unchanged:
- every other resource;
- the additive-only rule (no `google_project_iam_policy`, `_binding`, keys or `google_project_service`);
- `prevent_destroy` on the service accounts and roles;
- disabled provisioner access;
- the custody wrapper, sentinel backends and lockfile.

**Why `iam.googleapis.com` rather than `allServices`:** it is the narrowest entry that covers token minting. `allServices` would turn on Data Access logs for every API in the project (BigQuery and Storage reads included), with far more volume and cost, and its scope would exceed the M6J F7 approval.

## Recovery checker (`scripts/bootstrap_plan_check.py --mode recovery`)

The checker passes only if every condition below holds.
- **Variables** are the approved values: the project, `prefix = bankpilot`, `environment = dev`, `provisioner_access_enabled = false` and `token_audit_logging_enabled = true`.
- **The 10 existing resources** are present as `no-op` at their exact addresses.
- **There is exactly one create,** `google_project_iam_audit_config.iamcredentials[0]`, with:
  - `service = iam.googleapis.com`;
  - log types exactly `ADMIN_READ` and `DATA_READ`;
  - no exempted members;
  - this project.
- **Nothing else changes:** no update, replace, delete or extra create, no data source, no `resource_drift`, and the plan has not `errored`.
- **Values match the repository:** custom-role permissions equal the repository JSON; the runner member is unconditional and bound to `bankpilot_dev_jobs` for `bankpilot-dev-runner`; the account IDs are the expected ones.
- **No forbidden text anywhere in the plan JSON:**
  - `roles/owner`, `roles/editor`, `allUsers`, `allAuthenticatedUsers`, `allServices`, `iamcredentials.googleapis.com`;
  - policy or binding resources, service-account keys, `google_project_service`, `impersonate_service_account`.

It prints only the mode, counts, create addresses, the unchanged count, problems and `ok`. No variable values are printed.

## Tests

`tests/test_bootstrap_plan_check.py`, **33 passed**:
- **Good plans:**
  - recovery gives `ok`, `{"google_project_iam_audit_config": 1}`, 1 create and 10 unchanged;
  - initial gives `ok` with 11 creates (2 service accounts, 7 roles, 1 member, 1 audit config);
  - neither mode accepts the other's plan.
- **17 adversarial recovery plans, all rejected:**
  - the old `iamcredentials` service, `allServices`, missing `DATA_READ`, extra `DATA_WRITE`, exempted member;
  - widened role update, service-account replace, role delete;
  - provisioner access enabled, audit flag off;
  - drift, a missing existing resource, another project, an Owner member, a data source, an errored plan, a rebound runner member.
  - An audit-config replace is also rejected.
- **Each check tested on its own:**
  - non-create actions are named;
  - each variable is checked separately;
  - a missing variable is rejected;
  - forbidden audit services are rejected anywhere in the JSON, including outputs.
- **Consistency with Terraform:** the real `main.tf` audit block uses `iam.googleapis.com`, `ADMIN_READ`/`DATA_READ`, no exemptions and the flag-gated `count`, and the checker's durable set has 10 addresses. The existing static count test still gives 10 + 1 = **11** (13 with access).
- **CLI:** exit 0 for ok, 1 for problems, 2 for invalid JSON, a non-object or a missing `--mode`. It runs under `python3 -I`.

**Mutation check:** 8 mutations of the checker, all detected (4, 1, 1, 1, 2, 5, 1 and 2 failures):
- old service accepted;
- exemption check removed;
- drift check removed;
- non-create actions allowed;
- any log types accepted;
- variable check removed;
- missing-existing check removed;
- forbidden-service text check removed.

The file was restored byte-identical after each.

## Validation

| Check | Result |
| --- | --- |
| Full `pytest` | **620 passed, 1 skipped** |
| `make gcp-offline-check` | **376 passed, 1 skipped**; `terraform fmt` passed |
| `ruff check .` | Passed |
| `terraform fmt -check -recursive` (both roots) | Passed |
| `terraform validate` (existing repository provider caches, no `init`) | Both roots valid |
| Docs validator (`PYTHONPATH=src`) | Passed, 0 issues |
| Hygiene | `git diff --check` clean; nothing staged; no state, plan or tfvars in the repository; custody and backups not read |

## Expected recovery plan behaviour

With the unchanged inputs file (`provisioner_access_enabled = false`, `token_audit_logging_enabled = true`) and state serial 12:
- `plan` refreshes the 10 existing resources: these are read-only API calls;
- Terraform reports **`1 to add, 0 to change, 0 to destroy`**: `google_project_iam_audit_config.iamcredentials[0]` with `iam.googleapis.com`, `ADMIN_READ` and `DATA_READ`;
- the checker reports `ok: true`, with `counts {"google_project_iam_audit_config": 1}`, `creates 1` and `unchanged 10`.

On apply, the provider reads the project IAM policy and writes it back with only the `iam.googleapis.com` auditConfig entry added, guarded by the policy etag. Bindings are not modified. State then moves past serial 12, with 11 resources.

**Stop and do not apply if any of these occur:**
- any change other than that one create;
- any drift;
- a non-zero checker exit;
- a `-/+` or `~` on any address.

All of them mean the state or the project differs from the recorded incident.

## Residual risks

1. **API behaviour is unverified offline.** Acceptance of `iam.googleapis.com` rests on the Google documentation the operator cited. M6Q could not test it, and the saved plan cannot prove it either. If apply fails again, nothing else changes: the 10 resources stay as they are, and access remains disabled.
2. **The audit config is authoritative for its service entry.** `google_project_iam_audit_config` replaces any existing `iam.googleapis.com` auditConfig (for example one set in the console, or its exemptions). *Operator pre-check:* inspect `auditConfigs` in the project policy, read-only. If an `iam.googleapis.com` or `allServices` entry already exists, stop for review. Organisation- or folder-level audit configs are unaffected and still apply.
3. **Broader logging than intended.** `DATA_READ` on IAM also logs other IAM read calls, such as service-account and role gets/lists and Terraform refreshes. The volume is small at pilot scale, but ingestion pricing is unverified.
4. **The address name `iamcredentials` is now historical.** It was kept deliberately for state stability. A rename later would need a `moved` block and its own plan review.
5. **Partial state.** Until the recovery apply, token minting is not Data Access-logged. This is mitigated because no identity can mint provisioner tokens: access is disabled.
6. **Carried over from M6P:** P2 (custom-role permission support), P4 (token identity), the regex-based provider check, and wrapper bypass.

## Recommended operator steps (none run here)

1. **Review and commit M6Q.**
2. **Offline, in Terminal.app:** `python3 scripts/terraform_custody.py run --root bootstrap -- state list`. Expect exactly the 10 addresses and no audit config.
3. **Optional read-only pre-check:** the project policy's `auditConfigs` has no existing `iam.googleapis.com` or `allServices` entry (risk 2).
4. **New saved plan:** M6P steps 0–3 with a new timestamped `$P`, which picks up the new commit hash. Then step 4 with the new checker:
   ```sh
   env -i HOME="$HOME" PATH="$PATH" python3 scripts/terraform_custody.py run --root bootstrap -- show -json "$P" \
     | python3 scripts/bootstrap_plan_check.py --mode recovery
   ```
   Report only the SHA-256, the checker JSON and "1 to add, 0 to change, 0 to destroy".
5. **Separate apply approval** for that exact SHA-256. Then, **immediately**, run `state_backup.py create --root bootstrap`, the OneDrive upload and `verify-download`. Keep the serial-12 backup until the new one is verified and retention allows it.
6. **Access window:** a separate gate, opened only after the audit config is confirmed in place.

**Stopped after M6Q.**
