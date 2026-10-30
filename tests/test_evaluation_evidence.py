"""Evidence authenticity, reproducibility, path safety and CLI compatibility."""

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from financial_crime_ml import cli
from financial_crime_ml.evaluation.fraud_baseline import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_INPUT_PATH,
    run_fraud_evaluation,
)
from financial_crime_ml.ingestion.load_data import REPO_ROOT


def test_runs_are_reproducible_and_evidence_matches_actual_artifact_bytes(tmp_path):
    first = run_fraud_evaluation(run_id="first", runs_root=tmp_path)
    second = run_fraud_evaluation(run_id="second", runs_root=tmp_path)
    manifest = json.loads((first["run_dir"] / "manifest.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["test_evaluations"] == 1
    assert manifest["threshold_source"] == "validation"
    assert datetime.fromisoformat(manifest["completed_at"]) >= datetime.fromisoformat(
        manifest["started_at"]
    )
    assert first["metrics"] == second["metrics"]
    assert manifest["source_fingerprint_sha256"] == second["manifest"]["source_fingerprint_sha256"]
    for filename, digest in manifest["artifacts_sha256"].items():
        content = (first["run_dir"] / filename).read_bytes()
        assert hashlib.sha256(content).hexdigest() == digest
        assert content == (second["run_dir"] / filename).read_bytes()
    assert (
        manifest["inputs"]["transactions"]["sha256"]
        == hashlib.sha256(DEFAULT_INPUT_PATH.read_bytes()).hexdigest()
    )
    assert (
        manifest["inputs"]["configuration"]["sha256"]
        == hashlib.sha256(DEFAULT_CONFIG_PATH.read_bytes()).hexdigest()
    )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert manifest["git_revision"] == revision
    assert "src/financial_crime_ml/evaluation/fraud_baseline.py" in manifest["source_sha256"]
    assert {"python", "numpy", "pandas", "scipy", "scikit-learn", "PyYAML"} == set(
        manifest["versions"]
    )
    saved = pd.read_csv(first["run_dir"] / "test_predictions.csv")
    actual, predicted = saved.is_suspicious, saved.fraud_prediction
    assert first["metrics"]["confusion_matrix"] == [
        [
            int(((actual == 0) & (predicted == 0)).sum()),
            int(((actual == 0) & (predicted == 1)).sum()),
        ],
        [
            int(((actual == 1) & (predicted == 0)).sum()),
            int(((actual == 1) & (predicted == 1)).sum()),
        ],
    ]


def test_failed_run_records_failure_without_claiming_completion(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_fraud_evaluation(tmp_path / "missing.csv", run_id="failed", runs_root=tmp_path)
    manifest = json.loads((tmp_path / "failed/manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["error"]["type"] == "FileNotFoundError"
    assert manifest["test_evaluations"] == 0
    assert "completed_at" not in manifest
    assert not (tmp_path / "failed/metrics.json").exists()


def test_run_ids_cannot_overwrite_runs_or_escape_the_output_root(tmp_path):
    run_fraud_evaluation(run_id="existing", runs_root=tmp_path)
    before = (tmp_path / "existing/manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        run_fraud_evaluation(run_id="existing", runs_root=tmp_path)
    assert before == (tmp_path / "existing/manifest.json").read_bytes()
    for invalid in ("../escape", "/absolute", "a/b", ".", "a" * 81):
        with pytest.raises(ValueError, match="run_id"):
            run_fraud_evaluation(run_id=invalid, runs_root=tmp_path)
    with pytest.raises(ValueError, match="committed samples"):
        run_fraud_evaluation(run_id="blocked", runs_root=REPO_ROOT / "outputs/sample")


def test_cli_new_command_dispatch_and_legacy_status_and_governance(monkeypatch, capsys):
    from financial_crime_ml.evaluation import fraud_baseline

    calls = []
    monkeypatch.setattr(fraud_baseline, "evaluation_main", lambda args: calls.append(args))
    cli.main(["evaluate-fraud", "--run-id", "isolated"])
    assert calls == [["--run-id", "isolated"]]
    cli.main([])
    assert cli.get_status_message() in capsys.readouterr().out

    def legacy_governance():
        calls.append("legacy_governance")
        return {
            "control_count": 19,
            "assessment_count": 9,
            "governance_evidence_pack_path": Path("legacy-report.md"),
        }

    monkeypatch.setattr(cli, "run_governance_pack_workflow", legacy_governance)
    cli.main(["generate-governance-pack"])
    assert calls[-1] == "legacy_governance"
    assert "Governance evidence pack generated." in capsys.readouterr().out
