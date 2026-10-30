# Governance Evidence Pack

The governance evidence pack consolidates controls, model risk assessments, evidence inventory, audit log, lifecycle traceability, and markdown reports for the synthetic financial crime ML platform.

These inherited artifacts are **legacy demonstrations**. The audit generator
unconditionally emits completed events at a fixed timestamp; it does not observe
pipeline execution. Checklist success tests path existence, not control
effectiveness. Upgrade M1 preserves the committed samples and records genuine
fraud-evaluation execution separately in `outputs/runs/<run-id>/manifest.json`.

## Evidence Pack Structure

- Governance control checklist
- Model and component risk assessment
- Evidence inventory
- Static audit log
- Lifecycle traceability summary
- Governance evidence markdown report
- Model risk management markdown report

## Audit Log Design

The audit log is a local JSONL simulation with deterministic timestamps and
illustrative lifecycle events. It is not an execution attestation.

For isolated generation, call
`run_governance_pack_workflow(output_dir=temporary_directory)`. All generated
files retain their `outputs/sample/` or `reports/sample/` hierarchy under that
directory. Evidence references still point to inherited repository artifacts.
The no-argument legacy API/CLI remains unchanged. Governance tests always supply
temporary output directories.

## Lifecycle Traceability

Traceability maps each stage to input artefacts, output artefacts, config artefacts, documentation artefacts, risk controls, and limitations.

## Human Review Requirement

The pack reinforces that all model, scoring, anomaly, network, and NLP outputs require human review and must not be used for autonomous final decisioning.

## Responsible AI Controls

The project remains synthetic-data-only, local-first, explainable, documented, and bounded by explicit limitations.

## Live Tooling Exclusion

This milestone does not implement live governance workflow tooling, model registry integration, GCP deployment, APIs, dashboards, retraining automation, LLM workflows, or agentic AI.
