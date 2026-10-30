"""Serialized v2 contract enforcement, run evidence, CLI and M1 integration."""

import hashlib
from dataclasses import replace

import pandas as pd
import pytest

from financial_crime_ml import cli
from financial_crime_ml.data_generation.v2 import workflow
from financial_crime_ml.data_generation.v2.contracts import SyntheticConfig
from financial_crime_ml.data_generation.v2.validation import load_v2
from financial_crime_ml.evaluation.fraud_baseline import run_fraud_evaluation
from financial_crime_ml.ingestion.load_data import REPO_ROOT


@pytest.fixture
def config():
    return SyntheticConfig(
        customers=10,
        accounts=15,
        beneficiaries=30,
        devices=20,
        transactions=600,
        fraud_prevalence=0.15,
    )


def test_serialized_runs_are_deterministic_revalidated_and_hashed(tmp_path, config):
    first = workflow.run_generation(config, "first", runs_root=tmp_path)
    second = workflow.run_generation(config, "second", runs_root=tmp_path)
    assert first["manifest"]["status"] == second["manifest"]["status"] == "completed"
    assert first["report"]["issue_count"] == 0
    assert first["manifest"]["seed"] == config.seed
    assert first["manifest"]["pre_serialization_checks"] > 100
    assert (
        first["manifest"]["source_fingerprint_sha256"]
        == second["manifest"]["source_fingerprint_sha256"]
    )
    assert first["manifest"]["artifacts_sha256"] == second["manifest"]["artifacts_sha256"]
    for name, digest in first["manifest"]["artifacts_sha256"].items():
        raw = (first["run_dir"] / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        assert raw == (second["run_dir"] / name).read_bytes()
    checked = workflow.run_validation(first["run_dir"], "check", runs_root=tmp_path)
    assert checked["manifest"]["status"] == "completed"
    assert checked["report"] == first["report"]
    loaded = load_v2(first["run_dir"])
    assert isinstance(loaded["transactions"].amount.iloc[0], str)
    assert all(pd.api.types.is_object_dtype(dtype) for dtype in loaded["transactions"].dtypes)


def test_validator_reads_actual_files_and_records_a_failed_report(tmp_path, config):
    generated = workflow.run_generation(config, "data", runs_root=tmp_path)
    path = generated["run_dir"] / "transactions.csv"
    original = path.read_bytes()
    data = pd.read_csv(path, dtype=str, keep_default_na=False)
    data.loc[0, "amount"] = "0.001"
    data.to_csv(path, index=False)
    tampered = path.read_bytes()
    failed = workflow.run_validation(generated["run_dir"], "tampered", runs_root=tmp_path)
    assert failed["manifest"]["status"] == "failed"
    assert failed["report"]["status"] == "failed"
    assert (
        failed["manifest"]["input_artifacts_sha256"]["transactions.csv"]
        == hashlib.sha256(tampered).hexdigest()
    )
    assert path.read_bytes() == tampered != original
    assert any(issue["check"] == "exact_money" for issue in failed["report"]["issues"])
    path.unlink()
    missing = workflow.run_validation(generated["run_dir"], "missing", runs_root=tmp_path)
    assert missing["manifest"]["status"] == "failed"
    assert missing["report"]["issues"][0]["check"] == "execution_or_load"


def test_contract_version_descriptor_and_run_paths_are_enforced(tmp_path, config):
    generated = workflow.run_generation(config, "data", runs_root=tmp_path)
    before = (generated["run_dir"] / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        workflow.run_generation(config, "data", runs_root=tmp_path)
    assert (generated["run_dir"] / "manifest.json").read_bytes() == before
    for run_id in ("../escape", "", "nested/path", "/absolute"):
        with pytest.raises(ValueError, match="run_id"):
            workflow.run_generation(config, run_id, runs_root=tmp_path)
    with pytest.raises(ValueError, match="committed samples"):
        workflow.run_generation(config, "blocked", runs_root=REPO_ROOT / "data/sample")
    (generated["run_dir"] / "contract.json").write_text('{"contract_version": "1"}')
    failed = workflow.run_validation(generated["run_dir"], "version", runs_root=tmp_path)
    assert failed["manifest"]["status"] == "failed"
    assert "contract.json" in failed["report"]["issues"][0]["detail"]
    (generated["run_dir"] / "uncontracted_features.csv").write_text("target_copy\n1\n")
    extra = workflow.run_validation(generated["run_dir"], "extra", runs_root=tmp_path)
    assert extra["manifest"]["status"] == "failed"
    assert "Unexpected CSV tables" in extra["report"]["issues"][0]["detail"]


def test_m1_evaluator_uses_v2_export_without_changing_feature_or_threshold_contract(
    tmp_path, config
):
    generated = workflow.run_generation(config, "data", runs_root=tmp_path)
    path = generated["run_dir"] / "evaluation_transactions.csv"
    first = run_fraud_evaluation(path, run_id="evaluate-first", runs_root=tmp_path)
    second = run_fraud_evaluation(path, run_id="evaluate-second", runs_root=tmp_path)
    assert first["metrics"] == second["metrics"]
    assert first["manifest"]["test_evaluations"] == 1
    assert first["metrics"]["threshold_source"] == "validation"
    assert sum(map(sum, first["metrics"]["confusion_matrix"])) == first["metrics"]["row_count"]
    # Generation is honest about extreme prevalence; it does not manufacture evaluable partitions.
    one_class = workflow.run_generation(
        replace(config, fraud_prevalence=0), "one", runs_root=tmp_path
    )
    assert one_class["report"]["status"] == "passed"
    with pytest.raises(ValueError, match="both target classes"):
        run_fraud_evaluation(
            one_class["run_dir"] / "evaluation_transactions.csv",
            run_id="one-evaluation",
            runs_root=tmp_path,
        )


def test_new_cli_options_and_legacy_dispatch_remain_separate(tmp_path, monkeypatch, capsys):
    calls = []

    def generate(config, run_id):
        calls.append((config, run_id))
        return {
            "run_dir": tmp_path,
            "manifest": {"status": "completed"},
            "report": {"status": "passed", "issue_count": 0},
        }

    monkeypatch.setattr(workflow, "run_generation", generate)
    cli.main(
        [
            "generate-data-v2",
            "--seed",
            "7",
            "--transactions",
            "123",
            "--fraud-prevalence",
            "0.2",
            "--run-id",
            "cli-v2",
        ]
    )
    assert calls[0][0].seed == 7 and calls[0][0].transactions == 123
    assert calls[0][0].fraud_prevalence == 0.2 and calls[0][1] == "cli-v2"
    assert "Synthetic v2 completed" in capsys.readouterr().out
    monkeypatch.setattr(cli, "generate_all_datasets", lambda: calls.append("legacy") or {})
    cli.main(["generate-data"])
    assert calls[-1] == "legacy"
    with pytest.raises(SystemExit) as rejected:
        cli.main(["generate-data-v2", "--fraud-prevalence", "2"])
    assert rejected.value.code == 1


def test_validation_cli_reports_failure_with_nonzero_exit(tmp_path, monkeypatch, capsys):
    def validate(directory, run_id):
        assert directory == tmp_path and run_id == "bad"
        return {
            "run_dir": tmp_path,
            "manifest": {"status": "failed"},
            "report": {
                "status": "failed",
                "issues": [
                    {
                        "dataset": "transactions",
                        "check": "exact_money",
                        "count": 1,
                        "detail": "Invalid precision",
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow, "run_validation", validate)
    with pytest.raises(SystemExit) as failed:
        cli.main(["validate-data-v2", "--dataset-dir", str(tmp_path), "--run-id", "bad"])
    assert failed.value.code == 1
    assert "transactions.exact_money: 1" in capsys.readouterr().out
