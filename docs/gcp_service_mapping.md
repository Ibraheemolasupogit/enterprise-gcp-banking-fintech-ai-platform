# GCP Service Mapping

This table maps the local repository artifacts to a target GCP service design. It is a reference architecture artifact, not a statement of what is deployed.

**Deployment status (M6T, operator-reported):** only a dev pilot's **infrastructure** exists: one Cloud Storage bucket and one BigQuery dataset with seven **empty** tables, plus their scoped IAM ([M6T](upgrade/m6t_validation.md)). No workload in this table has been executed in GCP: no ingestion or loading, no BigQuery analytics, no Dataflow, Vertex AI, Cloud Run, Pub/Sub, Looker Studio or monitoring service, and no production operation.

| Local artifact | Local module/script | Target GCP service | Production responsibility | Governance/audit consideration |
| --- | --- | --- | --- | --- |
| `data/sample/*.csv` | `scripts/generate_demo_data.py` | Cloud Storage landing bucket | Store inbound synthetic or approved source files | Retention, data classification, synthetic data caveat |
| `outputs/sample/data_quality_report.json` | `financial_crime_ml.ingestion` | BigQuery checks / Dataplex-style governance | Validate schema, completeness, and relationships | Data quality evidence and exception logging |
| `data/processed/transaction_features.csv` | `scripts/build_features.py` | BigQuery feature tables / Vertex AI Feature Store concept | Maintain governed reusable features | Feature lineage, config versioning, validation |
| `outputs/sample/model_metrics.json` | `scripts/train_fraud_model.py` | Vertex AI Experiments | Track model evaluation metrics | Model approval evidence and reproducibility |
| `reports/sample/model_card.md` | `financial_crime_ml.governance.model_card_generator` | Vertex AI Model Registry metadata concept / Cloud Storage | Document intended use and limitations | Model risk management evidence |
| `outputs/sample/fraud_predictions.csv` | `financial_crime_ml.models.fraud_classifier` | Vertex AI batch prediction / BigQuery scoring table | Produce supervised fraud risk predictions | Score lineage and threshold governance |
| `outputs/sample/aml_risk_scores.csv` | `financial_crime_ml.scoring.aml_risk_model` | Dataflow / BigQuery scheduled scoring / Cloud Run batch job | Apply transparent AML rule scoring | Reason codes and human review policy |
| `outputs/sample/anomaly_scores.csv` | `financial_crime_ml.models.anomaly_detector` | Vertex AI batch prediction / Cloud Run scheduled job | Identify unusual transaction behaviour | Excluded target fields and reason-code evidence |
| `outputs/sample/network_risk_scores.csv` | `financial_crime_ml.features.graph_features` | Dataproc / BigQuery graph features / Cloud Run batch job | Calculate relationship and cluster risk | Explainable graph features and cluster evidence |
| `outputs/sample/nlp_alert_triage.csv` | `financial_crime_ml.models.nlp_alert_classifier` | Vertex AI custom training / Cloud Run batch job | Classify synthetic case-note typologies | Human review and lightweight NLP limitations |
| `outputs/sample/prioritised_alerts.csv` | `financial_crime_ml.scoring.alert_prioritiser` | BigQuery alert queue table | Combine risk signals for triage | No autonomous final decisioning |
| `outputs/sample/monitoring_summary.json` | `scripts/run_monitoring.py` | Vertex AI Model Monitoring concept / Cloud Logging / BigQuery | Monitor drift and risk distributions | Monitoring evidence and operational review |
| `outputs/sample/governance_control_checklist.json` | `scripts/generate_governance_pack.py` | Cloud Storage evidence bucket / BigQuery audit table | Track governance controls | Control status evidence |
| `outputs/sample/audit_log.jsonl` | `financial_crime_ml.governance.audit_log` | Cloud Logging / BigQuery audit tables | Record lifecycle events | Auditability and replayability |
| `reports/sample/*.md` | Workflow scripts | Cloud Storage / Looker Studio source layer concept | Share governance and operational reports | Evidence retention and reviewer access |
| `configs/*.yaml` | All workflows | Cloud Storage config bucket / Artifact metadata | Parameterise reproducible runs | Config versioning and change control |

## Caveat

The mapping is conceptual and uses synthetic data only. It is not production deployment and does not create GCP resources, credentials, APIs, Terraform, or live pipelines.
