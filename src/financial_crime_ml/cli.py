"""Command-line entry point for the project."""

from __future__ import annotations

import sys

from financial_crime_ml.data_generation import generate_all_datasets
from financial_crime_ml.docs_validation import validate_architecture_docs
from financial_crime_ml.features import load_feature_config, run_feature_pipeline
from financial_crime_ml.governance import run_governance_pack_workflow
from financial_crime_ml.ingestion import DEFAULT_REPORT_PATH, run_data_validation
from financial_crime_ml.models.workflow import run_fraud_model_workflow
from financial_crime_ml.monitoring import (
    run_anomaly_detection_workflow,
    run_monitoring_workflow,
    run_network_risk_workflow,
    run_nlp_triage_workflow,
)
from financial_crime_ml.project_readiness import run_final_project_check

STATUS_MESSAGE = "GCP Financial Crime ML Intelligence Platform scaffold is ready."


def get_status_message() -> str:
    """Return the current scaffold status message."""
    return STATUS_MESSAGE


def main(argv: list[str] | None = None) -> None:
    """Run a simple project command."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "build-financial-marts":
        from financial_crime_ml.analytics.workflow import analytics_main

        analytics_main(args[1:])
        return

    if args and args[0] in {"build-ingestion-packet", "verify-ingestion-packet"}:
        from financial_crime_ml.deployment.ingestion_packet import build_main, verify_main

        command = build_main if args[0] == "build-ingestion-packet" else verify_main
        command(args[1:])
        return

    if args and args[0] == "render-ingestion-bundle":
        from financial_crime_ml.deployment.ingestion_bundle import render_main

        render_main(args[1:])
        return

    if args and args[0] == "verify-ingestion-evidence":
        from financial_crime_ml.deployment.ingestion_verify import verify_main

        verify_main(args[1:])
        return

    if args and args[0] == "ingest-financial-data":
        from financial_crime_ml.ingestion.foundation.cli import ingestion_main

        ingestion_main(args[1:])
        return

    if args and args[0] in {"generate-data-v2", "validate-data-v2"}:
        from financial_crime_ml.data_generation.v2.workflow import generation_main, validation_main

        command = generation_main if args[0] == "generate-data-v2" else validation_main
        command(args[1:])
        return

    if args and args[0] == "evaluate-fraud":
        from financial_crime_ml.evaluation.fraud_baseline import evaluation_main

        evaluation_main(args[1:])
        return

    if args == ["generate-data"]:
        written_files = generate_all_datasets()
        for dataset_name, file_path in written_files.items():
            print(f"Generated {dataset_name}: {file_path}")
        return

    if args == ["validate-data"]:
        report = run_data_validation()
        print(f"Data validation overall status: {report['overall_status']}")
        print(f"Validation issues: {len(report['validation_issues'])}")
        print(f"Report written to: {DEFAULT_REPORT_PATH}")
        return

    if args == ["build-features"]:
        config = load_feature_config()
        _, summary = run_feature_pipeline(config=config)
        print("Feature build completed.")
        print(f"Rows: {summary['number_of_rows']}")
        print(f"Columns: {summary['number_of_columns']}")
        print(f"Feature table written to: {config.transaction_features_path}")
        print(f"Feature summary written to: {config.feature_summary_path}")
        return

    if args == ["train-fraud-model"]:
        result = run_fraud_model_workflow()
        metrics = result["metrics"]
        print("Fraud model workflow completed.")
        print(f"Model: {metrics['model_name']}")
        print(f"F1 score: {metrics['f1_score']:.4f}")
        print(f"Metrics written to: {result['model_metrics_path']}")
        print(f"Prioritised alerts written to: {result['prioritised_alerts_path']}")
        return

    if args == ["run-anomaly-detection"]:
        result = run_anomaly_detection_workflow()
        summary = result["summary"]
        print("Anomaly detection completed.")
        print(f"Anomalies: {summary['anomaly_count']}")
        print(f"Anomaly rate: {summary['anomaly_rate']:.4f}")
        print(f"Anomaly scores written to: {result['anomaly_scores_path']}")
        print(f"Report written to: {result['anomaly_report_path']}")
        return

    if args == ["run-network-risk"]:
        result = run_network_risk_workflow()
        summary = result["summary"]
        print("Network risk workflow completed.")
        print(f"Nodes: {summary['node_count']}")
        print(f"Edges: {summary['edge_count']}")
        print(f"High-risk network rows: {summary['high_risk_network_count']}")
        print(f"Network scores written to: {result['network_risk_scores_path']}")
        print(f"Report written to: {result['network_report_path']}")
        return

    if args == ["run-nlp-triage"]:
        result = run_nlp_triage_workflow()
        summary = result["summary"]
        print("NLP alert triage completed.")
        print(f"Alerts triaged: {summary['alert_count']}")
        print(f"Case notes classified: {summary['case_note_count']}")
        print(f"Alert triage written to: {result['alert_triage_path']}")
        print(f"Report written to: {result['nlp_report_path']}")
        return

    if args == ["run-monitoring"]:
        result = run_monitoring_workflow()
        summary = result["monitoring_summary"]
        print("Monitoring workflow completed.")
        print(f"Overall status: {summary['overall_status']}")
        print(f"Total drifted features: {summary['total_drifted_features']}")
        print(f"Report written to: {result['monitoring_report_path']}")
        return

    if args == ["generate-governance-pack"]:
        result = run_governance_pack_workflow()
        print("Governance evidence pack generated.")
        print(f"Controls: {result['control_count']}")
        print(f"Risk assessments: {result['assessment_count']}")
        print(f"Evidence pack written to: {result['governance_evidence_pack_path']}")
        return

    if args == ["validate-docs"]:
        result = validate_architecture_docs()
        print(f"Documentation validation status: {result.status}")
        print(f"Documents checked: {result.docs_checked}")
        print(f"Mermaid diagrams checked: {result.diagrams_checked}")
        print(f"Issues: {len(result.issues)}")
        for issue in result.issues:
            print(f"- {issue}")
        if not result.passed:
            raise SystemExit(1)
        return

    if args == ["final-check"]:
        result = run_final_project_check()
        print(f"Final project check status: {result.status}")
        print(f"Checks run: {result.checks_run}")
        print(f"Issues: {len(result.issues)}")
        for issue in result.issues:
            print(f"- {issue}")
        if not result.passed:
            raise SystemExit(1)
        return

    print(get_status_message())


if __name__ == "__main__":
    main()
