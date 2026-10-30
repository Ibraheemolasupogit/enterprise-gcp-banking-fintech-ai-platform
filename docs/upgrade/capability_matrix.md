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
