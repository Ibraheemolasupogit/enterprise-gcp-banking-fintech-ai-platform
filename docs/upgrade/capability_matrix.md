# Capability evidence boundaries

Implemented means source exists. A committed sample is not proof of execution.
M0 did not execute tests; M1 executed results belong in [validation.md](validation.md).

| Capability | Inherited evidence | Upgrade M1 boundary |
| --- | --- | --- |
| Data generation | Seeded synthetic CSV generator and samples | Preserved; temporal defects documented |
| Validation | Schemas, keys, relations and quality reports | Preserved; no new data engineering system |
| Fraud | Random-split classifier with leaked derived features | Separate row-local, chronological train/validation/test evaluation |
| AML | Deterministic scores using synthetic target context | Legacy demonstration; no independent detection validation |
| Anomalies | Isolation Forest; derived-label features remain | Legacy demonstration; not retrained by M1 |
| Graph | NetworkX and label-aware network rules | Legacy demonstration; no entity-resolution service |
| NLP | Keyword matching and original-label fallback | Legacy demonstration; no LLM/agent |
| Monitoring | Local reports over saved artifacts | No live operational telemetry |
| Governance | Static events, existence-based controls, report templates | Tests write to temporary directories; records remain simulations |
| Run evidence | No inherited source/input/config execution manifest | Actual M1 timestamps, hashes, revisions, versions, splits and metrics |
| GCP | Architecture, security and deployment documents | Documentation only; no cloud commands or resources |
| Enterprise expansion | KYC fields and financial-crime context | Credit risk, Document AI, RAG, APIs, dashboards and other domains deferred |

M1 uses existing pandas, NumPy, PyYAML and scikit-learn dependencies. It does not
certify model validity, enforce investigator approval, or add production controls.

## M2–M5 current evidence

| Capability | Implemented and locally tested | Deployment readiness / actual GCP status |
| --- | --- | --- |
| M2 synthetic contracts | Exact currency minor units, constrained UTC events, deterministic generation and validation | Synthetic inputs only; no cloud generator |
| M3 financial foundation | Traceable raw/validated/curated snapshots, quarantine and reconciliation | Explicit target schemas for curated transactions/customers/accounts; no cloud ingestion connector |
| M4 analytics | Four SQLite daily marts, independent grain/metric checks, currency totals and lineage | M4 GoogleSQL CTAS mapping preserved as an untested proposal |
| M5 infrastructure | Modular Terraform, JSON/HCL parsing and resource/IAM/schema policy checks; environment-isolated names; reviewable provisioning/cleanup candidates | **Deployed (dev pilot, operator-applied):** IAM bootstrap root applied (M6Q/M6R); all 18 pilot instances deployed after the M6T recovery (2026-11-10). Tables are empty. See the M6 table below |
| M5 migration verification | Offline comparison of seven complete CSV exports to verified M3/M4 runs, exact typed fields/keys/grains/currency totals | Does not authenticate export provenance or establish BigQuery SQL execution; no cloud export/acquisition tool |
| M5 operations/cost | Explicit prerequisite/plan/apply/workload/cleanup approval gates; bounded control proposals and cost arithmetic | No cloud executor, budget, API enablement, paid service call or hard spending cap implemented |

See [M5 validation](m5_validation.md) for the M5 executed checks. The deployment status above is superseded by M6.

## M6 GCP pilot status (closed at M6T, 2026-11-12)

All cloud and Terraform steps were run by the operator under separate approvals and are **operator-reported**; the assistant worked offline.

| Capability | Status | Boundary |
| --- | --- | --- |
| IAM bootstrap | Applied; runtime account, custom roles and token-minting audit config verified ([M6R](m6r_validation.md)) | Provisioner role still defined; its grants are revoked |
| Pilot infrastructure | **18 of 18 deployed**: bucket and policy, dataset and policy, 7 tables, 7 runner table grants ([M6T](m6t_validation.md)) | Single-region dev project, no Organization; deletion protection on |
| Dataset access | Read back as named user OWNER plus the runner's custom metadata role only | Personal-account owner |
| Temporary access | 2 grants revoked; impersonation denied afterwards; 8 `GenerateAccessToken` audit entries observed | Numeric service-account identity in the audit entries not independently mapped |
| State custody | Encrypted backups verified after a OneDrive round-trip: pilot serial 21, bootstrap serial 26 | Raw state and plans kept in custody, not in the repository |
| Data and workloads | **None.** No upload, load, query or mart build | No cloud data ingestion is claimed |
| Production readiness | **Not claimed** | No VPC-SC, CMEK, org policies, group ownership, alerting or hard spending cap |

**Next: M7**, offline preparation for a controlled synthetic GCS-to-BigQuery ingestion (Gate C of the [pilot runbook](../gcp_pilot_plan.md)). It executes no cloud work.
