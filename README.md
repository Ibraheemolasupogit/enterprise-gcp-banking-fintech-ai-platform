# GCP Financial Crime ML Intelligence Platform

A local-first, GCP-aligned machine learning intelligence platform for synthetic financial crime analytics, covering fraud detection, AML risk scoring, anomaly detection, graph risk, NLP alert triage, monitoring, governance evidence, and cloud architecture design.

## Upgrade M2: Synthetic Data Integrity

The separate v2 generator enforces UTC entity/event chronology, ownership and
foreign keys, account currency and exact integer minor-unit amounts. It emits
observable behavioural scenarios and separate synthetic outcomes, plus an M1
evaluation export. Historical samples and legacy commands remain unchanged.

```bash
python3 -m financial_crime_ml.cli generate-data-v2 --run-id m2-data-1
python3 -m financial_crime_ml.cli validate-data-v2 --dataset-dir outputs/runs/m2-data-1
python3 -m financial_crime_ml.cli evaluate-fraud \
  --input outputs/runs/m2-data-1/evaluation_transactions.csv --run-id m2-fraud-1
```

Outputs go to fresh ignored `outputs/runs/` folders; run IDs cannot be reused.
See the [v2 data contract](docs/synthetic_v2_contract.md) for configuration, money,
timestamp rules, outcome separation and limitations, and
[executed M2 validation](docs/upgrade/m2_validation.md) for actual results.

## Upgrade M1: Traceable Fraud Evaluation

The original 12 milestones and committed sample artifacts are preserved. The
inherited `outputs/sample/model_metrics.json` and model card are **legacy
demonstrations affected by target-derived features and future aggregates**.
Static governance audit records simulate completed events; checklist status means
an evidence path exists, not that a control was verified.

Use the separate evaluation path for new local fraud-baseline evidence:

```bash
python3 -m financial_crime_ml.cli evaluate-fraud --run-id m1-review
```

It reads raw transaction observations with a fixed feature allowlist, trains on
the earliest window, selects a threshold on a later validation window, and
evaluates the final test window once. Actual run evidence is saved under ignored
`outputs/runs/m1-review/`. Run IDs cannot be reused. Committed samples are not
rewritten. This remains synthetic local evaluation, not production fraud evidence.

See [evaluation methodology](docs/modelling.md),
[inherited baseline and donor provenance](docs/upgrade/baseline.md),
[capability boundaries](docs/upgrade/capability_matrix.md),
[architecture decision](docs/architecture_decisions/ADR-001-upgrade-in-place.md), and
[executed validation](docs/upgrade/validation.md).

## Problem Statement

Financial institutions need to detect suspicious behaviour across customers, accounts, transactions, beneficiaries, devices, alerts, and case narratives. Financial crime teams must prioritise noisy alert queues, identify emerging typologies, explain risk decisions, monitor model behaviour, and retain evidence for governance and audit review.

This repository demonstrates those engineering patterns using synthetic data only. It is designed for public portfolio review and does not contain real customer data, credentials, secrets, production infrastructure, or live financial crime intelligence.

## Why This Project Matters

Financial crime ML systems need more than a classifier. They need governed data ingestion, deterministic feature engineering, transparent scoring, unsupervised risk discovery, network analysis, text triage, monitoring, auditability, and human review controls. This project shows how those components fit together in a practical local implementation while mapping clearly to a future GCP production architecture.

## Key Capabilities

- Deterministic synthetic financial crime data generation
- Data ingestion, schema validation, relationship checks, and data quality reporting
- Transaction, velocity, customer, account, beneficiary, device, and typology features
- Supervised fraud classification baseline using scikit-learn
- Transparent AML risk scoring and prioritised alert generation
- Isolation Forest anomaly detection with reason codes
- NetworkX graph and network risk modelling
- Lightweight NLP alert triage and synthetic case-note classification
- Drift, prediction, risk distribution, and alert quality monitoring
- Governance evidence pack, model risk assessment, audit log, and lifecycle traceability
- GCP reference architecture, deployment blueprint, operations runbook, and service mapping

## Architecture Overview

The local workflow is organised as a synthetic ML lifecycle:

```text
synthetic data -> validation -> features -> models and scoring -> risk outputs -> monitoring -> governance evidence -> GCP blueprint
```

The platform is intentionally batch-oriented and local-first. Each stage writes explicit artifacts under `data/processed/`, `outputs/sample/`, or `reports/sample/`, making the workflow easy to inspect and reproduce.

Key architecture documents:

- `docs/architecture_narrative.md`
- `docs/gcp_reference_architecture.md`
- `docs/distributed_systems_design.md`
- `docs/deployment_blueprint.md`
- `docs/security_and_governance_mapping.md`
- `docs/operations_runbook.md`
- `docs/gcp_service_mapping.md`

## GCP Service Mapping Summary

| Local capability | GCP-aligned production concept |
| --- | --- |
| Synthetic CSV landing data | Cloud Storage landing bucket |
| Data validation and quality reports | BigQuery checks / Dataplex-style governance |
| Transaction feature table | BigQuery feature tables / Vertex AI Feature Store concept |
| Fraud classifier training | Vertex AI Training |
| Model metrics and model card | Vertex AI Experiments / Model Registry metadata concept |
| AML scoring and alert prioritisation | Dataflow, BigQuery scheduled scoring, or Cloud Run batch jobs |
| Anomaly and NLP batch scoring | Vertex AI batch prediction or Cloud Run jobs |
| Network risk modelling | Dataproc, BigQuery graph-style features, or Cloud Run at demo scale |
| Monitoring and drift outputs | Vertex AI Model Monitoring concept, Cloud Logging, BigQuery monitoring tables |
| Governance evidence pack | Cloud Storage evidence bucket, BigQuery audit tables, Dataplex-style metadata |

This mapping is a target design. Only a small dev pilot (one Storage bucket and one BigQuery dataset with seven **empty** tables) has been deployed, by the operator; see [M6 GCP pilot status](#m6-gcp-pilot-status). None of the other services listed is deployed.

## Local Quickstart

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
python -m ruff check .
python -m pytest
```

If your environment uses `python3` rather than `python`, run the equivalent `python3` commands.

## End-to-End Demo

Run the full local workflow:

This legacy demo overwrites sample paths. During upgrade validation, run it only
in a disposable copy. Use `evaluate-fraud` above in the working repository.

```bash
bash scripts/run_all_local.sh
```

Or run each stage manually:

```bash
python3 scripts/generate_demo_data.py
python3 scripts/validate_demo_data.py
python3 scripts/build_features.py
python3 scripts/train_fraud_model.py
python3 scripts/run_anomaly_detection.py
python3 scripts/run_network_risk.py
python3 scripts/run_nlp_triage.py
python3 scripts/run_monitoring.py
python3 scripts/generate_governance_pack.py
python3 scripts/validate_docs.py
python3 scripts/final_project_check.py
```

Equivalent CLI commands are available:

```bash
python3 -m financial_crime_ml.cli generate-data
python3 -m financial_crime_ml.cli validate-data
python3 -m financial_crime_ml.cli build-features
python3 -m financial_crime_ml.cli train-fraud-model
python3 -m financial_crime_ml.cli run-anomaly-detection
python3 -m financial_crime_ml.cli run-network-risk
python3 -m financial_crime_ml.cli run-nlp-triage
python3 -m financial_crime_ml.cli run-monitoring
python3 -m financial_crime_ml.cli generate-governance-pack
python3 -m financial_crime_ml.cli validate-docs
python3 -m financial_crime_ml.cli final-check
```

Detailed walkthrough: `docs/end_to_end_walkthrough.md`.

## Sample Outputs

Important generated artifacts include:

- `data/sample/*.csv`: synthetic customers, accounts, transactions, beneficiaries, devices, alerts, and case notes
- `outputs/sample/data_quality_report.json`: schema, quality, and relationship validation report
- `data/processed/transaction_features.csv`: transaction-level engineered feature table
- `outputs/sample/model_metrics.json`: fraud classifier metrics
- `outputs/sample/fraud_predictions.csv`: fraud probabilities and predictions
- `outputs/sample/aml_risk_scores.csv`: deterministic AML scores and reason codes
- `outputs/sample/prioritised_alerts.csv`: combined fraud and AML alert priority output
- `outputs/sample/anomaly_scores.csv`: anomaly scores, ranks, bands, and reason codes
- `outputs/sample/network_risk_scores.csv`: graph/network risk scores and cluster indicators
- `outputs/sample/nlp_alert_triage.csv`: alert-level NLP triage output
- `outputs/sample/monitoring_summary.json`: monitoring status and key observations
- `outputs/sample/governance_control_checklist.json`: governance control evidence
- `reports/sample/model_card.md`: model card for the fraud classifier
- `reports/sample/model_monitoring_report.md`: monitoring report
- `reports/sample/governance_evidence_pack.md`: consolidated governance evidence pack
- `reports/sample/model_risk_management_report.md`: model risk management summary

Detailed output guide: `docs/sample_outputs_guide.md`.

## Model Governance and Controls

The repository includes model governance artifacts designed to reflect financial services model risk expectations:

- Explicit synthetic data and non-production caveats
- Data validation and relationship checks
- Transparent feature and rule documentation
- Model card and model metrics
- AML, anomaly, network, and NLP reason codes
- Drift and alert quality monitoring summaries
- Governance control checklist
- Model risk assessment
- Evidence inventory
- Deterministic local audit log
- Lifecycle traceability summary
- Human-in-the-loop review requirement
- No autonomous final decisioning

## Repository Structure

```text
configs/                  Configuration files for generation, features, scoring, monitoring, governance
data/sample/              Synthetic demo datasets
data/processed/           Engineered feature table
diagrams/                 Mermaid architecture and lifecycle diagrams
docs/                     Architecture, modelling, monitoring, governance, and walkthrough documentation
outputs/sample/           Generated JSON and CSV outputs
reports/sample/           Generated markdown reports and model cards
scripts/                  Local workflow scripts
src/financial_crime_ml/   Python package source
tests/                    Pytest test suite
```

## Milestone Summary

| Milestone | Scope |
| --- | --- |
| 1 | Professional Python project scaffold |
| 2 | Synthetic financial crime data generation |
| 3 | Data ingestion and validation |
| 4 | Feature engineering and typology indicators |
| 5 | Fraud classifier, AML risk scoring, and prioritised alerts |
| 6 | Anomaly detection |
| 7 | Graph and network risk modelling |
| 8 | NLP alert triage and case-note classification |
| 9 | Monitoring and drift reporting |
| 10 | Governance evidence pack and model risk controls |
| 11 | GCP reference architecture and deployment blueprint |
| 12 | Final portfolio polish and end-to-end walkthrough |

## Portfolio Positioning

This project demonstrates financial crime ML engineering, scalable pipeline design, transparent scoring, monitoring, model governance, and GCP-aligned production thinking. It is intended to show practical Python engineering across the full synthetic ML lifecycle rather than a narrow notebook-only model exercise.

## Synthetic Data Disclaimer

All datasets and outputs are synthetic demonstration artifacts. They must not be interpreted as real customer behaviour, real alerts, real typologies, real financial crime intelligence, or production model evidence.

## Non-Production Disclaimer

This repository is not production deployment. Its local workflows do not create cloud infrastructure, service accounts, credentials, APIs, BigQuery tables, Pub/Sub topics, Dataflow jobs, Vertex AI pipelines, dashboards, model registry integrations, or live monitoring services. The only cloud resources are the small M6 dev pilot that the operator applied manually through approval gates: the IAM bootstrap, one Storage bucket, one BigQuery dataset and seven **empty** tables. No data has been loaded into it and no workload runs there (see [GCP pilot status](#m6-gcp-pilot-status)).

## Limitations

- Synthetic data cannot represent the full complexity of real financial crime behaviour.
- Model metrics are demonstration metrics and not evidence of production suitability.
- Rule-based AML, network, and NLP logic is intentionally transparent and lightweight.
- Monitoring is local and file-based, not a live operational service.
- GCP alignment is mostly architecture and deployment planning. The deployed dev pilot holds empty tables only; there is no cloud data ingestion, cloud workload or production hardening.
- Human investigation, validation, governance approval, and compliance review would be required before any production use.

## Future Enhancements

- Infrastructure-as-code examples in a separate, non-credentialed deployment milestone
- Containerised batch workflow packaging
- BigQuery SQL feature engineering examples
- Vertex AI training pipeline prototype
- Looker Studio-ready reporting layer
- Investigator feedback loop design
- Model registry and approval workflow integration
- Expanded fairness, explainability, and typology evaluation documentation

### M3 local financial data foundation

The versioned local raw → validated → curated workflow supports explainable quarantine, currency-specific reconciliation and immutable reruns. See the [contract and CLI](docs/financial_data_foundation.md) and [executed validation evidence](docs/upgrade/m3_validation.md). This is a local implementation; no GCP deployment is claimed.

### M4 local financial analytics

Four versioned SQLite daily marts cover observed transaction, customer, account and operational activity with currency-specific reconciliation and source lineage. See [analytics contracts and SQL migration](docs/financial_analytics.md) and [executed evidence](docs/upgrade/m4_validation.md). BigQuery schemas and SQL are proposed and have not been cloud-tested.

### M5 GCP deployment readiness

Reviewable modular Terraform proposes one regional versioned Storage bucket, one BigQuery dataset/seven tables and scoped runtime IAM. [Design and limitations](docs/gcp_deployment_readiness.md), [approval gates](docs/gcp_pilot_plan.md), and [validation evidence](docs/upgrade/m5_validation.md) distinguish offline checks from outstanding provider/cloud verification. M5 itself deployed nothing; the pilot was deployed later, in M6 (below).

M5 also provides separate provisioning/cleanup permission candidates and environment examples, plus an offline seven-table export comparator. Run `make gcp-offline-check` with existing tooling; it performs no downloads or cloud operations. Export comparison instructions and remaining migration gaps are in the readiness design. M1–M4 interfaces and the 43 historical sample artifacts remain preserved.

### M6 GCP pilot status

M6 took the M5 design through gated, operator-run deployment in the dedicated dev project (`europe-west2`). Every cloud and Terraform step was run by the operator under separate approval; the assistant worked offline. As of **M6T, closed on 2026-11-12** ([evidence](docs/upgrade/m6t_validation.md)):

- the IAM bootstrap root is applied ([M6Q](docs/upgrade/m6q_validation.md), [M6R](docs/upgrade/m6r_validation.md));
- **all 18 pilot resources are deployed**: the bucket, the dataset and its two-entry access policy (named owner and runner metadata role), seven **empty** tables and their runner grants;
- the temporary provisioner access was revoked and impersonation was confirmed denied;
- encrypted pilot and bootstrap state backups were verified after a OneDrive round-trip.

**This is not production readiness and not cloud data ingestion.** No object has been uploaded, no load or query job has run, and the [outstanding limitations](docs/upgrade/m6t_validation.md#outstanding-limitations) still apply.

**Next: M7**, offline preparation for a controlled synthetic GCS-to-BigQuery ingestion. Any actual upload or load needs its own approved access window.
