# Architecture

M1–M4 are implemented local workflows: leakage-safe fraud evaluation, versioned synthetic generation, raw → validated → curated ingestion and reconciled SQLite analytics.

M5 adds a [minimal GCP readiness design](gcp_deployment_readiness.md), [explicit deployment gates](gcp_pilot_plan.md) and [ADR-002](architecture_decisions/ADR-002-minimal-gcp-pilot.md). Environment-isolated Terraform is authored and offline checked; provider-backed validation, a cloud plan and deployment remain pending. Provisioning/cleanup permission candidates are explicit review files. An implemented offline comparator verifies seven CSV exports against M3/M4 references without invoking GCP; export provenance and cloud SQL parity remain future verification work.

The [inherited reference architecture](gcp_reference_architecture.md) describes broader legacy aspirations. Only Storage, BigQuery and supporting identity configuration are in the proposed M5 pilot. No GCP deployment is evidenced.
