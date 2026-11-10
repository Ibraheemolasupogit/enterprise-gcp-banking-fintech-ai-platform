# M6P GCP IAM bootstrap security review (read-only)

Date 2026-11-10. Baseline `9124348` (M6O committed; the user reports that CI passed, the custody tree exists, and the bootstrap/pilot `init`, `validate` and run-phase preflights passed). Project `ebank-pilot-dev-261031-k7m4`, region `europe-west2`.

**Review only.** M6P did none of the following:
- call gcloud or any GCP API;
- run Terraform plan, apply, destroy or refresh;
- create a credential, token or state;
- change IAM or billing;
- touch OneDrive;
- commit or push.

Cloud facts come from earlier evidence ([M6I](m6i_validation.md) and [M6J](m6j_review.md)) and were **not re-queried**.

## Verdict

**Ready for the operator to generate a saved bootstrap plan, subject to the approvals at the end.** The medium finding P1 (ambient credential environment) is **fixed in code** (see [P1 remediation](#p1-remediation-2026-11-10)) and is additionally covered by the `env -i` commands. No open finding blocks plan generation.

## Scope reviewed

- **Bootstrap root:** [`bootstrap/main.tf`](../../infrastructure/terraform/bootstrap/main.tf), [`variables.tf`](../../infrastructure/terraform/bootstrap/variables.tf) and the placeholder `dev.tfvars.example`.
- **Role sources:** [`infrastructure/iam/*.json`](../../infrastructure/iam/).
- **Pilot provider and identity postconditions.**
- **Records and tests:** ADR-003, M6J, M6K and the M6L–M6O custody tooling, plus `tests/test_gcp_iam_separation.py` and `tests/test_terraform_custody.py`.
- **Pinned provider binary:** Google 7.31.0 (darwin_arm64), whose environment-variable names were inspected offline with `strings`.

## Comparison with the approved M6K design

| Item | M6K / ADR-003 | Repository now | Status |
| --- | --- | --- | --- |
| Separate root, own state, no impersonation in the provider block | Required | `provider "google" { project = var.project_id }`. State in custody `state/bootstrap/` behind the M6O sentinel backend | **Matches** |
| Additive members only; no `*_iam_policy`/`*_iam_binding`, keys, `google_project_service`, Owner or Editor | Required | 9 resource blocks, all additive; test-enforced | **Matches** |
| Provisioner role | 20 permissions, no administration | 20, identical; disjoint from the 23 tested administrative permissions; no wildcards | **Matches** |
| Impersonate role | `iam.serviceAccounts.getAccessToken` only, bound on the provisioner SA resource only | Same; single `user:` principal validated | **Matches** |
| Runtime roles | 5 roles; only `jobs` bound at project scope | jobs 2, metadata 1, objects 4, readers 2, tables 3 permissions; no wildcards | **Matches** |
| Grants disabled by default; ≤ 8 h expiry | Required | `access_count` gate; `access_expiry` validated against `plantimestamp()` | **Matches** (validation only statically tested) |
| **Instance count** | ADR-003: "11, or 13 with access enabled" | Static count: **10 core** + **1 audit config (default on) = 11**; access enabled adds 2 = **13** | ADR-003 correct. **M6K evidence ("11 creates … and the audit config") and the pilot plan ("11 … plus the audit config") overcount by one.** The pilot plan is corrected in M6P; M6K is historical evidence, recorded here as an erratum |

## Exact permissions (all project-level custom roles; no wildcards)

| Role ID (`prefix=bankpilot`, `env=dev`) | Permissions | Bound to (first bootstrap) |
| --- | --- | --- |
| `bankpilot_dev_jobs` | `bigquery.jobs.create`, `serviceusage.services.use` | Runner, project, **unconditional** |
| `bankpilot_dev_metadata` | `bigquery.datasets.get` | Not bound (pilot root binds at resource level) |
| `bankpilot_dev_objects` | `storage.buckets.get`, `storage.objects.create/get/list` | Not bound |
| `bankpilot_dev_readers` | `bigquery.tables.get/getData` | Not bound |
| `bankpilot_dev_tables` | `bigquery.tables.get/getData/updateData` | Not bound |
| `bankpilot_dev_provision` | `resourcemanager.projects.get/getIamPolicy`, `serviceusage.services.use`, `iam.roles.get`, `iam.serviceAccounts.get`, `storage.buckets.create/get/update/getIamPolicy/setIamPolicy`, `bigquery.datasets.create/get/update/getIamPolicy/setIamPolicy`, `bigquery.tables.create/get/update/getIamPolicy/setIamPolicy` (20) | **Not bound** (`provisioner_access_enabled = false`) |
| `bankpilot_dev_impersonate` | `iam.serviceAccounts.getAccessToken` | **Not bound** |

**Bootstrap administrator needs** (the sole Owner holds all of them):
- `iam.serviceAccounts.create/get`;
- `iam.roles.create/get`;
- `resourcemanager.projects.get/getIamPolicy/setIamPolicy`, for the runner member and the audit config;
- later, for the access window, `iam.serviceAccounts.getIamPolicy/setIamPolicy`.

## Expected changes: first bootstrap plan

Inputs: `provisioner_access_enabled = false`, `token_audit_logging_enabled = true`.

| Address | Action |
| --- | --- |
| `google_service_account.runner` (`bankpilot-dev-runner`), `google_service_account.provisioner` (`bankpilot-dev-provisioner`) | 2 × create (keyless, `prevent_destroy`) |
| `google_project_iam_custom_role.runtime["jobs"\|"metadata"\|"objects"\|"readers"\|"tables"]`, `.provision`, `.impersonate` | 7 × create (`prevent_destroy`) |
| `google_project_iam_member.runner_jobs` | 1 × create (additive, unconditional) |
| `google_project_iam_audit_config.iamcredentials` | 1 × create: ADMIN_READ and DATA_READ for `iamcredentials.googleapis.com` only. **Superseded by M6Q:** the API rejected this service entry; the service is now `iam.googleapis.com` ([M6Q](m6q_validation.md)) |
| **Total** | **11 to add, 0 to change, 0 to destroy.** The Owner binding and every other binding are untouched |

The later access window (a separate approval) re-plans with `provisioner_access_enabled = true` and an exact UTC `access_expiry` no more than 8 h ahead. That adds the conditional `google_project_iam_member.provisioner` and `google_service_account_iam_member.operator_impersonation`: 2 creates, 0 changes.

## Security findings

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| P1 | **Medium** | **Ambient credential environment reaches the bootstrap provider.** The M6O wrapper strips only `TF_*`. The pinned provider reads `GOOGLE_IMPERSONATE_SERVICE_ACCOUNT`, `GOOGLE_CREDENTIALS`, `GOOGLE_CLOUD_KEYFILE_JSON`, `GCLOUD_KEYFILE_JSON`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_BILLING_PROJECT`, `USER_PROJECT_OVERRIDE` and others (strings present in the binary). A stray `GOOGLE_IMPERSONATE_SERVICE_ACCOUNT` would make the bootstrap impersonate, contradicting ADR-003, and the static provider-block tests cannot see it. Key-file variables could switch identity if the access token were absent | **Fixed (2026-11-10):** fail-closed credential policy, allow-listed child environment, provider-identity checks and token redaction in `terraform_custody.py`; 123 new tests; mutation-tested. The `env -i` commands remain as a second layer |
| P2 | Medium | **Custom-role permission support is not verified.** A permission unsupported in custom roles fails at **apply**, possibly after other resources were created (partial apply). `prevent_destroy` then makes rollback a reviewed code change | Optional read-only pre-check (approval 4): `gcloud iam list-testable-permissions` for the project, compared with the 33 role permissions. Otherwise accept and stop on the first error; **never substitute or widen a permission** |
| P3 | Medium (carried, M6K risk 1) | The provisioner's project-wide bucket/dataset/table `setIamPolicy`/`update` can grant pilot data access or relax public-access prevention | Not exercised by the first bootstrap (access disabled). Accepted design; re-review at the access-window gate |
| P4 | Low | **Tooling cannot prove the token's identity.** A plan with an empty state makes few API calls, so a non-Owner token could plan cleanly and then fail mid-apply | Operator checks `gcloud config get-value account` (local configuration read) before plan and before apply. Apply errors are stop conditions |
| P5 | Low | **Quota attribution for user tokens.** gcloud user access tokens are not tied to this project; some APIs may demand a quota project | If an error says so, **stop**. The remedy (`user_project_override = true`, `billing_project = var.project_id`) is a reviewed provider change, not an ad-hoc environment variable (P1) |
| P6 | Low | **Runner `bigquery.jobs.create` is unconditional at project scope.** Anyone able to act as the runner could run billed queries. No impersonation binding exists for the runner, and only the Owner could create one | Accepted; the GBP 5 budget alerts but is **not a cap** |
| P7 | Info | **IAM eventual consistency.** A newly created SA may briefly be "not found" when its member is added | Re-plan and re-apply after a short wait under the same approval; never change permissions to work around it |
| P8 | Info (carried, F4) | The Owner is unrestricted. Separation limits token blast radius, not the human | Unchanged |

**Not found:**
- wildcards;
- predefined broad roles;
- `actAs`, `signBlob`/`signJwt`, `implicitDelegation` or key permissions;
- authoritative policy resources;
- impersonation in the bootstrap provider block;
- runner impersonation;
- API enablement;
- project-level token minting.

## Identity separation

| Identity | Acts as | Holds after the first bootstrap |
| --- | --- | --- |
| Bootstrap administrator (human, sole Owner) | Itself, via a per-process `GOOGLE_OAUTH_ACCESS_TOKEN` from gcloud; no ADC file; no impersonation (enforced by `env -i`) | Owner (unchanged) |
| Provisioner SA | Nobody yet | Nothing: the role exists, no binding |
| Runner SA | Nobody (no impersonation binding) | `bankpilot_dev_jobs` at project scope |
| Operator as impersonator | — | Nothing until the access-window gate |

Pilot-root separation is unchanged (M6K): the pilot provider impersonates the provisioner, and its postconditions fail on role drift or extra grants.

## Variables, provider identity, state and plan handling

- **Inputs** go in custody `inputs/bootstrap-dev.tfvars` (0600). The wrapper accepts `-var-file` only from there. They contain the operator's email, which is why they are not in Git:
  - `project_id = "ebank-pilot-dev-261031-k7m4"`, `environment = "dev"`, `prefix = "bankpilot"`;
  - `operator_member = "user:<operator email>"`, required by validation even while access is disabled;
  - `provisioner_access_enabled = false`, `access_expiry = null`;
  - `token_audit_logging_enabled = true` (a decision; see approvals).
- **Provider identity.** The token comes only from the environment and never from a variable, so it is **not written into the saved plan or state**. It lasts at most about 1 h, and its scope is the Owner session. Since the P1 fix, the wrapper **requires** a well-formed token for `plan`/`apply`, **refuses** it for offline commands, passes it only to the Terraform process, and never prints it.
- **Plan file.** `plans/bootstrap/<UTC>-<gitsha>.tfplan` is 0600 and enforced by the wrapper.
  - It contains the operator email, SA names, project ID and role definitions: confidential, but no secrets.
  - Never paste `terraform show` output into Claude, chat or evidence. Record only the SHA256 and the checker summary below.
  - Delete the plan after the apply is verified, unless retention in custody is approved.
- **Bootstrap state** (after apply) contains SA emails and unique IDs, role definitions, members (the operator email once access is enabled), the audit config and etags. It holds no keys or tokens and is integrity-critical. **Back it up immediately after apply** (`state_backup.py create --root bootstrap`, then upload and download-verify).
- **Plan must not leave state.** After the plan, `state/bootstrap/` must contain no non-empty `terraform.tfstate`. Verify with step 5 below.

## APIs, costs and failure points

- **APIs** (enabled in M6I, not re-verified):
  - `iam.googleapis.com`, for SAs and roles;
  - `cloudresourcemanager.googleapis.com`, for the project policy, members and audit config;
  - `iamcredentials.googleapis.com`, needed only later for impersonation.

  No API enablement is part of the bootstrap.
- **Expected cost: £0.** Service accounts, custom roles, IAM bindings and Admin Activity logs carry no charge. The IAM Credentials Data Access logs fall far inside the Cloud Logging free ingestion allotment at pilot scale (pricing not re-verified). The GBP 5 budget remains alert-only.
- **Likely failure points:**
  - a token that is wrong, expired or not the Owner's (P4);
  - quota-project errors (P5);
  - permission support (P2);
  - SA propagation (P7);
  - SA or role IDs that are reserved or still soft-deleted from earlier attempts. Role IDs stay reserved for 7–44 days after deletion; an earlier attempt would make the plan show the role as new but the apply would fail;
  - an etag conflict on concurrent project-policy edits;
  - a stale saved plan, which forces a re-plan.

  **Every one of these is a stop condition.**

## Operator-only commands: NOT executed, and not approved to run yet

Run in **Terminal.app**, never in Claude Code, from the repository root, with M6P reviewed. `C` is the custody root.

```sh
# 0. Context and identity (local reads only)
cd ~/Developer/enterprise-gcp-banking-fintech-ai-platform
C="$HOME/.secure/ebank-pilot-dev-261031-k7m4"
git status --porcelain                       # expect empty; note: git rev-parse --short HEAD
gcloud config get-value account              # must be the sole-Owner operator account
# In your normal shell, so that stray GOOGLE_*/CLOUDSDK_*/proxy variables or a ~/.terraformrc are
# reported by name (values are never printed). It must print no problems; otherwise unset them.
python3 scripts/terraform_custody.py preflight --root bootstrap --phase run

# 1. Inputs (once; edit the email; never commit)
umask 077; set -o noclobber
cat > "$C/inputs/bootstrap-dev.tfvars" <<'EOF'
project_id                  = "ebank-pilot-dev-261031-k7m4"
environment                 = "dev"
prefix                      = "bankpilot"
operator_member             = "user:OPERATOR_EMAIL_HERE"
provisioner_access_enabled  = false
access_expiry               = null
token_audit_logging_enabled = true
EOF
set +o noclobber; stat -f '%Lp %N' "$C/inputs/bootstrap-dev.tfvars"   # expect 600

# 2. Saved plan in a clean environment: only HOME, PATH, TERM and a per-process token.
#    The token is fetched inside the child shell, so it never appears in argv or history.
#    The wrapper re-checks it: a token is required and format-checked for plan/apply, and any
#    other credential, impersonation, key-file, quota, metadata or proxy variable is refused.
#    If gcloud prints an error or the wrapper reports the token malformed: STOP.
P="$C/plans/bootstrap/$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD).tfplan"
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" /bin/sh -c '
  GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)" || exit 1
  export GOOGLE_OAUTH_ACCESS_TOKEN
  exec python3 scripts/terraform_custody.py run --root bootstrap -- \
    plan -input=false -var-file="$1" -out="$2"' sh "$C/inputs/bootstrap-dev.tfvars" "$P"

# 3. Record (non-secret): hash and mode
shasum -a 256 "$P"; stat -f '%Lp' "$P"                                  # expect 600

# 4. Review: human diff in your own terminal, then the summary checker (prints counts/addresses only).
#    SUPERSEDED (M6Q): this inline checker expects the rejected iamcredentials service. Use
#    scripts/bootstrap_plan_check.py --mode recovery|initial instead (docs/upgrade/m6q_validation.md).
#    No token here: show is offline, and the wrapper refuses a token for offline commands.
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" python3 scripts/terraform_custody.py run --root bootstrap -- show "$P"
env -i HOME="$HOME" PATH="$PATH" python3 scripts/terraform_custody.py run --root bootstrap -- show -json "$P" \
  | python3 /dev/fd/3 3<<'PY'
# M6P-CHECKER-BEGIN
import json, sys
plan = json.load(sys.stdin)
project = "ebank-pilot-dev-261031-k7m4"
want = {"google_service_account": 2, "google_project_iam_custom_role": 7,
        "google_project_iam_member": 1, "google_project_iam_audit_config": 1}
iam = "infrastructure/iam/"
roles = {f"bankpilot_dev_{k}": set(v["permissions"])
         for k, v in json.load(open(iam + "runtime_roles.json")).items()}
for f, rid in (("provisioner", "provision"), ("impersonate", "impersonate")):
    roles[f"bankpilot_dev_{rid}"] = set(json.load(open(f"{iam}{f}.role.json"))["includedPermissions"])
counts, problems = {}, []
for rc in plan.get("resource_changes", []):
    actions, kind, after = rc["change"]["actions"], rc["type"], rc["change"].get("after") or {}
    if actions == ["no-op"]:
        continue
    counts[kind] = counts.get(kind, 0) + 1
    if actions != ["create"]:
        problems.append(f"{rc['address']}: {actions}")
    if after.get("project") not in (None, project):
        problems.append(f"{rc['address']}: other project")
    if kind == "google_project_iam_custom_role" and \
            set(after.get("permissions") or []) != roles.get(after.get("role_id"), {None}):
        problems.append(f"{rc['address']}: permissions differ from repository JSON")
    if kind == "google_project_iam_member" and (rc["name"] != "runner_jobs" or after.get("condition")):
        problems.append(f"{rc['address']}: unexpected project member")
    if kind == "google_project_iam_audit_config" and after.get("service") != "iamcredentials.googleapis.com":
        problems.append(f"{rc['address']}: unexpected audit service")
text = json.dumps(plan)
for bad in ("roles/owner", "roles/editor", "allUsers", "allAuthenticatedUsers",
            "google_project_iam_policy", "google_project_iam_binding",
            "google_service_account_key", "google_project_service", "impersonate_service_account"):
    if bad in text:
        problems.append(f"forbidden token present: {bad}")
if counts != want:
    problems.append(f"counts {counts} != expected {want}")
print(json.dumps({"counts": counts, "creates": sum(counts.values()),
                  "problems": problems, "ok": not problems}, indent=2))
sys.exit(0 if not problems else 1)
# M6P-CHECKER-END
PY

# 5. Plan created no state
ls -la "$C/state/bootstrap/"     # expect only workspaces/ (no non-empty terraform.tfstate)

# STOP. Send only: plan SHA256, file mode, the checker JSON and "11 to add, 0 to change,
# 0 to destroy". Apply is a separate approval:
#   env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" /bin/sh -c '
#     GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)" || exit 1
#     export GOOGLE_OAUTH_ACCESS_TOKEN
#     exec python3 scripts/terraform_custody.py run --root bootstrap -- apply "$1"' sh "$P"
# followed at once by: state_backup.py create --root bootstrap, upload, download, verify-download.
```

Optional read-only pre-checks (approval 4): run them as the operator and keep the output in custody `evidence/`, never in Git:
- `gcloud projects get-iam-policy ebank-pilot-dev-261031-k7m4 --format=json`, to compare with the M6I baseline (one Owner binding);
- `gcloud iam service-accounts list --project …` and `gcloud iam roles list --project …`, both expected empty;
- `gcloud iam list-testable-permissions //cloudresourcemanager.googleapis.com/projects/ebank-pilot-dev-261031-k7m4`, compared against the 33 role permissions (P2).

## Changes in M6P

| File | Change |
| --- | --- |
| `docs/upgrade/m6p_validation.md` (new) | This review and the P1 remediation |
| `scripts/terraform_custody.py` | **P1 fix:** `credential_problems`, `provider_problems`, `child_environment`, `_assert_no_secret`; the preflight takes the command |
| `tests/test_terraform_custody.py` | 123 new P1 tests (195 in the file) |
| `docs/terraform_state_custody.md` | Preflight list gains the credential policy |
| `docs/gcp_pilot_plan.md` | Bootstrap instance count corrected (10 core, 12 with access; plus 1 audit config if enabled: 11 / 13) |
| `docs/index.md` | Link |

No Terraform, role JSON or state change.

## Offline validation

| Check | Result |
| --- | --- |
| Static bootstrap inventory (parsed from `main.tf` and `runtime_roles.json`) | 10 core + 1 audit = **11**; +2 with access = 13. No wildcard permission in any role |
| Provider binary (`strings`, offline) | Credential and identity environment-variable names present (P1 evidence) |
| Plan checker | Extracted **verbatim from this file** and run in the documented `python3 /dev/fd/3` form against synthetic `show -json` plans. The expected-good plan gave `ok: true` with 11 creates. **7/7 adversarial variants exited 1:** update, replace, widened permission, Owner member, authoritative binding, other project, impersonating provider |
| Full `pytest` | **464 passed, 1 skipped** |
| `make gcp-offline-check` | **220 passed, 1 skipped**; `terraform fmt` passed |
| Ruff; docs validator; `git diff --check` | Passed; 0 issues; clean |
| Hygiene | Nothing staged; no tracked or untracked file flagged; no state artefact under the repository; no email address in this document; checkpoint cache unchanged (no Terraform egress) |

## P1 remediation (2026-11-10)

**Policy** (`terraform_custody.py`). The checks are fail-closed and command-specific. They run in every preflight and every `run`.

| Control | Behaviour |
| --- | --- |
| Approved credential | `GOOGLE_OAUTH_ACCESS_TOKEN` only:<br>- **required** for `plan` and `apply`, the only commands that contact Google;<br>- format-checked (`ya29.` shape);<br>- **refused** for offline commands (`init`, `validate`, `show`, `output`, `state`, `providers`, `version`) |
| Refused variables (named, never silently dropped) | - every other `GOOGLE_*`: impersonation, `CREDENTIALS`, `APPLICATION_CREDENTIALS`, `CLOUD_KEYFILE_JSON`, `BILLING_PROJECT`, `CLOUD_QUOTA_PROJECT`, `PROJECT`, `USE_DEFAULT_CREDENTIALS`, …;<br>- every `CLOUDSDK_*` and `GCLOUD_*`;<br>- `GCE_METADATA_*` (metadata-server spoofing) and `NO_GCE_CHECK`;<br>- `USER_PROJECT_OVERRIDE`;<br>- `HTTP(S)_PROXY`/`ALL_PROXY`/`NO_PROXY` in any case, and `SSL_CERT_FILE`/`SSL_CERT_DIR` |
| Token-run extras | `TF_LOG` refused when a token is present, because debug logs can capture request headers. An ADC file (`~/.config/gcloud/application_default_credentials.json`) is refused as an unapproved fallback identity |
| Terraform CLI configuration | `~/.terraformrc`, `~/terraform.rc`, `~/.terraform.d/plugins` and `~/Library/Application Support/io.terraform/plugins` are refused, because they could replace the provider binary or its source |
| Provider identity (static, all root `*.tf`) | Exactly one `google` provider block across the root's files. Any `override.tf`, `*_override.tf` or `*.tf.json` is refused, and no module may declare a provider.<br>- **Bootstrap:** no `impersonate_service_account(_delegates)`, `credentials`, `access_token`, `billing_project` or `user_project_override`.<br>- **Pilot:** must impersonate exactly `var.provisioner_service_account`, and sets no other identity attribute |
| Child environment | An explicit allow-list: `HOME PATH TERM LANG LC_ALL LC_CTYPE TMPDIR USER LOGNAME`, plus `TF_LOG(_PATH)`, `TF_IN_AUTOMATION` and `TF_INPUT`, plus the wrapper's `TF_DATA_DIR` and `CHECKPOINT_DISABLE`. The token is added only for `plan`/`apply`. Everything else is not passed |
| Redaction | Reports show `GOOGLE_OAUTH_ACCESS_TOKEN=<redacted>` and the child environment's **key names** only. Refusals name variables and never values. `_assert_no_secret` blocks any report that would contain the token |

**Tests** (`tests/test_terraform_custody.py`; 195 pass, 123 of them new):
- 19 credential and override variables × 5 command contexts are refused, with neither the token nor the variable value appearing in any message;
- the token is required and well-formed for `plan`/`apply`, and refused for 6 offline commands;
- `TF_LOG`, an ADC file and the 4 CLI-config paths are refused;
- **13 provider-identity tampering cases** are refused by both `provider_problems` and the preflight: 6 bootstrap identity attributes; a second provider block in another file; an override file; a JSON configuration file; pilot without impersonation; pilot impersonating another variable; pilot with credentials; a module provider block;
- the child environment is an allow-list: unrelated secrets, `TF_WORKSPACE` and foreign `TF_DATA_DIR` are dropped, and there is no token for offline commands;
- redaction holds in the dry-run report, in an exception message and at CLI level;
- **approved flow, offline, for both roots:** a fake `terraform` executable recorded its environment. It received the token, and no other `GOOGLE_*`/`CLOUDSDK_*` variable, the custody `TF_DATA_DIR` and `CHECKPOINT_DISABLE=1`. The following offline `validate` received no token;
- bootstrap instance count, computed statically: **10 + 1 audit = 11** (access off); 13 with access.

**Mutation check.** Each control was disabled in turn and the suite run, then the file was restored (SHA-256 identical):

| Mutation | Tests failing |
| --- | --- |
| No environment refusal | 97 |
| Inherit the whole environment (the pre-fix behaviour) | 3 |
| Token passed to offline commands | 1 |
| No token format check | 2 |
| Bootstrap provider unchecked | 7 |
| `main.tf`-only scan (overrides missed) | 3 |
| Token leaked into the report | 3 |

**Validation after the fix:**
- full `pytest`: **587 passed, 1 skipped**;
- `make gcp-offline-check`: **343 passed, 1 skipped**;
- Ruff passed; the docs validator reports 0 issues; `git diff --check` is clean;
- `terraform fmt` passed, and `validate` is valid for both roots (no init or plan);
- the real-shell preflight for bootstrap (run phase) reports **no problems**: no stray credential variables, CLI configuration or ADC file on this Mac;
- nothing staged or flagged; no state under the repository; no checkpoint egress.

One combined validation command stalled for several minutes, but its results were all passes. Re-timed individually with hard timeouts, every step finished in under 1 s. The cause was not reproduced or identified.

**Residual risks:**
- **Out of reach of the wrapper:**
  - running `terraform` directly bypasses the wrapper; the sentinel backend still fails closed;
  - the operator's gcloud session itself (the account and its stored refresh token) is outside the wrapper;
  - the token-shape check confirms the format, not that the token belongs to the Owner (P4: check the gcloud account);
  - Python cannot wipe the token from process memory.
- **Not checked by the policy:**
  - the shell's `PATH` is passed through, so a malicious `terraform` earlier on `PATH` would run; this is local compromise, outside scope;
  - the static provider check parses HCL text with regular expressions, not a full HCL parser. It is tested against the realistic tampering forms listed, but exotic syntax could evade it. Plan review (the step 4 checker plus a human diff) remains the authoritative gate.
- **Corporate proxies** are refused outright. Allowing one would need a reviewed policy change.

## Approvals required (none granted by M6P)

1. **Accept M6P** (review and commit).
2. ~~P1 code fix~~: **implemented and tested (2026-11-10).** It is included in the M6P commit review (approval 1).
3. **Inputs decisions:** the named `operator_member` (the Owner operator), and `token_audit_logging_enabled = true`, which changes project `auditConfigs` for IAM Credentials only.
4. **Optional read-only GCP pre-checks** above: project IAM baseline, empty SA and role inventories, permission support.
5. **Generate the saved bootstrap plan** (steps 0–5), then report the SHA256, the checker JSON and the summary.
6. **Apply approval** for that exact plan SHA256. Back up the state immediately (M6M), then read-back verification:
   - policy diff = +1 additive member + the audit config, Owner unchanged;
   - roles equal the JSON;
   - SA keys system-managed only.
7. **Access window:** a later separate gate (re-plan with access enabled; ≤ 8 h; P3 re-review).

**Stopped after M6P.** No bootstrap deployment was started.
