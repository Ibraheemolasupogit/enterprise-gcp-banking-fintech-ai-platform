"""Governance evidence pack workflow."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from financial_crime_ml.governance.audit_log import build_audit_events, write_audit_log
from financial_crime_ml.governance.control_checklist import (
    build_control_checklist,
    load_governance_config,
)
from financial_crime_ml.governance.evidence_pack import (
    build_evidence_inventory,
    generate_governance_evidence_pack,
)
from financial_crime_ml.governance.lifecycle_traceability import build_lifecycle_traceability
from financial_crime_ml.governance.model_risk_assessment import build_model_risk_assessment
from financial_crime_ml.models.model_utils import resolve_repo_path

OUTPUT_PATHS = {
    "control_checklist": "outputs/sample/governance_control_checklist.json",
    "model_risk_assessment": "outputs/sample/model_risk_assessment.json",
    "evidence_inventory": "outputs/sample/evidence_inventory.json",
    "audit_log": "outputs/sample/audit_log.jsonl",
    "lifecycle_traceability": "outputs/sample/lifecycle_traceability.json",
    "governance_evidence_pack": "reports/sample/governance_evidence_pack.md",
    "model_risk_management_report": "reports/sample/model_risk_management_report.md",
}


def _write_json(payload: Any, output_path: str | Path) -> Path:
    path = resolve_repo_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def generate_model_risk_management_report(
    checklist: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
    output_path: str | Path,
) -> Path:
    """Write model risk management summary report."""
    path = resolve_repo_path(output_path)
    status_counts = Counter(control["status"] for control in checklist)
    residual_risk_counts = Counter(item["residual_risk_level"] for item in assessments)
    key_risks = sorted({risk for item in assessments for risk in item["key_risks"]})
    content = f"""# Model Risk Management Report

## Purpose

This report summarises model risk management controls for the synthetic financial crime ML
intelligence platform.

## Risk Governance Approach

The approach uses local evidence generation, transparent controls, model/component risk
assessment, deterministic audit logging, lifecycle traceability, and explicit human review
requirements.

## Component Inventory

Components assessed: {[item["model_or_component_name"] for item in assessments]}

## Model Risk Assessment Summary

Residual risk counts: {dict(residual_risk_counts)}

Production readiness remains portfolio demonstration only.

## Key Risks

{key_risks}

## Mitigating Controls

Controls include synthetic-data-only constraints, validation reports, model card, reason codes,
monitoring outputs, audit log, traceability, and human review requirements.

## Control Status Summary

{dict(status_counts)}

## Open Issues

- No live approval workflow.
- No independent model validation sign-off.
- No production monitoring integration.
- No artefact hashing or model registry.

## Human Review Requirement

All outputs require financial crime investigator or control-owner review before any operational
interpretation.

## Responsible AI Notes

The repository uses synthetic data, transparent rules where possible, documented limitations,
no autonomous final decisioning, and no real customer data.

## Financial Services Relevance

The pack demonstrates model risk awareness, auditability, control evidence, lifecycle
traceability, and responsible use expectations relevant to financial crime ML systems.

## GCP Governance Alignment Note

The local artefacts map conceptually to Vertex AI metadata, Cloud Logging audit trails,
BigQuery governance evidence tables, Cloud Storage artefacts, and Looker Studio governance
reporting. No live GCP governance tooling is implemented.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def run_governance_pack_workflow(output_dir: str | Path | None = None) -> dict[str, Any]:
    """Generate legacy demonstration reports, optionally outside the sample folders.

    With output_dir, preserve the outputs/sample and reports/sample hierarchy below
    that directory. Evidence references still describe inherited repository samples;
    they are not an execution attestation. None preserves the legacy CLI contract.
    """
    paths = {
        key: resolve_repo_path(value) if output_dir is None else Path(output_dir).resolve() / value
        for key, value in OUTPUT_PATHS.items()
    }
    config = load_governance_config()
    checklist = build_control_checklist(config)
    assessments = build_model_risk_assessment()
    inventory = build_evidence_inventory()
    traceability = build_lifecycle_traceability()
    audit_events = build_audit_events()

    checklist_path = _write_json(checklist, paths["control_checklist"])
    assessment_path = _write_json(assessments, paths["model_risk_assessment"])
    inventory_path = _write_json(inventory, paths["evidence_inventory"])
    traceability_path = _write_json(traceability, paths["lifecycle_traceability"])
    audit_log_path = write_audit_log(audit_events, paths["audit_log"])
    evidence_pack_path = generate_governance_evidence_pack(
        checklist,
        assessments,
        inventory,
        traceability,
        paths["governance_evidence_pack"],
    )
    model_risk_report_path = generate_model_risk_management_report(
        checklist,
        assessments,
        paths["model_risk_management_report"],
    )

    return {
        "control_checklist_path": checklist_path,
        "model_risk_assessment_path": assessment_path,
        "evidence_inventory_path": inventory_path,
        "audit_log_path": audit_log_path,
        "lifecycle_traceability_path": traceability_path,
        "governance_evidence_pack_path": evidence_pack_path,
        "model_risk_management_report_path": model_risk_report_path,
        "control_count": len(checklist),
        "assessment_count": len(assessments),
        "evidence_item_count": len(inventory),
    }
