"""Final repository readiness checks for the portfolio project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REQUIRED_SOURCE_FOLDERS = [
    Path("src/financial_crime_ml/data_generation"),
    Path("src/financial_crime_ml/ingestion"),
    Path("src/financial_crime_ml/features"),
    Path("src/financial_crime_ml/models"),
    Path("src/financial_crime_ml/scoring"),
    Path("src/financial_crime_ml/monitoring"),
    Path("src/financial_crime_ml/governance"),
]

REQUIRED_DOCS = [
    Path("README.md"),
    Path("docs/index.md"),
    Path("docs/end_to_end_walkthrough.md"),
    Path("docs/architecture_narrative.md"),
    Path("docs/sample_outputs_guide.md"),
    Path("docs/repository_quality_checklist.md"),
    Path("docs/gcp_reference_architecture.md"),
    Path("docs/deployment_blueprint.md"),
    Path("docs/model_risk_management.md"),
]

REQUIRED_SCRIPTS = [
    Path("scripts/generate_demo_data.py"),
    Path("scripts/validate_demo_data.py"),
    Path("scripts/build_features.py"),
    Path("scripts/train_fraud_model.py"),
    Path("scripts/run_anomaly_detection.py"),
    Path("scripts/run_network_risk.py"),
    Path("scripts/run_nlp_triage.py"),
    Path("scripts/run_monitoring.py"),
    Path("scripts/generate_governance_pack.py"),
    Path("scripts/validate_docs.py"),
    Path("scripts/run_all_local.sh"),
    Path("scripts/final_project_check.py"),
]

REQUIRED_SAMPLE_OUTPUTS = [
    Path("data/sample/customers.csv"),
    Path("data/sample/accounts.csv"),
    Path("data/sample/transactions.csv"),
    Path("outputs/sample/data_quality_report.json"),
    Path("data/processed/transaction_features.csv"),
    Path("outputs/sample/model_metrics.json"),
    Path("outputs/sample/fraud_predictions.csv"),
    Path("outputs/sample/aml_risk_scores.csv"),
    Path("outputs/sample/prioritised_alerts.csv"),
    Path("outputs/sample/anomaly_scores.csv"),
    Path("outputs/sample/network_risk_scores.csv"),
    Path("outputs/sample/nlp_alert_triage.csv"),
    Path("outputs/sample/monitoring_summary.json"),
    Path("outputs/sample/governance_control_checklist.json"),
]

REQUIRED_REPORTS = [
    Path("reports/sample/model_card.md"),
    Path("reports/sample/model_monitoring_report.md"),
    Path("reports/sample/governance_evidence_pack.md"),
    Path("reports/sample/model_risk_management_report.md"),
]

REQUIRED_DIAGRAMS = [
    Path("diagrams/gcp_reference_architecture.mmd"),
    Path("diagrams/ml_lifecycle.mmd"),
    Path("diagrams/governance_workflow.mmd"),
    Path("diagrams/distributed_dataflow.mmd"),
    Path("diagrams/deployment_phases.mmd"),
]

README_REQUIRED_SECTIONS = [
    "Problem",
    "System Overview",
    "Capabilities",
    "Verified GCP Pilot Results",
    "Quickstart",
    "Repository Layout",
    "Documentation",
    "Limitations and Status",
]

DOCUMENTED_COMMANDS = [
    "scripts/run_all_local.sh",
    "scripts/final_project_check.py",
    "financial_crime_ml.cli final-check",
]

OBVIOUS_CREDENTIAL_FILES = [
    Path(".env"),
    Path("service-account.json"),
    Path("service_account.json"),
    Path("credentials.json"),
    Path("gcp_credentials.json"),
    Path("terraform.tfvars"),
]


@dataclass(frozen=True)
class FinalProjectCheckResult:
    """Result from final project readiness checks."""

    status: str
    checks_run: int
    issues: list[str]

    @property
    def passed(self) -> bool:
        """Return whether all checks passed."""
        return self.status == "passed"


def _require_paths(root: Path, paths: list[Path], label: str, issues: list[str]) -> int:
    checks = 0
    for path in paths:
        checks += 1
        if not (root / path).exists():
            issues.append(f"Missing {label}: {path}")
    return checks


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def run_final_project_check(repo_root: Path | str = ".") -> FinalProjectCheckResult:
    """Run final readiness checks for the local portfolio repository."""
    root = Path(repo_root)
    issues: list[str] = []
    checks_run = 0

    checks_run += _require_paths(root, REQUIRED_SOURCE_FOLDERS, "source folder", issues)
    checks_run += _require_paths(root, REQUIRED_DOCS, "document", issues)
    checks_run += _require_paths(root, REQUIRED_SCRIPTS, "script", issues)
    checks_run += _require_paths(root, REQUIRED_SAMPLE_OUTPUTS, "sample output", issues)
    checks_run += _require_paths(root, REQUIRED_REPORTS, "report", issues)
    checks_run += _require_paths(root, REQUIRED_DIAGRAMS, "Mermaid diagram", issues)

    readme_path = root / "README.md"
    if readme_path.exists():
        readme_text = _read_text(readme_path)
        readme_lower = readme_text.lower()
        for section in README_REQUIRED_SECTIONS:
            checks_run += 1
            if f"## {section}".lower() not in readme_lower:
                issues.append(f"README missing section: {section}")
        for phrase in ["synthetic data", "not production", "human-in-the-loop"]:
            checks_run += 1
            if phrase not in readme_lower:
                issues.append(f"README missing required caveat phrase: {phrase}")
        for command in DOCUMENTED_COMMANDS:
            checks_run += 1
            if command not in readme_text:
                issues.append(f"README missing documented command: {command}")

    for credential_path in OBVIOUS_CREDENTIAL_FILES:
        checks_run += 1
        if (root / credential_path).exists():
            issues.append(f"Obvious credential-like file is present: {credential_path}")

    status = "passed" if not issues else "failed"
    return FinalProjectCheckResult(status=status, checks_run=checks_run, issues=issues)
