# Synthetic financial data contract v2.0.0

M2 adds an isolated pathway under `data_generation/v2/`. The inherited generator,
legacy schemas, CLI defaults and 43 committed sample artifacts are unchanged.
The inherited generator has invalid event/entity chronology and target-derived
helper fields; those samples remain legacy demonstration evidence. New v2 files
must not be substituted into the old feature/AML/graph/NLP pipelines.

## Run and revalidate

From the repository root, using the existing environment:

```bash
python3 -m financial_crime_ml.cli generate-data-v2 --run-id m2-data-1
python3 -m financial_crime_ml.cli validate-data-v2 \
  --dataset-dir outputs/runs/m2-data-1 --run-id m2-check-1
python3 -m financial_crime_ml.cli evaluate-fraud \
  --input outputs/runs/m2-data-1/evaluation_transactions.csv --run-id m2-fraud-1
```

`--config` selects a v2 YAML configuration. Generation also supports `--seed`,
`--customers`, `--accounts`, `--beneficiaries`, `--devices`, `--transactions` and
`--fraud-prevalence` overrides. Omit `--run-id` for a generated UUID-based ID.
Both new commands write only to fresh ignored `outputs/runs/<run-id>/` folders;
they reject path traversal, existing run IDs and protected sample destinations.
Revalidation never changes its input dataset and writes a separate report.
Failed validation exits nonzero and retains a failed report/manifest. Invalid CLI
configuration is rejected before generation. No command uses cloud services.

## Closed, versioned tables

`contract.json` records version `2.0.0`, exact required field sets, enumerations,
foreign keys, UTC rules and currency exponents. Missing/extra columns, duplicate
keys, extra CSV tables, unsupported versions and empty required tables are errors.
`configuration.json` records the effective configuration after CLI overrides.

| Table | Relationship and purpose |
| --- | --- |
| `customers.csv` | Unique customer, country and `onboarded_at` |
| `accounts.csv` | Unique account owned by one customer, base currency and `opened_at` |
| `beneficiaries.csv` | Unique beneficiary registration scoped to one account, country and `first_seen_at` |
| `devices.csv` | Unique device owned by one customer, IP country and `first_seen_at` |
| `transactions.csv` | Unique event, customer/account/beneficiary/device references, exact amount and transaction observations; no labels or scenario hints |
| `outcomes.csv` | One synthetic outcome per event: `is_suspicious`, episode ID and scenario type; never a predictor source |
| `evaluation_transactions.csv` | Exact observation export joined one-to-one with only `is_suspicious`, for the existing M1 evaluator |

IDs are opaque typed identifiers with eight digits. Transaction IDs are assigned
after chronological sorting and are excluded by M1. Outcome/episode fields are
absent from raw transactions. The export has no helper patterns or derived flags.
Validation verifies every exported observation against its source and every
exported label against the outcome table.

Customers may own multiple accounts and devices. Devices may be used by that
customer's accounts, never unrelated customers. Beneficiary IDs represent an
account's registration, not globally resolved legal entities. Every customer has
at least one account and device; every account has at least one beneficiary.
Configured counts must support those requirements. Transaction ownership must
agree across all referenced entities, not merely reference existing IDs.

## Temporal contract

All timestamps are explicit UTC whole-second strings: `YYYY-MM-DDTHH:MM:SSZ`.
Naive times, offsets (including `+00:00`), fractional seconds, impossible dates
and missing values fail. Transaction events lie in `[event_start, event_end)`;
all entity timestamps precede the configured exclusive cutoff. The window must
be at least one day. The contract does not consult the wall clock: it describes
a reproducible fictional snapshot, not proof that dates reflect current reality.

Required ordering, with equality allowed:

- Customer onboarding precedes account opening and device first-seen.
- Account opening precedes its beneficiary registration.
- Each transaction follows its customer onboarding, account opening, beneficiary
  registration and device first-seen.

The generator creates an established customer/account cohort before the event
window. Each account/customer has a pre-window beneficiary/device; additional
registrations may appear during the window. Events select only registrations
already available at their event time. A first-seen timestamp records availability
in the synthetic history; it need not equal the first observed event in this
window. This version has no ownership changes, account closure or slowly changing
entity state. Equal event timestamps are allowed; M1 keeps timestamp groups in
one partition.

## Currency and precision

`amount_minor` is authoritative: a positive canonical integer, at most
1,000,000,000. Integer monetary arithmetic is used throughout generation.
`amount` is its exact canonical decimal-string rendering at the currency's
configured exponent. No rounding or float acceptance repairs invalid input.
CSV revalidation loads strings to preserve precision and lexical errors.

Supported local contract: GBP, USD, EUR, SGD and AED use two decimal places; JPY
uses zero. The default subset is GBP/USD/EUR/JPY. This is an explicit supported
subset, not a complete currency standards database. Examples: 101 minor GBP is
`1.01`; 101 minor JPY is `101`. `1.001` GBP, `101.00` JPY, exponent notation,
leading-zero minor units, nonfinite values, zero and negatives fail.

Transaction currency must equal account base currency and belong to the run's
configured subset. Origin country matches the referenced device; destination
matches the beneficiary registration. No FX conversion, settlement amount,
balance, fee, refund or debit/credit ledger semantics are inferred. Reports sum
integer minor units separately by currency and deliberately have no combined
monetary total. M1 converts the decimal observation to float for its log transform;
that analytic feature is not an accounting representation.

## Scenarios and outcome separation

The prevalence parameter controls injected suspicious intent, not a rule evaluated
against the labels. The exact suspicious count is `transactions * prevalence`,
rounded to the nearest integer using decimal half-up rounding. Allowed prevalence
includes 0 and 1. The remainder is legitimate background/control activity.

Concrete episodes are generated before outcome metadata is attached:

- Velocity burst: four payments on the same account/device/beneficiary over three
  minutes, with varying amounts.
- Round payments: three equal whole-major-unit payments, five minutes apart,
  over ten minutes. Amounts are 100, 200 or 500 nominal currency units; these are
  illustrative values, not regulatory thresholds.
- Amount spike: one payment at 8–15 times a private seeded account amount profile.
  Ordinary amounts use 20–200% of that profile. The profile is sampled independently
  of outcomes and is never exported as a label-derived predictor.

Suspicious episodes cycle through 4/3/1-event groups until the requested count is
exhausted, using singleton spikes for residual counts. Benign controls use the
same mechanisms: roughly a tenth of legitimate rows are reserved for burst/round
groups, and about a tenth of remaining events use the spike mechanism. Therefore
high amounts, repetitions and bursts are not exclusive proof of suspiciousness.
Validation checks multi-event episode size, common entity references, duration,
distinct timestamps and repeated round amounts. The spike multiplier is a
generation assumption, not an independent outcome-validation study.

No country, currency or identifier encodes a suspicious class. There are no
predictor fields computed from `is_suspicious`, episode type or later outcomes.
The M1 allowlist is unchanged; it does not yet model episode velocity or account
histories. Changing/removing outcome labels cannot change its observation features.

## Validation, provenance and reproducibility

Generation validates in-memory data and then validates the serialized CSV bundle
again. `data_quality_report.json` records each rule's violation count, affected
sample IDs, row counts, realised prevalence, scenario mix and per-currency totals.
Missing files, broken descriptors and parse failures produce explicit failed
bundle reports. Revalidation reads each input file once, fingerprints the exact
consumed bytes and ignores any previously recorded quality result.

Actual manifests record operation, status, timestamps, seed/effective configuration,
Git revision/dirty state, source hashes, package versions and every output hash.
The implementation reuses the M1 provenance helper and existing dependencies;
no donor code, datasets, serialized models or external services were imported.
M2 starts from M1 commit `7ab53915990ae5dc9bd742f0904e07bf38e61c27`.

The same version/configuration/seed/environment gives byte-identical seven CSVs,
contract, effective configuration and quality report. Run manifests differ in
identity and actual wall-clock metadata. NumPy's existing generator supplies
randomness; exact results across library versions or platforms are not promised.
Source hashes identify the uncommitted M2 implementation.

## Limitations and compatibility

This is a deliberately small synthetic behavioural model, not representative
banking data or real fraud evidence. Amount profiles and scenario mix are hand
chosen, customer ownership is simple, events can overlap, and no delayed labels,
adversarial adaptation or real dispositions are modelled. Outcomes are synthetic
ground truth, not investigation or regulatory decisions.

`generate-data`, `validate-data` and all inherited commands retain their legacy
semantics. The v2 bundle does not include legacy alerts or case notes and is not
compatible with that old seven-table workflow. Only the explicit M1 export is
the supported modelling interface. M1 still rejects a single-class partition;
generation does not resample labels or time windows to force evaluability or
improve metrics. There is no new feature engineering, model tuning, banking
domain, API, dashboard or deployment in M2.

Executed checks and remaining risks: [M2 validation](upgrade/m2_validation.md).
