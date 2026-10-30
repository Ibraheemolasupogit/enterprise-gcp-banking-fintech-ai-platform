# GCP Reference Architecture

M5 scope note: this inherited broad reference is not the deployment inventory. The current [minimal readiness design](gcp_deployment_readiness.md) proposes only Storage, BigQuery and supporting IAM. Its Terraform is offline checked, pending provider validation and deployment approval. Other services below remain historical design concepts.

## Architecture Purpose

This reference architecture describes how the local synthetic financial crime ML platform could map to a production-style Google Cloud Platform design. It is a deployment blueprint only: this repository does not create live GCP infrastructure, service accounts, APIs, BigQuery tables, Pub/Sub topics, Dataflow jobs, Vertex AI resources, or production dashboards.

## Financial Crime Use Case Overview

The platform supports fraud detection, AML risk scoring, anomaly detection, graph and network risk modelling, NLP alert triage, monitoring, and governance evidence. In a production setting, these capabilities would support investigator prioritisation and risk discovery. They would not provide autonomous final decisioning; a human-in-the-loop review process remains required for financial crime decisions.

The current repository uses synthetic data only. No real customer data, transaction data, credentials, secrets, or confidential intelligence are required or expected.

## Local-to-GCP Mapping

| Local component | GCP service alignment | Production responsibility |
| --- | --- | --- |
| `data/sample/*.csv` | Cloud Storage landing bucket | Governed ingestion of source extracts or events |
| Data validation layer | BigQuery data quality checks / Dataplex-style governance | Schema validation, completeness checks, quality exceptions |
| `transaction_features.csv` | BigQuery feature tables / Vertex AI Feature Store concept | Reusable governed feature layer |
| Fraud classifier | Vertex AI Training | Managed training jobs and evaluation tracking |
| Model metrics and model card | Vertex AI Experiments / Model Registry metadata concept | Model lineage, evaluation evidence, approval metadata |
| AML scoring | Dataflow / BigQuery scheduled scoring / Cloud Run batch job | Transparent deterministic score generation |
| Anomaly detection | Vertex AI batch prediction or scheduled Cloud Run job | Unsupervised risk discovery |
| Network risk modelling | Dataproc / BigQuery graph-style feature tables / NetworkX batch job on Cloud Run for demo scale | Relationship and cluster risk analysis |
| NLP alert triage | Vertex AI custom training or Cloud Run batch job | Case-note classification and triage support |
| Monitoring outputs | Vertex AI Model Monitoring concept / Cloud Logging / BigQuery monitoring tables | Drift, prediction, risk, and alert-quality monitoring |
| Governance evidence pack | Cloud Storage evidence bucket / BigQuery audit tables / Dataplex metadata concept | Evidence retention and auditability |
| Markdown reports | Cloud Storage / Looker Studio source layer concept | Governance, operational, and analyst reporting |

## Data Ingestion Layer

Batch ingestion would land synthetic or approved non-production extracts in Cloud Storage. A production version could also accept event-style transaction messages through Pub/Sub. Ingested records would be validated before use, with rejected records routed to a dead-letter location and quality exceptions logged to Cloud Logging and BigQuery audit tables.

## Storage and Analytics Layer

BigQuery would hold curated transaction, account, customer, beneficiary, device, alert, and case-note tables. Tables should be partitioned by event or processing date and clustered by high-cardinality access keys such as account, customer, transaction, alert, and beneficiary identifiers.

## Feature Engineering Layer

Feature engineering would run as scheduled BigQuery SQL, Dataflow transforms, or a containerised Cloud Run batch job depending on scale and latency requirements. The local `data/processed/transaction_features.csv` maps conceptually to governed BigQuery feature tables or Vertex AI Feature Store concepts.

## Model Training Layer

The local fraud classifier maps to Vertex AI Training. Training jobs would read governed features from BigQuery, write evaluation metrics, and produce model artifacts. Experiments, feature versions, and model configurations would be captured for reproducibility.

## Model Registry and Governance Layer

Model metrics, model cards, monitoring reports, and governance evidence would map to Vertex AI Experiments, Model Registry metadata concepts, Cloud Storage evidence buckets, and BigQuery audit tables. Promotion gates would require validation evidence, documented limitations, and human approval.

## Batch Scoring Layer

Fraud prediction, AML risk scoring, anomaly detection, network risk scoring, and NLP alert triage would run as scheduled batch workflows. Dataflow is appropriate for large-scale transformations, BigQuery scheduled queries for transparent scoring logic, Vertex AI batch prediction for registered model scoring, and Cloud Run jobs for lightweight Python workloads.

## Alert Prioritisation Layer

Prioritised alerts would be written to BigQuery tables with reason codes, score bands, recommended review queues, and evidence links. A controlled investigator application or Looker Studio source layer could read these tables, but this repository does not implement a dashboard.

## Monitoring Layer

The local monitoring outputs map conceptually to Vertex AI Model Monitoring, Cloud Logging, BigQuery monitoring tables, Cloud Monitoring alerts, and Looker Studio reporting. Monitoring should cover data drift, prediction drift, risk band distributions, alert volumes, operational failures, and evidence completeness.

## Audit and Evidence Layer

Governance evidence should be retained in Cloud Storage and indexed in BigQuery. Audit events should capture pipeline runs, data validation results, model evaluation, scoring outputs, monitoring reviews, approval gates, and control evidence. Dataplex-style metadata and lineage concepts can support discovery and governance.

## Human Review Workflow

High-risk alerts should be routed to investigator queues. Financial crime outcomes should require human review, documented rationale, and escalation procedures. The architecture explicitly excludes autonomous final decisioning.

## Limitations and Non-Production Caveat

This is a local-first reference design using synthetic data. It is not production deployment, does not create cloud resources, does not enforce IAM, does not manage secrets, and does not replace financial crime governance, validation, or compliance review.
