# M9C: portable fraud model and SQL scoring

> **Final status (2026-11-18): spec v2 `m9c-spec-2.0.0`, the approved Option B, is the deployable specification.** See [Final: spec v2](#final-spec-v2-approved-option-b). The sections below it document the v1 export (`m9c-export-2`), kept as historical evidence. v1 rebuilds byte for byte.

Date 2026-11-18. Base `main` at `5dd9afc` (M9C uncommitted at run time; the manifest records it). Synthetic data only. **No GCP, Terraform, IAM or deployment; the champion is not refit; labels are never read.**

## Final: spec v2 (approved Option B)

**Approval** (`configs/approvals/DR-M9B-m9a-eval-1.json`, SHA-256 `084aca3835a7de224218c72661166f4c8b2da509648edb65e58db646d70a2995`):
- Decision record `DR-M9B-m9a-eval-1` (logistic regression) is **approved for offline development and for preparing the controlled synthetic-data pilot (M9D) only**. Status: `approved_limited`.
- **Production approval remains pending.** Real data, operational alerting and the M9D cloud steps without their own window approvals are explicitly not approved.
- The record is bound by hash to the M9B decision record (`916a9e48…ecf`) and the M9A selection (`846f40a7…169f`). The spec build refuses any mismatch, any change of status, or a claimed production approval.
- The M9B model card and decision record still say `pending_human_review`. They are historical evidence and are not edited; this record supersedes their approval status.

**Deployment-policy change.** The alert rule changes from `probability ≥ 0.4639053232165378` (FLOAT64) to **`score_ppm ≥ 463905` (INT64)**:

| Unchanged | Changed |
| --- | --- |
| The champion (coefficients, means, scales, categories, intercept), the M9A threshold value (kept as `decision.m9a_threshold`), and all M9A predictions and manifests | `decision` (rule, `threshold_ppm`, quantisation note), `provenance` (approval scope and record hash), `spec_version` |
| `feature_contract`, `input_validity`, `model`, `reason_codes`: identical to v1 (enforced by the build) | |

**Quantisation limitations of the integer rule:**
- **Effective threshold.** A row alerts when its probability rounds (half up) to at least 463,905 ppm, which means probability ≥ about 0.4639045. That is up to 8.2e-7 below the M9A threshold. Rows in that window alert under v2 but not v1.
- **Current data.** No row of the 20,000 falls in that window: the closest row is 0.82 ppm from the v2 boundary. Future data could.
- **Resolution.** Probabilities that differ by less than 1 ppm are indistinguishable for alerting; ranking and monitoring use `score_ppm`.
- **Remaining engine sensitivity.** It now sits only at the rounding half-point 463,904.5 ppm, where the smallest margin observed is 0.82 ppm. Across all rows, the smallest distance to any half-ppm rounding point is 3.0e-5 ppm (about 3e-11 in probability). That is many orders of magnitude above last-bit differences, so a cross-engine flip is implausible but not proven until M9D.
- **Monitoring** compares alert rates in ppm terms. The reference alert rate is now 5.000% (200 of 4,000 fold-3 rows, matching M9A).

**Evidence run `m9c-export-3`** (spec v2):

| Check | Result |
| --- | --- |
| **20,000-row alert parity vs the M9A champion** | **973 vs 973 alerts, 0 mismatches.** The v1 rule had 1, the threshold row. The M9A reference is scikit-learn's `LogisticRegression` with the frozen parameters and no fitting; it is bit-identical to all 8,000 stored M9A champion predictions (test and fold 3) |
| Test window vs stored M9A alerts | 0 mismatches (188 alerts) |
| **M9B reason codes** | 0 mismatches; contributions identical (Δ = 0) |
| **Python vs SQLite, full dataset** | 20,000 rows, 973 alerts: `score_ppm`, alert, reason codes, validity and integer features 0 mismatches; logit ≤ 1.8e-15; probability ≤ 3.3e-16 |
| Python vs SQLite, edge cases | 14 rows: 0 mismatches |
| **Model coefficients unchanged** | Spec terms equal M9A `model_parameters.json` value for value; the v1 and v2 model sections are identical |
| **Deterministic generation** | The spec builds twice with identical bytes. Historical v1 rebuilds byte for byte from `m9c-export-2` (`2fb13e17…4b09`). A test confirms a full rerun reproduces every artifact |
| Historical evidence | M9A `96eafe4e…`, M9B `218e09cd…`, and the `m9c-export-1/-2` artifact hashes are all unchanged |
| Labels read | None |

| Artifact (`m9c-export-3`) | SHA-256 |
| --- | --- |
| **`model_spec.json` (v2)** | **`38180198803761eeeb2f72838d8ed88021b5664b956d36fa006a7de519f239e0`** |
| `approval_record.json` | `084aca3835a7de224218c72661166f4c8b2da509648edb65e58db646d70a2995` |
| `scoring_select.googlesql.sql` | `d07e4c7d6d0f5b0162f05d8eaff21b7496f829c6b75ebd381fe7afa15b32286e` |
| `scoring_refresh.googlesql.sql` | `8912b36a554d22bed31f4ed4df103abe19bc9f397c145e7659d768d2791bdb0e` |
| `preflight.googlesql.sql` | `38dbe75f3f8535fa5d8fa38f2610564393d201cb030808f2f8b54afd008a7f14` |
| `monitoring_reference.json` | `c1f7fdbdd8322d1f4d1ab513df5bf8a75d8bcf35757638c405a2709a6fa6b025` |
| `parity_report.json` | `b42f236050b9d4cacddb9e2c565c677b8022a07e0085f3d55aae569be535308e` |
| `manifest.json` | `d086fff8fe894d135d180b5ed014c661f638753d1dffa4d4e596bb10cd4f6533` |

The generated GoogleSQL alert is `CASE WHEN CAST(FLOOR(probability × 1000000.0 + 0.5) AS INT64) >= 463905 THEN 1 ELSE 0 END`.

**Boundary tests** (new, in `tests/test_scoring_sql.py`, 34 tests in total):
- probabilities at and within ±3 floating-point steps of the 463,904.5-ppm half-point, and at 463,904 / 463,905 / 463,906 ppm, produce the same alert in Python and SQLite and follow `score_ppm ≥ 463905`;
- the threshold row alerts under v2 even one step below the M9A threshold (it would not under v1);
- the quantisation window is below 1 ppm;
- v2 requires a matching limited approval: a production claim, a wrong status, record hash, decision or `threshold_ppm` is refused;
- the M9A reference must be bit-identical;
- v1 and v2 differ only in the decision rule;
- generation is deterministic and the historical v1 export is reproduced.

## What M9C adds

| Piece | Where |
| --- | --- |
| Portable model specification and Python reference scorer | `deployment/model_spec.py` |
| GoogleSQL and SQLite renderings from one builder; refresh and preflight scripts | `deployment/scoring_sql.py` |
| Score table schema (proposed) | `sql/scoring/v1/transaction_risk_scores.schema.json` |
| Monitoring reference profile and PSI | `monitoring/reference_profile.py` |
| Run and CLI | `deployment/m9c_workflow.py`; `financial-crime-ml export-fraud-model --m9a-run <dir> --m9b-run <dir> [--run-id]` |
| Tests | `tests/test_scoring_sql.py` (25) |

**Reused:**
- `explainability` (M9A/M9B evidence loading, contributions, reason codes);
- `behavioural` (feature contract, Python features);
- `marts_bundle.render` (identifier substitution);
- `manifest`.

No new dependencies.

## The specification (`model_spec.json`)

Canonical JSON (sorted keys, no whitespace, round-trip floats), identified by the SHA-256 of its bytes. It contains:
- **Provenance:** M9A/M9B run IDs and manifest hashes, the selection and parameter hashes, and the decision record ID with its approval status (`pending_human_review`).
- **Feature contract:** the full M9A contract.
- **Input validity rules.**
- **Preprocessing:** the training mean, scale and coefficient of each numeric feature, and each categorical feature's category coefficients.
- **Model terms:** the intercept and threshold.
- **Decision rules:** the alert, probability and `score_ppm` rules.
- **Reason codes:** the M9B mapping (`m9b-reasons-1.0.0`) with its ranking and padding rules.

The build fails unless the spec reproduces the frozen champion exactly:
- coefficients, means, scales, categories and intercept are compared value for value with M9A's `model_parameters.json`;
- the spec is rebuilt and its bytes must match.

**Scoring rules** (identical in Python and SQL):

| Rule | Definition |
| --- | --- |
| Valid input | All 14 contract columns non-NULL, string columns non-empty, currency in the supported exponent list. Invalid rows are excluded from history and get NULL scores and codes |
| Logit | Intercept, plus each numeric coefficient × (x − mean)/scale, plus the coefficient of each active category. An unseen category contributes 0 |
| Probability | 1 / (1 + EXP(−logit)) |
| Alert | v1: probability ≥ 0.4639053232165378 (FLOAT64). **v2 (deployable): `score_ppm ≥ 463905` (INT64)** |
| `score_ppm` | `CAST(FLOOR(probability × 1,000,000 + 0.5) AS INT64)`: round half up, 0–1,000,000. The threshold is 463,905 ppm (informational) |
| Reason codes | For alerts: positive contributions, ranked by contribution descending then fixed feature order. Three slots, padded with `R00` |

## Generated SQL

| Artifact | Purpose |
| --- | --- |
| `scoring_select.googlesql.sql` | One row per source row: features, contributions, logit, probability, `score_ppm`, alert, reason codes |
| `scoring_refresh.googlesql.sql` | Transaction: BEGIN; `DELETE … WHERE TRUE`; explicit-column INSERT into `transaction_risk_scores`; COMMIT (the M8 idempotent pattern) |
| `preflight.googlesql.sql` | Read-only counts: rows, duplicate IDs, invalid rows, sub-second timestamps (all must be 0 except rows) |
| `scoring_select.sqlite.sql` | The same builder rendered for the offline parity engine |

- **Features:** windows are `RANGE BETWEEN w × 10⁶ PRECEDING AND 1 PRECEDING` over `UNIX_MICROS(transaction_timestamp)`, partitioned per the contract, so features are strictly past-only with no future leakage. A golden test checks the exact frames. Means use `CAST(SUM) AS FLOAT64 / COUNT`.
- **Inputs:** the only table read is `PROJECT.DATASET.curated_transactions`. Placeholders are resolved only by `marts_bundle.render`, with validated identifiers. The SELECT contains no DML, DDL, `RAND` or `CURRENT_*`.
- **Constants:** every model constant is an exact round-trip FLOAT64 literal from the hashed spec.
- **Dialect hooks:** the two renderings differ only in event-time units, hour and day-of-week extraction, casts, `MOD`, `LEAST`, and integer division of times.

## Evidence run `m9c-export-2`

**Inputs and integrity:**
- Built on `m9a-eval-1` and `m9b-explain-3`. Their directory hashes, `96eafe4e…` and `218e09cd…`, were identical before and after every M9C run.
- `labels_read: false`. M9A prediction files are read without their label column.
- `m9c-export-1` is superseded: it lacked the decision-margin section. Its spec, SQL and monitoring profile are byte-identical to run 2's.
- After the final typing fixes, a scratch rerun reproduced every artifact byte for byte.

| Artifact | SHA-256 |
| --- | --- |
| **`model_spec.json`** | **`2fb13e175f179dd52638805f7f583347c3593f52a6120ecc7e053b89e7644b09`** |
| `scoring_select.googlesql.sql` | `8491a362904831b702977bb33e116f74deb7cd946cf084a12a26cda8d4e6d038` |
| `scoring_refresh.googlesql.sql` | `cc45c0fef0bfdcbf8511d388dfbbf4f0c357ae24e6112c4e6702abbdae33895a` |
| `preflight.googlesql.sql` | `4d351fcc3d5dc891d745fc5e7def772fbc365f93c91a5c5c2f156466df4dca66` |
| `monitoring_reference.json` | `5f041d342b63db7f8ac937c59627282103f89fb712ed179169c14632b11d4dfb` |
| `parity_report.json` | `d68ca1fdf05f4d18fb64fc7303c14bba87eed78e7e7d525df0aaeac5602fc62d` |
| `manifest.json` | `578d03064adf3c9172ddcc728c670fd9f37defc0f399b5202a54316eafb1edf2` |

### Parity

| Comparison | Rows | Result |
| --- | --- | --- |
| Python reference vs M9A original model (test window) | 4,000 | Max \|probability Δ\| 3.3e-16; **0 alert mismatches** |
| Python reference vs M9A (fold-3 validation) | 4,000 | Max \|probability Δ\| 3.9e-16; 1 alert mismatch (the threshold row, below) |
| Python reference vs M9B | 4,000 contributions; 188 alerts | Contributions identical (Δ = 0); **0 reason-code mismatches** |
| **SQL vs Python, full dataset** | 20,000 (972 alerts) | INT64 features: 0 mismatches. FLOAT64 features: max Δ 8.9e-16. Categoricals: 0. Contributions: max Δ 3.3e-16. Logit: 1.8e-15. Probability: 3.3e-16. **`score_ppm` 0, alert 0, reason codes 0, validity 0 mismatches** |
| **SQL vs Python, edge cases** | 14 (9 valid, 6 alerts) | All exact or ≤ 8.9e-16; 0 mismatches |

The edge-case fixture covers:
- a zero-history account;
- a burst;
- two payments at the same instant (neither sees the other);
- unseen channel, merchant category and country (each contributes 0);
- a supported but unseen currency (SGD; contributes 0, row valid);
- a JPY round amount (exponent 0);
- a gap of more than 30 days (capped);
- NULL device, empty beneficiary, unsupported currency, NULL timestamp and NULL amount (each invalid, NULL score);
- an extreme amount.

A separate test proves that invalid rows never change any valid row's features or scores.

### Decision margins (how close any decision is to flipping)

| Margin over 20,000 rows | Value |
| --- | --- |
| Smallest \|probability − threshold\| | **1.1e-16**: row T2-00012042 (next smallest 2.5e-4) |
| Smallest distance of `score_ppm` × 10⁶ from a rounding half-point | 3.0e-5 ppm |
| Smallest gap between ranked reason contributions | 1.3e-4 |

**Finding: the threshold row.**
- T2-00012042 (fold-3 validation) is the row whose score *defined* the threshold: M9A set the threshold to the score of the last alert at 5% capacity, so that score equals the threshold exactly.
- The portable formula, sigmoid of the summed contributions, reproduces it one step (1.1e-16) lower. Python and SQL agree with each other, but neither alerts that row, while M9A did. Fold-3 alerts are 199 vs 200.
- The test window is unaffected, and every other row is at least 2.5e-4 from the threshold.

**Resolved by spec v2 (Option B approved); see the top of this document.** The options as originally presented:
- **(a) Keep the FLOAT64 rule** and record this one validation-row difference (recommended for now).
- **(b) Adopt the integer rule `score_ppm ≥ 463,905`.** It is engine-robust (its smallest rounding margin is 3e-5 ppm, about 3e-11 in probability) and matches M9A on all 20,000 rows. It would need a new spec version and approval, because it changes the stated operating rule.

## Monitoring reference profile

- **Reference window:** M9A `validation_3`, the window where the threshold was set: 4,000 rows, scored with the spec. No labels are used.
- **Contents:**
  - fixed decile cut points and proportions for every numeric feature;
  - category shares plus `__other__` for every categorical feature;
  - `score_ppm` proportions over fixed edges (0 / 1% / 2% / 5% / 10% / 20% / 30% / 50% / 100%) and deciles (median 25,850 ppm);
  - alert rate 4.975%.
- **PSI:** `Σ (a − e) ln(a/e)` with proportions floored at 1e-6. Bands: < 0.10 stable, 0.10–0.25 moderate, ≥ 0.25 significant.
- **Tests:** the reference window against itself gives PSI exactly 0. A forced score and channel shift is flagged as significant.

## BigQuery deployment contract (for gated M9D)

**Objects** (none exist yet):
- Table `transaction_risk_scores` in `bankpilot_dev`, using `sql/scoring/v1/transaction_risk_scores.schema.json`: deletion-protected and created by the **pilot Terraform root**.
- An additive runner table grant with the existing `bankpilot_dev_tables` role (`get`, `getData`, `updateData`).
- These need a pilot-root change and its plan checker (the pilot modes currently expect 18 resources), applied under an M6S-style provisioner window. That window can also run the overdue M7D A10 check.

**Identity:** the runner, through the M8 runner-window mechanism (`runner-access` / `runner-revoke`, live mint and denial checks). No new IAM permission or custom role is needed:

| Runner permission | Needed for | Source |
| --- | --- | --- |
| `bigquery.jobs.create` | Scoring and preflight jobs | Project jobs role |
| `getData` on `curated_transactions` | Reading the source | Existing readers grant |
| `get`, `getData`, `updateData` on `transaction_risk_scores` | Writing and reading back scores | New grant, existing tables role |

**Execution order:**
1. Preflight: must report 0 duplicates, 0 invalid rows and 0 sub-second timestamps.
2. Dry runs of the SELECT and of the refresh script. Expected referenced tables: `curated_transactions` (plus the target for the refresh).
3. The refresh script, with `maximumBytesBilled = 104857600`. Expect 4 child jobs (BEGIN, DELETE, INSERT, COMMIT).
4. `tabledata.list` read-back.
5. Offline parity against `score_python(spec)` on the same 4,000-row pilot history, using the M9C tolerances: integers and codes exact, floats ≤ 1e-12, logits ≤ 1e-9.
6. PSI against the reference profile (expected to drift: the pilot data is a different synthetic window).
7. Revoke the runner window.

**Cost**, using the repository's illustrative US$10/TiB and the M8 billing evidence (DELETE on an empty table bills 0; INSERT bills 10 MiB per referenced table, including the destination):
- preflight 10 MiB + INSERT about 20 MiB, so **about 30 MiB per run, roughly US$0.0003**;
- the upper bound at the cap is 100 MiB per job;
- dry runs and read-back are free;
- jobs: 1 preflight + 2 dry runs + 1 parent + 4 children = 8 of 20.

The £5 budget only alerts; it is not a spending cap.

## Remaining risks

1. **GoogleSQL is not executed yet.**
   - Its syntax and semantics are checked only structurally, plus by SQLite parity of the same builder.
   - Specific constructs still to prove on BigQuery: `EXTRACT … AT TIME ZONE`, `DIV`, `LEAST` with NULL, and `CAST(STRING AS FLOAT64)`.
   - M9D's dry run and live parity are the gate.
2. **Cross-engine floating point.** BigQuery `LN`/`EXP`/parsing may differ in the last bit.
   - The smallest margins are 0.82 ppm to the v2 alert boundary, 3e-5 ppm to any rounding half-point, and 1.3e-4 between ranked reason contributions.
   - Flips are therefore implausible, but are only proven by M9D live parity.
   - The v1 threshold-row sensitivity is removed by spec v2.
3. **History scope.**
   - Cloud features equal offline features only when the scored table holds the same transaction history.
   - The pilot table holds the 4,000-row M7B packet, a different synthetic window from the 20,000-row evaluation dataset.
   - M9D parity must therefore compare against `score_python` on the pilot packet, not on M9A outputs.
4. **Distribution shift.** The pilot packet comes from a different generator configuration, so PSI and alert rate against the reference may well drift. The model is not recalibrated.
5. **Governance.**
   - The approval is limited to offline development and preparing the synthetic pilot; production approval is pending.
   - M9C was run from uncommitted code (recorded in the manifest).
6. **Infrastructure.**
   - The new table, grant and checker modes are M9D work (a pilot Terraform change plus a provisioner window).
   - The M7D/M8 gaps (A10, bucket/dataset policy read-back, billing evidence) remain open.

## Readiness for gated M9D

**Ready:**
- the frozen, hashed spec;
- deterministic GoogleSQL (select, refresh, preflight);
- an exact offline parity harness and Python reference;
- the monitoring reference;
- the table schema;
- the cost and job envelope.

**Before M9D:**
- ~~approve `DR-M9B-m9a-eval-1` and choose a threshold option~~: done, approved for limited scope, Option B (spec v2);
- commit M9C, including the approval record;
- write and test the pilot Terraform change for the score table and runner grant, with checker modes;
- approve the provisioner window (with A10) and the runner window.
