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
| M5 infrastructure | Modular Terraform, JSON/HCL parsing and resource/IAM/schema policy checks; environment-isolated names; reviewable provisioning/cleanup candidates | M6K: 18 pilot instances plus a separate IAM bootstrap root (11–13 instances); both roots provider-validated offline; no plan; no resources deployed |
| M5 migration verification | Offline comparison of seven complete CSV exports to verified M3/M4 runs, exact typed fields/keys/grains/currency totals | Does not authenticate export provenance or establish BigQuery SQL execution; no cloud export/acquisition tool |
| M5 operations/cost | Explicit prerequisite/plan/apply/workload/cleanup approval gates; bounded control proposals and cost arithmetic | No cloud executor, budget, API enablement, paid service call or hard spending cap implemented |

See [M5 validation](m5_validation.md) for current executed checks and remaining deployment blockers.
