# ADR-001: Upgrade in place with an isolated fraud evaluation

Status: accepted for the user-approved upgrade M1.

## Context

The inherited `financial_crime_ml` package has a coherent batch financial-crime
workflow and 12 incremental commits. Existing sample files are useful historical
fixtures, but inherited evaluation leaks labels and future aggregates. A fresh
repository would duplicate working interfaces and obscure that history.

## Decision

Retain the repository name, package structure, Git graph, CLI commands and committed
samples. Add `evaluation/` and `evaluate-fraud` as a separate, bounded local path.
Use a fixed transaction-time allowlist, chronological train/validation/test windows,
training-only preprocessing and validation-only threshold selection. Do not feed
new evaluation results into inherited AML, graph, NLP or governance reports.

Preserve timestamp groups at split boundaries and reject single-class partitions.
Write only to fresh ignored `outputs/runs/<run-id>/` directories. Capture real
execution status and source fingerprints, including uncommitted modules; a Git
revision alone cannot identify this uncommitted M1 implementation.

Governance output destinations become configurable without changing the no-argument
legacy API/CLI. Tests use temporary destinations. The old demo runs only in a
disposable copy during M1 validation because it deliberately writes sample paths.

## Consequences and exclusions

Legacy sample metrics and simulated audit records remain byte-for-byte intact and
are explicitly labelled historical demonstrations in documentation. New metrics
may be lower; there is no performance-improvement acceptance target. Repeatability
applies to numerical results and artifact bytes in the same environment, not run
IDs, wall-clock timestamps or changing working-tree status.

Only existing dependencies are used. There are no donor code imports, repository
renames, commits, pushes, cloud resources, external services, frameworks, APIs,
dashboards or additional banking domains in M1. Shared platform contracts and
full point-in-time entity histories remain later decisions.
