# Inherited baseline and provenance

Upgrade M1 starts from `27e955fbac442483dc90d6fa2722770fd9418ba7`
(`Polish portfolio documentation and walkthrough`). This is a separate upgrade
sequence; original milestone 1 remains the historical scaffold milestone.

## Preserved history and artifacts

The inherited local Git graph has 12 commits, dated June 4–5, 2026. At assessment,
local `main` and the cached `origin/main` pointed to the same revision. No remote
verification was performed. M1 does not rewrite history, rename the package or
repository, commit, push, or deploy anything.

| Original milestone | Commit | Inherited scope |
| --- | --- | --- |
| 1 | `9b3ca27` | Python scaffold and CI |
| 2 | `dee3704` | Synthetic data generation |
| 3 | `900898c` | CSV ingestion and validation |
| 4 | `9b24bdf` | Features and typology indicators |
| 5 | `e90cd5c` | Fraud baseline, AML scores, prioritisation |
| 6 | `58f0da3` | Isolation Forest |
| 7 | `4e2d6cf` | NetworkX and network scores |
| 8 | `2ba89cc` | Keyword NLP triage |
| 9 | `2e4c978` | Local monitoring |
| 10 | `cd6f008` | Demonstration governance pack |
| 11 | `1b033f3` | GCP architecture blueprint |
| 12 | `27e955f` | Portfolio documentation |

[baseline_artifacts.json](baseline_artifacts.json) records the SHA-256 and byte
length of every inherited tracked file under `data/sample/`, `data/processed/`,
`outputs/sample/`, and `reports/sample/`: 43 artifacts including placeholders.
The hashes were captured before M1 changes. The session-wide test fixture checks
these bytes before and after pytest. They are historical fixtures, not regenerated
M1 results. The baseline inventory itself is documentation, not run evidence.

The inherited repository contains 171 tracked files, 56 source Python modules,
27 documents and 85 test-function definitions in 12 test modules. M0 inspected
those tests but did not execute pytest or Ruff. Current executed checks are
recorded separately in [validation.md](validation.md).

## Known inherited evaluation problems

- `account_suspicious_transaction_rate` is computed from labels across the full
  dataset. Several typology indicators read `suspicious_pattern` directly.
- Account, beneficiary and device aggregates include future rows before a random
  train/test split. Numeric feature inference admits these derived fields.
- AML adds points for `is_suspicious`; graph features use suspicious labels;
  NLP can fall back to the original supplied typology label.
- The saved fraud metrics are legacy demonstrations: ROC AUC 0.9542140921409213,
  precision 0.8390804597701149, recall 0.8111111111111111, F1 0.8248587570621468.
  Their confusion matrix is `[[792, 28], [34, 146]]`. M0 reconciled saved rows,
  but did not reproduce training. Leakage invalidates generalisation claims.
- The original classifier scores only its 1,000 holdout transactions; AML,
  anomalies and networks cover all 5,000. This is not unified population scoring.
- Governance audit records use a fixed timestamp and unconditional completion
  status. Checklist success means a path exists, not an effective control.

The independent evaluator reads raw transaction observations, never the inherited
feature table or scores. Its different features and chronological partitions mean
its metrics are not a like-for-like comparison with the legacy figures.

## Unresolved synthetic and operational limits

The sample has 500 customers, 700 accounts, 5,000 transactions, 800 beneficiaries,
600 devices, 900 alerts and 900 notes. There are 900 suspicious-labelled transactions.
Read-only M0 checks found transactions before account opening (328), customer
onboarding (467), beneficiary first seen (504), and device first seen (458).
M1 preserves these records and does not repair the generator or consume those
entity timestamps. Pattern labels do not always correspond to generated temporal
behaviour. Random entity reuse produces weakly discriminating network indicators.

Inherited aggregates combine nominal amounts across currencies without conversion.
Boolean validation has a mask/count inversion defect; anomaly normalisation lacks
a constant-score guard. Monitoring can report overall success alongside drift
warnings. These remain backlog items, not changes to unrelated M1 workflows.

No real fraud outcomes, investigator dispositions, independent validation,
calibration, fairness study, uncertainty estimate, production security testing or
GCP deployment is established. The row-local M1 features avoid future aggregates
but cannot make synthetic data representative of real banking behaviour.

## Donor provenance: inspected, not imported

All paths below are sibling local folders under `GitHub_repository`. These are
assessment references. **M1 copies no donor code, data, models or dependencies.**
The new evaluator is implemented in this package. No donor Git history is merged.

| Local folder | Inspected HEAD | Relevant ideas and restrictions |
| --- | --- | --- |
| `azure-financial-crime-risk-intelligence-platform` | `102237503dda4f002c81bec4cfc5090ff8ddfc2c` | Prior-only features, chronological splits, customer risk and explanations. Threshold tuning uses test data; do not reproduce that. No license file found. |
| `aws-enterprise-risk-intelligence-platform` | `e0d72e6746c7d2ed7540439abf0c74b628c8a780` | Statistical comparisons; challenger metrics are simulated. No license file found. |
| `gcp-enterprise-rag-agent-platform` | `8bd3d765d2e6346baa4330faf564937037a4c1b4` | Local retrieval/citations and API patterns; no hosted LLM evidence. License file present. |
| `gcp-realtime-data-engineering-platform` | `9c400bc33e0f5777b3e53eb9ea2d6d4d50b0aa99` | Local idempotency/replay; Beam is a reference skeleton. License file present. |
| `gcp-vertex-ai-mlops-predictive-platform` | `0923f94d246a20deffec3a1fa5752f7d809f1955` | Local gates/serving and cloud templates; Vertex proxy is a stub and deployment evidence folders are placeholders. License file present. |
| `transaction-fraud-detection` | `851b6869b316b766f5f5589a17f02f0aadbd6987` | Upstream history includes Clébio Oliveira Júnior. Serving example has path/response defects. License file present. |
| `Credit-Card-Fraud-Detection` | `249b26551cb880775fd6c0f88c61833a8a16e6ea` | Upstream history includes Sagnik Ghosh; PyOD example includes target in input. License file present. |
| `Credit-Card-Application-Fraud-Analytics` | `2b0d92c7a1045f777f35bd54866f35a42efb8557` | Upstream history includes Harden Zhang; notebook-oriented. No license file found. |
| `Fraud-Analytics-with-AI-ML` | `7bdb5d1b8fbaff68a32fb6b6a2cf82260523a510` | Upstream history includes Renu Prakash; duplicate notebook code. No license file found. |
| `Credit-card-approval-prediction-classification` | `e06cbeeeb4d7438a4781a05d75a5861a29f5e0c4` | Upstream history includes semasuka stern; remote downloads/S3/joblib loading are excluded. License file present. |

`hmrc-compliance-risk-decision-support` exists locally with forecasting and
allocation scripts but is not a Git repository, so no commit provenance is
available. Both explicitly named assessment repositories were present; none was
cloned. The candidate itself has no license file. Any later copying requires
verification of authorship and applicable license terms, including attribution.
