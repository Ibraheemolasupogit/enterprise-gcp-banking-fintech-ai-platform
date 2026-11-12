# Architecture

M1–M4 are implemented local workflows: leakage-safe fraud evaluation, versioned synthetic generation, raw → validated → curated ingestion and reconciled SQLite analytics.

M5 adds a [minimal GCP readiness design](gcp_deployment_readiness.md), [explicit deployment gates](gcp_pilot_plan.md) and [ADR-002](architecture_decisions/ADR-002-minimal-gcp-pilot.md). Environment-isolated Terraform is authored and offline checked. Provider validation, saved plans and the operator-run deployment followed in M6 (below). Provisioning/cleanup permission candidates are explicit review files. An implemented offline comparator verifies seven CSV exports against M3/M4 references without invoking GCP; export provenance and cloud SQL parity remain future verification work.

The [inherited reference architecture](gcp_reference_architecture.md) describes broader legacy aspirations. Only Storage, BigQuery and supporting identity configuration are in the M5 pilot design.

**Deployment status (M6T, 2026-11-12; operator-reported).** The dev pilot **infrastructure** is deployed in `europe-west2`: the IAM bootstrap and all 18 pilot resources (one bucket, one dataset, seven **empty** tables and their resource-level IAM). Temporary provisioner access is revoked. Nothing beyond that infrastructure has run in GCP: **no data ingestion, load or query job, cloud analytics or mart build, and no production operation** such as scheduling, monitoring or incident response. The pilot is not production-ready. See [M6T](upgrade/m6t_validation.md) and the [pilot runbook](gcp_pilot_plan.md); M7 prepares the synthetic ingestion offline.
