# GCP Financial Crime ML Intelligence Platform

A financial crime machine learning platform built on synthetic data. It covers governed data engineering, chronological fraud-model evaluation, explainability, portable SQL scoring and drift monitoring. Each stage is implemented and tested locally, and a gated Google Cloud dev pilot scored the model in BigQuery with exact Python parity.

> **Status:** synthetic data only and **not production**. The model is approved only for offline development and the controlled synthetic-data pilot; **production approval is pending**. See [Limitations and Status](#limitations-and-status).

## Problem

Financial crime teams must detect suspicious activity across customers, accounts and transactions. They must prioritise noisy alert queues, explain each risk decision, monitor model behaviour, and keep evidence that auditors and model-risk reviewers can verify. A classifier alone does not meet that bar. The data lineage, evaluation, scoring path, cloud access and approvals all have to be controlled and reproducible.

## System Overview

```text
synthetic data -> validation and quarantine -> curated tables -> analytics marts
               -> point-in-time features -> chronological evaluation -> explainability
               -> hashed model specification -> generated SQL scoring -> parity and drift checks
```

The platform is batch-oriented. Every run writes immutable, hashed artifacts so each result can be traced to its inputs, code and configuration. Diagrams are in [`diagrams/`](diagrams/), and the design in [architecture](docs/architecture.md) and [GCP reference architecture](docs/gcp_reference_architecture.md).

The capabilities sit in three distinct tiers:

| Tier | Scope |
| --- | --- |
| **Implemented locally** (tested in CI) | Synthetic data generation and validation, raw → validated → curated foundation, SQLite analytics marts, feature engineering, model evaluation, explainability, SQL generation with SQLite parity, monitoring reference, governance artifacts, offline Terraform and IAM checks |
| **Verified in the GCP dev pilot** (operator-run, `europe-west2`) | Terraform-managed Cloud Storage and BigQuery with least-privilege IAM. Governed synthetic ingestion, BigQuery mart build and BigQuery batch scoring, each a single approved job inside a time-bound access window that was then revoked and verified |
| **Architecture only** (not deployed) | Vertex AI training, registry and monitoring; Dataflow; Pub/Sub; Cloud Run; Looker Studio; scheduled or streaming pipelines; online serving. See the [GCP service mapping](docs/gcp_service_mapping.md) |

## Capabilities

### Data engineering
- A synthetic data generator with UTC chronology, foreign keys, account currencies and exact integer minor-unit amounts ([contract](docs/synthetic_v2_contract.md)).
- A versioned raw → validated → curated foundation with explainable quarantine, per-currency reconciliation and immutable reruns ([foundation](docs/financial_data_foundation.md)).
- Four daily analytics marts with source lineage and independent grain and metric checks ([analytics](docs/financial_analytics.md)).
- **In GCP:**
  - a 4,250-row packet loaded from Cloud Storage into BigQuery, byte-identical on read-back ([M7D](docs/upgrade/m7d_closure.md));
  - the four marts (11,914 rows) built in one transactional BigQuery script and reconciled per currency ([M8](docs/upgrade/m8_closure.md)).

### Applied machine learning
- Point-in-time behavioural features with no future leakage.
- Rolling-origin validation: a logistic regression champion against a gradient-boosting challenger, a threshold chosen on validation data only, a sealed test window and day-block bootstrap confidence intervals ([M9A](docs/upgrade/m9a_validation.md)).
- Explainability:
  - exact per-feature contributions with reconstruction checks;
  - deterministic reason codes;
  - permutation importance with cross-fold rank stability ([M9B](docs/upgrade/m9b_validation.md)).
- Earlier baselines (AML scoring, Isolation Forest anomalies, NetworkX network risk, keyword NLP triage) remain as documented legacy demonstrations ([capability boundaries](docs/upgrade/capability_matrix.md)).

### BigQuery batch inference
- The frozen champion is exported as a canonical model specification bound to its SHA-256. GoogleSQL scoring is generated from that specification, never written by hand.
- An integer alert rule (`score_ppm >= 463905`) makes alerting robust to engine rounding.
- Exact row-level parity with the Python reference, checked offline on a SQLite rendering ([M9C](docs/upgrade/m9c_validation.md)).
- **In GCP:**
  - one capped script job (100 MiB limit) scored the pilot packet;
  - exact parity was then verified by reading every row back ([M9D](docs/upgrade/m9d_closure.md)).

### MLOps and monitoring
- Run manifests record the source, configuration and data hashes. Run IDs are never reused.
- Deployment bundles are deterministic and re-derived and byte-compared before use. Evidence verifiers run in stages and fail closed.
- A label-free monitoring reference uses fixed bins for the Population Stability Index (PSI) and alert-rate tracking. Score drift is computed from the cloud scores.
- CI runs Ruff and the full pytest suite on Python 3.11.

### Cloud infrastructure and security
- Modular Terraform with a separate IAM bootstrap root ([ADR-003](docs/architecture_decisions/ADR-003-separated-iam-bootstrap.md)).
- Custom least-privilege roles, and keyless service accounts for the provisioner, loader and runner.
- **Time-bound access windows:** token minting is granted only through an approved plan and carries an IAM expiry condition. Windows are kept to 3 hours or less by the runbook, then revoked, and revocation is verified live with an 8-check IAM capture.
- Every plan is SHA-pinned and must pass the repository plan checker and an independent second verification before apply.
- Terraform state is held in custody outside Git, with encrypted backups verified after an off-site round trip ([state custody](docs/terraform_state_custody.md)).
- Deletion protection on data resources, and a pilot runbook with explicit stop rules ([pilot plan](docs/gcp_pilot_plan.md)).

### Model governance
- A model card and champion/challenger decision record generated from evidence.
- An approval record bound by hash to that evidence. Its status is `approved_limited`; **production approval is pending**.
- Reason codes on every alert, and a human-in-the-loop review requirement. No decision is final or automated.
- [Model risk management](docs/model_risk_management.md) and [security and governance mapping](docs/security_and_governance_mapping.md).

## Verified GCP Pilot Results

The M9D controlled scoring run in BigQuery ([closure](docs/upgrade/m9d_closure.md)):

| Measure | Result |
| --- | --- |
| Synthetic transactions scored | **4,000** |
| Alerts | **198** (4.95%; reference 5.00%) |
| Python/BigQuery parity | **0 mismatches** across every row and column |
| Drift against the reference | Score PSI 0.005; all 22 features stable |
| Execution | One approved, SHA-pinned bundle; 8 BigQuery jobs out of a limit of 20; 30 MiB billed |
| Access | The runner window was opened, used and revoked. The final live IAM capture passed 8/8, with token minting denied |

Earlier pilot stages are documented in [M6T](docs/upgrade/m6t_validation.md) (infrastructure), [M7D](docs/upgrade/m7d_closure.md) (ingestion, closed with limitations) and [M8](docs/upgrade/m8_closure.md) (marts). Every cloud and Terraform step was run by the operator under separate approval.

## Quickstart

```bash
python3.11 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt && python -m pip install -e .
python -m ruff check . && python -m pytest
python -m financial_crime_ml.cli final-check
```

`final-check` (also `python scripts/final_project_check.py`) checks the repository's documents, sample artifacts and caveats. No cloud access or credentials are needed. `scripts/run_all_local.sh` regenerates the legacy demonstration samples and **overwrites committed sample files**, so run it only in a disposable copy ([walkthrough](docs/end_to_end_walkthrough.md)).

## Repository Layout

```text
configs/          Generation, evaluation, scoring and approval configuration
data/             Synthetic sample datasets and engineered features
diagrams/         Mermaid architecture and lifecycle diagrams
docs/             Architecture, modelling, governance and milestone evidence (docs/upgrade/)
infrastructure/   Terraform roots (pilot and IAM bootstrap) and IAM role definitions
scripts/          Local workflows, plan checkers and operator verification tools
sql/              Analytics and scoring SQL contracts
src/              Python package (financial_crime_ml)
tests/            Pytest suite
```

## Documentation

- **Start here:** [documentation index](docs/index.md) and [capability boundaries](docs/upgrade/capability_matrix.md).
- **Modelling:** [methodology](docs/modelling.md) and [monitoring strategy](docs/monitoring_strategy.md).
- **Cloud:** [deployment readiness](docs/gcp_deployment_readiness.md), [pilot plan and gates](docs/gcp_pilot_plan.md), and the ADRs: [ADR-001](docs/architecture_decisions/ADR-001-upgrade-in-place.md), [ADR-002](docs/architecture_decisions/ADR-002-minimal-gcp-pilot.md), [ADR-003](docs/architecture_decisions/ADR-003-separated-iam-bootstrap.md).
- **Milestone evidence:**
  - [M1](docs/upgrade/validation.md) through [M5](docs/upgrade/m5_validation.md);
  - [M6T](docs/upgrade/m6t_validation.md), [M7D](docs/upgrade/m7d_closure.md), [M8](docs/upgrade/m8_closure.md);
  - [M9A](docs/upgrade/m9a_validation.md), [M9B](docs/upgrade/m9b_validation.md), [M9C](docs/upgrade/m9c_validation.md), [M9D runbook](docs/upgrade/m9d_runbook.md), [M9D closure](docs/upgrade/m9d_closure.md).

## Limitations and Status

- **Synthetic data only.** No real customer data, intelligence or credentials. Metrics are not evidence of performance on real financial crime.
- **Not production.**
  - There is no scheduled, streaming or online-serving workload, and no live monitoring or alerting.
  - Production controls such as VPC Service Controls, CMEK, organisation policies and group-based ownership are absent. The pilot dataset's owner is a single named user account.
- **Production approval pending.** The decision record covers offline development and the synthetic pilot only.
- **Legacy samples and governance records.** The inherited `outputs/sample/` metrics are affected by target-derived features and are kept only as legacy demonstrations. The governance audit records are simulations.
- **Open evidence items:** actual billing and the token-minting audit-log mapping are carried forward ([M9D G7 decisions](docs/upgrade/m9d_closure.md#g7-decisions-approved-2026-11-18)).
- **Cloud resources and cost:**
  - **What remains deployed:** one versioned bucket (about 1.1 MB of synthetic objects) and one BigQuery dataset with eight deletion-protected tables (4,250 input, 11,914 mart and 4,000 score rows). The service accounts are dormant, with no open access window.
  - **No spending cap:** the £5 monthly budget **only sends alerts at 50/80/100%**. Nothing stops charges automatically.
  - **Cleanup:** teardown (Gate D) has not started and needs its own approval.
- **Human review required.** Any production use would need human-in-the-loop investigation, independent model validation, governance approval and compliance review.
