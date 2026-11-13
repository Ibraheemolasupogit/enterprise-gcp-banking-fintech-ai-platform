# Documentation Index

Use this index to navigate the project documentation.

## Upgrade M2

- [Versioned synthetic data and temporal contracts](synthetic_v2_contract.md)
- [Executed M2 validation](upgrade/m2_validation.md)

## Upgrade M1

- [Inherited baseline, hashes and donor provenance](upgrade/baseline.md)
- [Capability evidence boundaries](upgrade/capability_matrix.md)
- [Upgrade-in-place decision](architecture_decisions/ADR-001-upgrade-in-place.md)
- [Executed validation](upgrade/validation.md)
- [Isolated fraud evaluation](modelling.md)

## Core Architecture

- [Architecture overview](architecture.md)
- [Architecture narrative](architecture_narrative.md)
- [GCP reference architecture](gcp_reference_architecture.md)
- [Distributed systems design](distributed_systems_design.md)
- [Deployment blueprint](deployment_blueprint.md)
- [GCP service mapping](gcp_service_mapping.md)
- [Security and governance mapping](security_and_governance_mapping.md)
- [Operations runbook](operations_runbook.md)

## Data and Features

- [Data dictionary](data_dictionary.md)
- [Data validation](data_validation.md)
- [Feature engineering](feature_engineering.md)
- [Financial crime typologies](financial_crime_typologies.md)

## Modelling and Scoring

- [Modelling](modelling.md)
- [AML risk scoring](aml_risk_scoring.md)
- [Anomaly detection](anomaly_detection.md)
- [Network risk modelling](network_risk_modelling.md)
- [NLP alert triage](nlp_alert_triage.md)

## Monitoring and Governance

- [Monitoring strategy](monitoring_strategy.md)
- [Model risk management](model_risk_management.md)
- [Governance evidence pack](governance_evidence_pack.md)
- [Agentic AI controls placeholder](agentic_ai_controls.md)
- [Limitations](limitations.md)

## Portfolio and Walkthrough

- [End-to-end walkthrough](end_to_end_walkthrough.md)
- [Sample outputs guide](sample_outputs_guide.md)
- [Repository quality checklist](repository_quality_checklist.md)
- [Portfolio positioning](portfolio_positioning.md)

- [M3 financial data foundation](financial_data_foundation.md)
- [M3 validation evidence](upgrade/m3_validation.md)

- [M4 financial analytics contracts](financial_analytics.md)
- [M4 validation evidence](upgrade/m4_validation.md)

- [M5 GCP readiness](gcp_deployment_readiness.md)
- [M6K provisioner role](../infrastructure/iam/provisioner.role.json), [runtime roles](../infrastructure/iam/runtime_roles.json), [impersonation role](../infrastructure/iam/impersonate.role.json) and [separate cleanup add-on](../infrastructure/iam/cleanup.role.json)
- [M5 operational approval gates](gcp_pilot_plan.md)
- [ADR-002 minimal GCP pilot](architecture_decisions/ADR-002-minimal-gcp-pilot.md)
- [ADR-003 separated IAM bootstrap](architecture_decisions/ADR-003-separated-iam-bootstrap.md)
- [M6J security review](upgrade/m6j_review.md) and [M6K remediation evidence](upgrade/m6k_validation.md)
- [M6L Terraform state custody design](terraform_state_custody.md) and [M6L evidence](upgrade/m6l_validation.md)
- [M6M encrypted OneDrive state backup runbook](terraform_state_backup_onedrive.md) and [M6M evidence](upgrade/m6m_validation.md); [M6N synthetic OneDrive drill](upgrade/m6n_validation.md)
- [M6O secure local Terraform backends](upgrade/m6o_validation.md)
- [M6P IAM bootstrap security review](upgrade/m6p_validation.md)
- [M6Q bootstrap audit-config recovery](upgrade/m6q_validation.md)
- [M6R post-bootstrap GCP security verification](upgrade/m6r_validation.md)
- [M6S provisioner-access window readiness](upgrade/m6s_validation.md)
- [M6T BigQuery dataset IAM partial-apply recovery](upgrade/m6t_validation.md)
- [M7A synthetic data ingestion readiness and gap assessment](upgrade/m7a_assessment.md)
- [M7B immutable synthetic ingestion packet](upgrade/m7b_validation.md)
- [M7C1 dedicated loader identity and least-privilege IAM](upgrade/m7c1_validation.md)
- [M5 validation](upgrade/m5_validation.md)
