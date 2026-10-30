"""Isolated generation/revalidation runs with actual provenance and failure reports."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from financial_crime_ml.data_generation.v2.contracts import (
    SCHEMAS,
    VERSION,
    SyntheticConfig,
    config_from_mapping,
    contract_description,
)
from financial_crime_ml.data_generation.v2.generator import generate_v2
from financial_crime_ml.data_generation.v2.validation import (
    DataIntegrityError,
    load_v2,
    validate_v2,
)
from financial_crime_ml.evaluation.manifest import (
    execution_provenance,
    sha256_bytes,
    utc_now,
    write_json,
)
from financial_crime_ml.ingestion.load_data import REPO_ROOT

DEFAULT_CONFIG_PATH = REPO_ROOT / "configs/synthetic_v2.yaml"
DEFAULT_RUNS_ROOT = REPO_ROOT / "outputs/runs"


def _start_run(run_id: str | None, runs_root: Path, operation: str) -> tuple[Path, dict]:
    run_id = f"{operation}-{uuid4().hex}" if run_id is None else run_id
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
        raise ValueError("run_id must be 1-80 letters, digits, underscores or hyphens.")
    runs_root = Path(runs_root).resolve()
    for protected in ("data/sample", "data/processed", "outputs/sample", "reports/sample"):
        if runs_root.is_relative_to(REPO_ROOT / protected):
            raise ValueError("v2 outputs cannot be placed under committed samples.")
    directory = runs_root / run_id
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {
        "contract_version": VERSION,
        "operation": operation,
        "run_id": run_id,
        "status": "running",
        "started_at": utc_now(),
    }
    write_json(directory / "manifest.json", manifest)
    return directory, manifest


def _finish_run(directory: Path, manifest: dict, report: dict, error: Exception | None) -> dict:
    write_json(directory / "data_quality_report.json", report)
    manifest.update(
        status="completed" if report["status"] == "passed" and error is None else "failed",
        finished_at=utc_now(),
    )
    if error:
        manifest["error"] = {"type": type(error).__name__, "message": str(error)}
    manifest["artifacts_sha256"] = {
        path.name: sha256_bytes(path.read_bytes())
        for path in sorted(directory.iterdir())
        if path.name != "manifest.json"
    }
    write_json(directory / "manifest.json", manifest)
    return {"run_dir": directory, "manifest": manifest, "report": report}


def _failure_report(error: Exception) -> dict:
    return {
        "contract_version": VERSION,
        "status": "failed",
        "issue_count": 1,
        "issues": [
            {
                "dataset": "bundle",
                "check": "execution_or_load",
                "count": 1,
                "sample_ids": [],
                "detail": f"{type(error).__name__}: {error}",
            }
        ],
    }


def run_generation(
    config: SyntheticConfig, run_id: str | None = None, *, runs_root: Path = DEFAULT_RUNS_ROOT
) -> dict[str, Any]:
    """Validate before and after serialization; write exclusively to a fresh directory."""
    directory, manifest = _start_run(run_id, runs_root, "synthetic-v2")
    report, error = None, None
    try:
        manifest.update(execution_provenance())
        manifest["seed"] = config.seed
        manifest["configuration"] = asdict(config)
        write_json(directory / "configuration.json", asdict(config))
        write_json(directory / "contract.json", contract_description())
        data = generate_v2(config)
        report = validate_v2(data, config)
        if report["status"] != "passed":
            raise DataIntegrityError(report)
        manifest["pre_serialization_checks"] = len(report["checks"])
        for name, frame in data.items():
            frame.to_csv(directory / f"{name}.csv", index=False, lineterminator="\n")
        report = validate_v2(load_v2(directory), config)
        if report["status"] != "passed":
            raise DataIntegrityError(report)
    except Exception as caught:
        error = caught
        report = (
            caught.report if isinstance(caught, DataIntegrityError) else _failure_report(caught)
        )
    return _finish_run(directory, manifest, report, error)


def run_validation(
    dataset_dir: Path, run_id: str | None = None, *, runs_root: Path = DEFAULT_RUNS_ROOT
) -> dict[str, Any]:
    """Revalidate exact consumed bytes without trusting an old report or changing input files."""
    directory, manifest = _start_run(run_id, runs_root, "validate-v2")
    report, error = None, None
    try:
        manifest.update(execution_provenance())
        manifest["dataset_dir"] = str(Path(dataset_dir).resolve())
        unexpected = {path.name for path in Path(dataset_dir).glob("*.csv")} - {
            f"{name}.csv" for name in SCHEMAS
        }
        if unexpected:
            raise ValueError(f"Unexpected CSV tables outside the v2 contract: {sorted(unexpected)}")
        names = [*(f"{name}.csv" for name in SCHEMAS), "configuration.json", "contract.json"]
        contents = {name: (Path(dataset_dir) / name).read_bytes() for name in names}
        manifest["input_artifacts_sha256"] = {
            name: sha256_bytes(content) for name, content in contents.items()
        }
        config = config_from_mapping(json.loads(contents["configuration.json"]))
        if json.loads(contents["contract.json"]) != json.loads(json.dumps(contract_description())):
            raise ValueError("contract.json does not match the supported v2 contract.")
        manifest["configuration"] = asdict(config)
        write_json(directory / "configuration.json", asdict(config))
        report = validate_v2(load_v2(Path(dataset_dir), contents=contents), config)
    except Exception as caught:
        error = caught
        report = _failure_report(caught)
    return _finish_run(directory, manifest, report, error)


def generation_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml generate-data-v2")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--run-id")
    for name in ("seed", "customers", "accounts", "beneficiaries", "devices", "transactions"):
        parser.add_argument(f"--{name}", type=int)
    parser.add_argument("--fraud-prevalence", type=float)
    args = parser.parse_args(argv)
    try:
        config = config_from_mapping(yaml.safe_load(args.config.read_bytes()))
        overrides = {
            key: value
            for key, value in vars(args).items()
            if key not in ("config", "run_id") and value is not None
        }
        result = run_generation(replace(config, **overrides), args.run_id)
    except (ValueError, TypeError, OSError) as error:
        parser.exit(1, f"Generation rejected: {error}\n")
    print(f"Synthetic v2 {result['manifest']['status']}: {result['run_dir']}")
    print(f"Data quality: {result['report']['status']}; issues: {result['report']['issue_count']}")
    if result["manifest"]["status"] != "completed":
        raise SystemExit(1)


def validation_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml validate-data-v2")
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    try:
        result = run_validation(args.dataset_dir, args.run_id)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Validation rejected: {error}\n")
    print(f"Synthetic v2 validation {result['report']['status']}: {result['run_dir']}")
    for issue in result["report"]["issues"]:
        print(f"{issue['dataset']}.{issue['check']}: {issue['count']} — {issue['detail']}")
    if result["manifest"]["status"] != "completed":
        raise SystemExit(1)
