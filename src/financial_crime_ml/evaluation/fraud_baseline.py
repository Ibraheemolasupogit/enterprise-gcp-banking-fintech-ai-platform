"""Training, validation threshold selection, and one held-out test evaluation."""

from __future__ import annotations

import argparse
import io
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd
import yaml
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from financial_crime_ml.evaluation.contracts import (
    CATEGORICAL_FEATURES,
    FEATURE_ALLOWLIST,
    NUMERIC_FEATURES,
    TARGET_COLUMN,
    EvaluationConfig,
    build_transaction_features,
    chronological_split,
    prepare_transactions,
)
from financial_crime_ml.evaluation.manifest import (
    execution_provenance,
    sha256_bytes,
    utc_now,
    write_json,
)
from financial_crime_ml.ingestion.load_data import REPO_ROOT

DEFAULT_INPUT_PATH = REPO_ROOT / "data/sample/transactions.csv"
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs/fraud_evaluation.yaml"
DEFAULT_RUNS_ROOT = REPO_ROOT / "outputs/runs"


def parse_config(content: bytes) -> EvaluationConfig:
    """Load only supported settings from the exact bytes saved with the run."""
    raw = yaml.safe_load(content)
    if not isinstance(raw, dict):
        raise ValueError("Evaluation configuration must be a mapping.")
    unknown = set(raw) - set(EvaluationConfig.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unsupported evaluation settings: {sorted(unknown)}")
    if "threshold_candidates" in raw:
        raw["threshold_candidates"] = tuple(raw["threshold_candidates"])
    return EvaluationConfig(**raw)


def build_model(config: EvaluationConfig) -> Pipeline:
    """Fit all numerical scaling and category vocabularies on training rows only."""
    return Pipeline(
        [
            (
                "preprocessor",
                ColumnTransformer(
                    [
                        ("numeric", StandardScaler(), list(NUMERIC_FEATURES)),
                        (
                            "categorical",
                            OneHotEncoder(handle_unknown="ignore"),
                            list(CATEGORICAL_FEATURES),
                        ),
                    ],
                    remainder="drop",
                ),
            ),
            (
                "classifier",
                LogisticRegression(
                    solver="liblinear",
                    max_iter=1000,
                    class_weight="balanced",
                    random_state=config.seed,
                ),
            ),
        ]
    )


def select_validation_threshold(
    validation_labels: pd.Series,
    validation_probabilities: np.ndarray,
    candidates: tuple[float, ...],
) -> tuple[float, pd.DataFrame]:
    """Maximise validation F1; ties prefer precision, recall, then higher threshold."""
    rows = []
    for threshold in candidates:
        predictions = validation_probabilities >= threshold
        rows.append(
            {
                "threshold": threshold,
                "precision": float(
                    precision_score(validation_labels, predictions, zero_division=0)
                ),
                "recall": float(recall_score(validation_labels, predictions, zero_division=0)),
                "f1_score": float(f1_score(validation_labels, predictions, zero_division=0)),
                "predicted_positive_count": int(predictions.sum()),
            }
        )
    analysis = pd.DataFrame(rows)
    best = analysis.sort_values(
        ["f1_score", "precision", "recall", "threshold"], ascending=False, kind="stable"
    ).iloc[0]
    return float(best["threshold"]), analysis


def evaluate_test(labels: pd.Series, probabilities: np.ndarray, threshold: float) -> dict[str, Any]:
    """Evaluate the fixed decision rule once; never search thresholds on these labels."""
    predictions = probabilities >= threshold
    tn, fp, fn, tp = confusion_matrix(labels, predictions, labels=[0, 1]).ravel()
    return {
        "row_count": len(labels),
        "positive_count": int(labels.sum()),
        "threshold": threshold,
        "threshold_source": "validation",
        "precision": float(precision_score(labels, predictions, zero_division=0)),
        "recall": float(recall_score(labels, predictions, zero_division=0)),
        "f1_score": float(f1_score(labels, predictions, zero_division=0)),
        "roc_auc": float(roc_auc_score(labels, probabilities)),
        "average_precision": float(average_precision_score(labels, probabilities)),
        "confusion_matrix": [[int(tn), int(fp)], [int(fn), int(tp)]],
        "true_negatives": int(tn),
        "false_positives": int(fp),
        "false_negatives": int(fn),
        "true_positives": int(tp),
    }


def _prediction_rows(
    partition: pd.DataFrame, probabilities: np.ndarray, threshold: float
) -> pd.DataFrame:
    output = partition[["transaction_id", "transaction_timestamp", TARGET_COLUMN]].copy()
    output["fraud_probability"] = probabilities
    output["fraud_prediction"] = (probabilities >= threshold).astype(int)
    return output


def run_fraud_evaluation(
    input_path: Path | str = DEFAULT_INPUT_PATH,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    run_id: str | None = None,
    *,
    runs_root: Path = DEFAULT_RUNS_ROOT,
) -> dict[str, Any]:
    """Execute locally into a new run directory; never reuse or overwrite a run."""
    run_id = run_id or f"fraud-{uuid4().hex}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
        raise ValueError("run_id must be 1-80 letters, digits, underscores or hyphens.")
    runs_root = runs_root.resolve()
    for protected in ("data/sample", "data/processed", "outputs/sample", "reports/sample"):
        if runs_root.is_relative_to(REPO_ROOT / protected):
            raise ValueError("Evaluation outputs must not be written under committed samples.")
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "status": "running",
        "started_at": utc_now(),
        "feature_names": list(FEATURE_ALLOWLIST),
        "target": TARGET_COLUMN,
        "test_evaluations": 0,
        "limitations": [
            "Synthetic suspicious labels are not confirmed fraud outcomes.",
            "Row-local transaction observations only; no entity aggregates or external context.",
            "One local holdout evaluation per run; repeated manual runs are not independent tests.",
            "No GCP deployment, model promotion or production validation.",
        ],
    }
    manifest_path = run_dir / "manifest.json"
    write_json(manifest_path, manifest)
    try:
        manifest.update(execution_provenance())
        input_bytes = Path(input_path).read_bytes()
        config_bytes = Path(config_path).read_bytes()
        manifest["inputs"] = {
            "transactions": {
                "path": str(Path(input_path).resolve()),
                "sha256": sha256_bytes(input_bytes),
            },
            "configuration": {
                "path": str(Path(config_path).resolve()),
                "sha256": sha256_bytes(config_bytes),
            },
        }
        (run_dir / "input.csv").write_bytes(input_bytes)
        (run_dir / "config.yaml").write_bytes(config_bytes)
        config = parse_config(config_bytes)
        manifest["configuration"] = asdict(config)
        ordered = prepare_transactions(pd.read_csv(io.BytesIO(input_bytes)))
        partitions = chronological_split(ordered, config)
        manifest["partitions"] = {
            name: {
                "row_count": len(frame),
                "positive_count": int(frame[TARGET_COLUMN].sum()),
                "start": frame["transaction_timestamp"].min().isoformat(),
                "end": frame["transaction_timestamp"].max().isoformat(),
            }
            for name, frame in partitions.items()
        }
        pd.concat(
            [
                frame[["transaction_id", "transaction_timestamp"]].assign(partition=name)
                for name, frame in partitions.items()
            ]
        ).to_csv(run_dir / "partitions.csv", index=False)

        train, validation, test = (partitions[name] for name in ("train", "validation", "test"))
        model = build_model(config)
        model.fit(build_transaction_features(train), train[TARGET_COLUMN])
        validation_probabilities = model.predict_proba(build_transaction_features(validation))[:, 1]
        threshold, analysis = select_validation_threshold(
            validation[TARGET_COLUMN], validation_probabilities, config.threshold_candidates
        )
        manifest["selected_threshold"] = threshold
        manifest["threshold_source"] = "validation"
        analysis.to_csv(run_dir / "validation_thresholds.csv", index=False)
        _prediction_rows(validation, validation_probabilities, threshold).to_csv(
            run_dir / "validation_predictions.csv", index=False
        )

        test_probabilities = model.predict_proba(build_transaction_features(test))[:, 1]
        manifest["test_evaluations"] = 1
        metrics = evaluate_test(test[TARGET_COLUMN], test_probabilities, threshold)
        write_json(run_dir / "metrics.json", metrics)
        _prediction_rows(test, test_probabilities, threshold).to_csv(
            run_dir / "test_predictions.csv", index=False
        )
        preprocessor = model.named_steps["preprocessor"]
        scaler = preprocessor.named_transformers_["numeric"]
        encoder = preprocessor.named_transformers_["categorical"]
        classifier = model.named_steps["classifier"]
        write_json(
            run_dir / "model_parameters.json",
            {
                "model_type": "LogisticRegression",
                "solver": "liblinear",
                "class_weight": "balanced",
                "max_iter": 1000,
                "seed": config.seed,
                "numeric_features": list(NUMERIC_FEATURES),
                "scaler_mean": scaler.mean_.tolist(),
                "scaler_scale": scaler.scale_.tolist(),
                "scaler_training_rows": int(scaler.n_samples_seen_),
                "categorical_features": list(CATEGORICAL_FEATURES),
                "categories": [values.tolist() for values in encoder.categories_],
                "transformed_features": preprocessor.get_feature_names_out().tolist(),
                "coefficients": classifier.coef_.tolist(),
                "intercept": classifier.intercept_.tolist(),
                "classes": classifier.classes_.tolist(),
            },
        )
        manifest["artifacts_sha256"] = {
            path.name: sha256_bytes(path.read_bytes())
            for path in sorted(run_dir.iterdir())
            if path != manifest_path
        }
        manifest.update(status="completed", completed_at=utc_now())
        write_json(manifest_path, manifest)
        return {"run_dir": run_dir, "manifest": manifest, "metrics": metrics}
    except Exception as error:
        manifest.update(
            status="failed",
            failed_at=utc_now(),
            error={"type": type(error).__name__, "message": str(error)},
        )
        write_json(manifest_path, manifest)
        raise


def evaluation_main(argv: list[str]) -> None:
    """Argument parsing isolated from the compatible legacy CLI."""
    parser = argparse.ArgumentParser(prog="financial-crime-ml evaluate-fraud")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    result = run_fraud_evaluation(args.input, args.config, args.run_id)
    print(f"Fraud evaluation completed: {result['run_dir']}")
    print(f"Test metrics: {result['metrics']}")
