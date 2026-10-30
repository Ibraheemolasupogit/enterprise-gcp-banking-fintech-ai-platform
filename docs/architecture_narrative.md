# Architecture Narrative

## Financial Crime Problem

Financial crime teams need to prioritise suspicious behaviour across transactions, accounts, customers, beneficiaries, devices, alerts, and investigation notes. The challenge is not only model accuracy. A useful platform also needs explainable scores, data quality checks, monitoring, governance evidence, human review, and a clear path to cloud-scale operation.

## Data Lifecycle

The local lifecycle starts with deterministic synthetic data generation. The repository creates sample customers, accounts, transactions, beneficiaries, devices, alerts, and case notes. The ingestion layer validates schemas, required values, primary keys, data types, categorical values, numeric ranges, and foreign-key style relationships.

This mirrors the production need for governed landing zones, validation gates, rejected record handling, and auditability before data is used for features or models.

## ML Lifecycle

The legacy feature layer produces transaction-level engineered features, including amount, time, velocity, customer, account, beneficiary, device, and typology indicators. Although raw helper fields are excluded from the legacy classifier, derived labels and full-dataset aggregates leak into its model inputs. Its metrics remain historical demonstrations.

Upgrade M1 adds a separate evaluator over a fixed allowlist of raw transaction-time observations, with chronological train/validation/test windows, training-only preprocessing and validation-only threshold selection. It writes actual run evidence under ignored `outputs/runs/` and does not alter the inherited scoring chain. See `docs/modelling.md`.

The design intentionally keeps model training lightweight so the repository remains understandable and reproducible.

## Risk Scoring Lifecycle

Fraud predictions are combined with deterministic AML risk scoring and alert prioritisation. Additional risk layers include anomaly detection, network risk modelling, and NLP alert triage. Each layer writes explicit outputs with scores, bands, reason codes, and caveats.

The platform treats model outputs as decision-support evidence, not autonomous final decisions.

## Monitoring Lifecycle

Monitoring covers data drift, prediction distributions, risk band movement, anomaly rates, network risk distributions, NLP triage distributions, alert quality, and high-priority volume. The monitoring layer demonstrates the operational questions a production financial crime ML system must answer after scoring begins.

## Governance Lifecycle

The governance layer consolidates control evidence, model risk assessments, evidence inventory, deterministic audit logs, lifecycle traceability, and markdown reports. It documents intended use, limitations, human review requirements, non-production caveats, and responsible AI controls.

## GCP-Aligned Production Architecture

The local artifacts map conceptually to a GCP architecture using Cloud Storage for landing and evidence, BigQuery for curated tables and monitoring outputs, Dataflow for scalable transforms, Vertex AI for model training and batch prediction, Cloud Logging for operational events, and Dataplex-style metadata for governance.

This repository documents the mapping but does not deploy live GCP infrastructure.

## Human-in-the-Loop Review

High-risk fraud, AML, anomaly, network, and NLP outputs should route to authorised investigators. The platform provides risk prioritisation, reason codes, and evidence links. It does not make final decisions, block transactions, close cases, or replace compliance review.

## Why Local-First Works for Portfolio Demonstration

A local-first implementation makes the full lifecycle inspectable without cloud credentials, cost, or access barriers. Reviewers can run the workflow end to end, inspect generated artifacts, read the governance evidence, and understand how the same design would map to production-style GCP services.
