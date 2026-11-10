# M6R post-bootstrap GCP security verification

Date 2026-11-10. Baseline `main` at `1327049` (M6Q committed; the operator reports that M6Q CI passed). Project `ebank-pilot-dev-261031-k7m4`, region `europe-west2`.

**Documentation only.** This record collects checks the operator ran independently in their own session. M6R did none of the following:
- run gcloud, a GCP API or Terraform;
- change any access;
- commit or push.

## Verified configuration (operator-confirmed)

| Control | Expected (M6K/M6Q) | Operator result |
| --- | --- | --- |
| Service accounts | Exactly `bankpilot-dev-provisioner` and `bankpilot-dev-runner`, both enabled | Confirmed: exactly two, both enabled |
| Custom roles | Seven `bankpilot_dev_*` roles at stage GA: `impersonate`, `jobs`, `metadata`, `objects`, `provision`, `readers`, `tables` | Confirmed: exactly seven, all GA |
| Project-level bankpilot service-account bindings | Only runner → `bankpilot_dev_jobs` (additive `google_project_iam_member.runner_jobs`) | Confirmed: exactly one binding |
| Provisioner project access | None: `provisioner_access_enabled = false` | Confirmed: no project-level provisioner role binding in the filtered policy output |
| Token-minting audit configuration | `iam.googleapis.com` with `ADMIN_READ` and `DATA_READ`, no exempted members (M6Q) | Confirmed earlier in the live project `auditConfigs` |
| Bootstrap state custody | Encrypted backup after every apply (M6M) | Serial 14 encrypted, uploaded to OneDrive, downloaded, authenticated and `verified=true` |
| Repository | M6Q committed, CI green | Commit `1327049`, CI passed |

**Provisioner access remains disabled.** No project-level provisioner binding exists, and the bootstrap inputs keep `provisioner_access_enabled = false`. Any access window is a separate, time-bound approval gate (M6K).

## What is not yet verified

1. **Audit event generation.** The audit configuration is verified, but no event has been tested. No `GenerateAccessToken` call (or any other IAM read) has yet been made and observed as a Data Access log entry. The configuration shows that logging is switched on, not that entries appear. *Test later:* during the first approved access window, mint one provisioner token and confirm the matching `iam.googleapis.com` Data Access log entry, with the caller identity and target service account.
2. **Service-account-level IAM policies were not assessed.** The binding check covered the project policy only. IAM policies attached to the service accounts themselves, such as `roles/iam.serviceAccountTokenCreator` or the `bankpilot_dev_impersonate` role on the provisioner, are separate resources and do not appear in it. M6K expects them to be empty while access is disabled: `operator_impersonation` has count 0. *Check later (read-only):* run `get-iam-policy` on both service accounts and expect no bindings.
3. **Inherited policy.** Organisation- and folder-level bindings and audit configs fall outside the project-level check.
4. **Role contents.** The roles were confirmed by name and stage. Their permission lists were checked against the repository JSON by the saved-plan checker (M6P/M6Q) and Terraform state, not by a separate live comparison.

## Status

- **Bootstrap:** complete. The live configuration matches the expected 10 durable resources plus the audit config. Custody state is at serial 14, with an encrypted, verified backup. M6R did not inspect the state's resource list.
- **Open items before any access window:**
  - the service-account IAM policy check (item 2);
  - the audit-event test planned for the first window (item 1);
  - the M6P carry-overs: P2 (custom-role permission support) and P4 (token identity).

**Stopped after M6R.**
