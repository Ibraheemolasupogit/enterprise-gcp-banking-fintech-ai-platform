# ADR-002: Minimal manually controlled GCP data pilot

Status: accepted for local design and IaC preparation; deployment approval pending.

Context: M3 creates validated, traceable snapshots and M4 executes reconciled SQLite marts. Neither has a cloud connector. Inherited reference diagrams describe a much wider platform, which is not the authorized deployment scope.

Decision: retain local M3 validation and orchestration. Propose one co-located versioned Storage bucket, one dataset, three input tables and four marts. Separate existing provisioning and new runtime identities, scoped custom permissions, explicit schemas, approved short-lived impersonation, no stored credentials. Terraform manages resource/schema/IAM configuration; data loads and transactional DML are future controlled operations. Preserve all prior code, SQL interfaces, Git history and sample evidence.

Consequences: reviewable, small resource inventory and bounded synthetic pilot without additional runtime services. Authoritative bucket/dataset IAM requires new dedicated resources. Some provisioning permissions are privileged; bootstrap, plan, apply, workload and cleanup have separate approval gates. Deletion guards protect evidence but increase retention/cost responsibilities. Budget alerts and proposed executor limits do not enforce a hard cap. Provider validation, account-specific IAM/API/region/quota checks, GoogleSQL validation, billing review and actual deployment evidence remain outstanding. IaC is authored and offline checked, not provider-validated or cloud-deployed.

Details: [readiness design](../gcp_deployment_readiness.md), [operational gates](../gcp_pilot_plan.md), [M5 validation](../upgrade/m5_validation.md).
