# Sample Outputs Guide

This guide explains the most important generated artifacts and where they fit in the synthetic ML lifecycle.

These are preserved legacy demonstrations. Inherited fraud metrics include
label-derived and future-aggregate leakage; governance logs are simulated and
checklist status is based on path existence. Upgrade M1 evaluation evidence lives
separately under ignored `outputs/runs/`; see [modelling](modelling.md).

| Output | What it represents | Why it matters | Lifecycle stage |
| --- | --- | --- | --- |
| `data/sample/*.csv` | Synthetic customers, accounts, transactions, beneficiaries, devices, alerts, and case notes | Provides safe local input data without real customer information | Data generation |
| `outputs/sample/data_quality_report.json` | Schema, quality, and relationship validation report | Shows whether data is fit to enter feature engineering | Data validation |
| `data/processed/transaction_features.csv` | Transaction-level engineered feature table | Main feature input for fraud, AML, anomaly, network, and monitoring workflows | Feature engineering |
| `outputs/sample/model_metrics.json` | Fraud classifier evaluation metrics | Documents model performance and limitations for review | Supervised modelling |
| `outputs/sample/fraud_predictions.csv` | Fraud probabilities and binary predictions | Provides supervised risk signal for alert prioritisation | Supervised scoring |
| `outputs/sample/aml_risk_scores.csv` | AML risk scores, bands, and reason codes | Demonstrates transparent deterministic financial crime scoring | AML risk scoring |
| `outputs/sample/prioritised_alerts.csv` | Combined fraud and AML alert priority output | Shows how model and rule signals can support investigator queueing | Alert prioritisation |
| `outputs/sample/anomaly_scores.csv` | Anomaly scores, ranks, bands, and reason codes | Supports unsupervised risk discovery for unusual behaviour | Anomaly detection |
| `outputs/sample/network_risk_scores.csv` | Graph and network risk scores | Captures shared-device, shared-beneficiary, and suspicious cluster risk | Network risk modelling |
| `outputs/sample/nlp_alert_triage.csv` | Alert-level NLP triage scores and suggested queues | Demonstrates lightweight text analytics for alert and case-note review | NLP triage |
| `outputs/sample/monitoring_summary.json` | Overall monitoring status and observations | Summarises drift, risk distribution, and alert quality monitoring | Monitoring |
| `outputs/sample/governance_control_checklist.json` | Governance controls and evidence status | Demonstrates model risk and audit control discipline | Governance |
| `reports/sample/model_card.md` | Model card for the fraud classifier | Documents purpose, intended use, metrics, assumptions, and limitations | Model governance |
| `reports/sample/model_monitoring_report.md` | Markdown monitoring report | Explains monitoring methodology, findings, and operational recommendations | Monitoring governance |
| `reports/sample/governance_evidence_pack.md` | Consolidated governance evidence pack | Pulls together controls, evidence inventory, lifecycle traceability, and caveats | Governance evidence |
| `reports/sample/model_risk_management_report.md` | Model risk management summary | Summarises component risk, mitigating controls, open issues, and governance posture | Model risk management |

## How to Use This Guide

Start with `outputs/sample/data_quality_report.json`, then inspect the feature table, model metrics, risk outputs, monitoring summary, and governance reports in order. This follows the same lifecycle described in `docs/end_to_end_walkthrough.md`.

## Caveat

All outputs are synthetic demonstration artifacts. They are not production model evidence, not real customer records, and not real financial crime intelligence.
