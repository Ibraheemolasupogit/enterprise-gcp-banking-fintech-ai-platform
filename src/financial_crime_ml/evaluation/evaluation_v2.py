"""M9A fraud evaluation v2: rolling-origin validation, champion/challenger, sealed test.

Flow (one run, one new directory under outputs/runs):
1. Load a verified synthetic v2 dataset. Features come only from ``transactions.csv``;
   labels only from ``outcomes.csv`` (``transaction_id``, ``is_suspicious``; episode and
   scenario columns are never read).
2. Point-in-time features for every row (``behavioural.py``).
3. Chronological split: development (earliest) and a final test window (latest).
   Development is cut into ``folds + 1`` time blocks; fold k trains on blocks 0..k-1
   (minus an embargo before the validation start) and validates on block k.
4. Logistic regression (champion) and HistGradientBoosting (challenger) per fold.
   Selection and the operating threshold use validation predictions only.
5. The decision (model, threshold, hashes) is frozen and written. Only then are the test
   labels read, once, by ``SealedTestWindow``.
The deployed model is the last fold's model: its threshold is chosen on data it never saw,
and nothing is refit after the threshold is frozen.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd
import yaml
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler

from financial_crime_ml.data_generation.v2.contracts import config_from_mapping
from financial_crime_ml.data_generation.v2.validation import load_v2, validate_v2
from financial_crime_ml.evaluation import metrics_v2 as mv
from financial_crime_ml.evaluation.behavioural import (
    FEATURE_VERSION,
    MODEL_CATEGORICAL_FEATURES,
    MODEL_FEATURES,
    MODEL_NUMERIC_FEATURES,
    build_behavioural_features,
    contract,
)
from financial_crime_ml.evaluation.contracts import NUMERIC_FEATURES as M1_NUMERIC_FEATURES
from financial_crime_ml.evaluation.manifest import (
    execution_provenance,
    sha256_bytes,
    utc_now,
    write_json,
)
from financial_crime_ml.ingestion.load_data import REPO_ROOT

DEFAULT_CONFIG_PATH = REPO_ROOT / "configs/fraud_evaluation_v2.yaml"
DEFAULT_RUNS_ROOT = REPO_ROOT / "outputs/runs"
MODELS = ("logistic_regression", "hist_gradient_boosting")
CHAMPION, CHALLENGER = MODELS
# Validation-only reference (never selectable, never scored on test): the M1 row-local
# feature set, to show what the point-in-time behavioural features add.
REFERENCE = "m1_row_local_logistic_regression"
EXACT_FLOAT = "%.17g"  # CSV scores round-trip exactly (M9B/M9C parity)
CLOUD_PORTABLE = {"logistic_regression": True, "hist_gradient_boosting": False}


@dataclass(frozen=True)
class EvaluationV2Config:
    seed: int = 42
    test_fraction: float = 0.2
    folds: int = 3
    embargo_seconds: int = 3600
    operating_capacity: float = 0.05
    capacities: tuple[float, ...] = (0.01, 0.02, 0.05, 0.10)
    bootstrap_resamples: int = 1000
    confidence: float = 0.95
    ece_bins: int = 10
    segment_min_rows: int = 200
    segment_min_class: int = 20
    challenger_ece_tolerance: float = 0.02
    logistic_c: float = 1.0
    hgb_max_iter: int = 200
    hgb_learning_rate: float = 0.05
    hgb_max_leaf_nodes: int = 15
    hgb_l2_regularization: float = 1.0

    def __post_init__(self) -> None:
        if type(self.seed) is not int or not 0 <= self.seed < 2**32:
            raise ValueError("seed must be an integer in [0, 2**32)")
        if not 0 < self.test_fraction < 0.5:
            raise ValueError("test_fraction must be in (0, 0.5)")
        if type(self.folds) is not int or self.folds < 3:
            raise ValueError("folds must be an integer >= 3")
        if type(self.embargo_seconds) is not int or self.embargo_seconds < 0:
            raise ValueError("embargo_seconds must be a nonnegative integer")
        caps = self.capacities
        if not caps or tuple(sorted(set(caps))) != caps or any(not 0 < c <= 1 for c in caps):
            raise ValueError("capacities must be sorted, unique and in (0, 1]")
        if self.operating_capacity not in caps:
            raise ValueError("operating_capacity must be one of capacities")
        if self.bootstrap_resamples < 20 or not 0.5 < self.confidence < 1:
            raise ValueError("need >= 20 bootstrap resamples and confidence in (0.5, 1)")
        if self.segment_min_rows < 1 or self.segment_min_class < 1 or self.ece_bins < 2:
            raise ValueError("invalid segment or ECE settings")


def parse_config(content: bytes) -> EvaluationV2Config:
    raw = yaml.safe_load(content)
    if not isinstance(raw, dict):
        raise ValueError("Evaluation configuration must be a mapping.")
    unknown = set(raw) - set(EvaluationV2Config.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unsupported evaluation settings: {sorted(unknown)}")
    if "capacities" in raw:
        raw["capacities"] = tuple(raw["capacities"])
    return EvaluationV2Config(**raw)


# --------------------------------------------------------------------------- data
def load_dataset(directory: Path) -> tuple[pd.DataFrame, pd.Series, dict[str, str]]:
    """Validate the v2 dataset, then return observations and labels separately."""
    directory = Path(directory).resolve()
    config = config_from_mapping(json.loads((directory / "configuration.json").read_text()))
    report = validate_v2(load_v2(directory), config)
    if report["status"] != "passed":
        raise ValueError("v2 dataset failed validation")
    raw = {name: (directory / f"{name}.csv").read_bytes()
           for name in ("transactions", "outcomes")}
    observations = pd.read_csv(io.BytesIO(raw["transactions"]), dtype=str)
    outcomes = pd.read_csv(io.BytesIO(raw["outcomes"]), dtype=str,
                           usecols=["transaction_id", "is_suspicious"])
    if not outcomes["is_suspicious"].isin(["0", "1"]).all():
        raise ValueError("is_suspicious must be 0/1")
    labels = outcomes.set_index("transaction_id")["is_suspicious"].astype(int)
    if set(labels.index) != set(observations["transaction_id"]) or labels.index.duplicated().any():
        raise ValueError("outcomes must match transactions one-to-one")
    hashes = {f"{name}.csv": sha256_bytes(content) for name, content in raw.items()}
    return observations, labels, hashes


# --------------------------------------------------------------------------- partitions
def _cut(times: pd.Series, position: int) -> int:
    """Move a cut forward so one instant never spans two blocks."""
    while 0 < position < len(times) and times.iloc[position] == times.iloc[position - 1]:
        position += 1
    return position


def make_partitions(features: pd.DataFrame, config: EvaluationV2Config) -> dict[str, Any]:
    times = features["transaction_timestamp"]
    if not times.is_monotonic_increasing:
        raise ValueError("features must be chronologically ordered")
    n = len(features)
    test_start = _cut(times, int(n * (1 - config.test_fraction)))
    bounds = [0]
    for k in range(1, config.folds + 1):
        bounds.append(_cut(times, max(bounds[-1] + 1, test_start * k // (config.folds + 1))))
    bounds.append(test_start)
    folds = []
    embargo = pd.Timedelta(seconds=config.embargo_seconds)
    for k in range(1, config.folds + 1):
        val_start, val_end = bounds[k], bounds[k + 1]
        train_index = np.arange(0, val_start)
        cutoff = times.iloc[val_start] - embargo
        train_index = train_index[(times.iloc[train_index] < cutoff).to_numpy()]
        folds.append({"fold": k, "train": train_index,
                      "validation": np.arange(val_start, val_end)})
    test = np.arange(test_start, n)
    if any(len(f["train"]) == 0 or len(f["validation"]) == 0 for f in folds) or not len(test):
        raise ValueError("empty fold or test window; need more data")
    return {"folds": folds, "test": test, "bounds": bounds + [n]}


# --------------------------------------------------------------------------- models
def build_model(name: str, config: EvaluationV2Config) -> Pipeline:
    numeric, categorical = list(MODEL_NUMERIC_FEATURES), list(MODEL_CATEGORICAL_FEATURES)
    if name == REFERENCE:
        numeric = list(M1_NUMERIC_FEATURES)
    if name in (CHAMPION, REFERENCE):
        return Pipeline([
            ("preprocessor", ColumnTransformer([
                ("numeric", StandardScaler(), numeric),
                ("categorical", OneHotEncoder(handle_unknown="ignore"), categorical)],
                remainder="drop")),
            ("classifier", LogisticRegression(solver="liblinear", C=config.logistic_c,
                                              max_iter=1000, random_state=config.seed))])
    if name == CHALLENGER:
        return Pipeline([
            ("preprocessor", ColumnTransformer([
                ("numeric", "passthrough", numeric),
                ("categorical", OrdinalEncoder(handle_unknown="use_encoded_value",
                                               unknown_value=-1), categorical)],
                remainder="drop")),
            ("classifier", HistGradientBoostingClassifier(
                max_iter=config.hgb_max_iter, learning_rate=config.hgb_learning_rate,
                max_leaf_nodes=config.hgb_max_leaf_nodes,
                l2_regularization=config.hgb_l2_regularization, early_stopping=False,
                categorical_features=list(range(len(numeric), len(numeric) +
                                                len(categorical))),
                random_state=config.seed))])
    raise ValueError(f"unknown model {name}")


def capacity_threshold(scores: np.ndarray, ids: np.ndarray, capacity: float) -> float:
    """The score of the last alerted row at this capacity, from validation only."""
    alerts = mv.capacity_alerts(scores, ids, capacity)
    return float(scores[alerts].min())


def promote_challenger(champion: dict, challenger: dict, config: EvaluationV2Config) -> dict:
    """Pre-registered rule on pooled out-of-fold validation predictions only."""
    lower = challenger["bootstrap"]["intervals"]["pr_auc"][0]
    better = lower > champion["metrics"]["pr_auc"]
    calibrated = challenger["metrics"]["ece"] <= (champion["metrics"]["ece"]
                                                   + config.challenger_ece_tolerance)
    return {"rule": "challenger PR-AUC CI lower bound > champion PR-AUC point estimate, and "
                    "challenger ECE <= champion ECE + tolerance (pooled out-of-fold validation)",
            "challenger_pr_auc_ci_lower": lower,
            "champion_pr_auc": champion["metrics"]["pr_auc"],
            "challenger_ece": challenger["metrics"]["ece"],
            "champion_ece": champion["metrics"]["ece"],
            "ece_tolerance": config.challenger_ece_tolerance,
            "pr_auc_condition": bool(better), "ece_condition": bool(calibrated),
            "selected": CHALLENGER if better and calibrated else CHAMPION}


# --------------------------------------------------------------------------- sealed test
class SealedTestWindow:
    """Holds test labels; releases them once, only against a frozen selection record."""

    def __init__(self, labels: pd.Series):
        self._labels = labels
        self.evaluations = 0

    def evaluate(self, frozen: dict, frozen_sha256: str, ids: np.ndarray,
                 scores: np.ndarray, config: EvaluationV2Config,
                 days: np.ndarray) -> tuple[dict, np.ndarray]:
        if self.evaluations:
            raise RuntimeError("the test window has already been evaluated")
        if _digest(frozen) != frozen_sha256 or "threshold" not in frozen:
            raise RuntimeError("test evaluation requires the frozen selection record")
        self.evaluations += 1
        y = self._labels.loc[ids].to_numpy(dtype=int)
        metrics = mv.point_metrics(y, scores, ids, capacities=config.capacities,
                                   threshold=frozen["threshold"], bins=config.ece_bins)
        metrics["bootstrap"] = mv.bootstrap(
            y, scores, ids, days, capacities=config.capacities,
            threshold=frozen["threshold"], bins=config.ece_bins,
            resamples=config.bootstrap_resamples, confidence=config.confidence,
            seed=config.seed + 1)
        return metrics, y


def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _days(frame: pd.DataFrame) -> np.ndarray:
    return frame["transaction_timestamp"].dt.strftime("%Y-%m-%d").to_numpy()


def _lr_parameters(model: Pipeline, threshold: float) -> dict:
    pre = model.named_steps["preprocessor"]
    scaler, encoder = pre.named_transformers_["numeric"], pre.named_transformers_["categorical"]
    clf = model.named_steps["classifier"]
    return {"model_type": "LogisticRegression", "solver": "liblinear", "class_weight": None,
            "feature_version": FEATURE_VERSION,
            "numeric_features": list(MODEL_NUMERIC_FEATURES),
            "scaler_mean": scaler.mean_.tolist(), "scaler_scale": scaler.scale_.tolist(),
            "categorical_features": list(MODEL_CATEGORICAL_FEATURES),
            "categories": [c.tolist() for c in encoder.categories_],
            "transformed_features": pre.get_feature_names_out().tolist(),
            "coefficients": clf.coef_[0].tolist(), "intercept": float(clf.intercept_[0]),
            "threshold": threshold, "score": "sigmoid(intercept + coefficients . x)"}


# --------------------------------------------------------------------------- run
def run_evaluation_v2(dataset_dir: Path | str, config_path: Path | str = DEFAULT_CONFIG_PATH,
                      run_id: str | None = None, *,
                      runs_root: Path = DEFAULT_RUNS_ROOT) -> dict[str, Any]:
    run_id = run_id or f"fraud-v2-{uuid4().hex}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
        raise ValueError("run_id must be 1-80 letters, digits, underscores or hyphens.")
    runs_root = Path(runs_root).resolve()
    for protected in ("data/sample", "data/processed", "outputs/sample", "reports/sample"):
        if runs_root.is_relative_to(REPO_ROOT / protected):
            raise ValueError("Evaluation outputs must not be written under committed samples.")
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "schema_version": "2.0", "milestone": "M9A", "run_id": run_id, "status": "running",
        "started_at": utc_now(), "feature_version": FEATURE_VERSION, "test_evaluations": 0,
        "limitations": [
            "Synthetic labels from the v2 generator, not confirmed fraud; benign controls "
            "deliberately share the fraud mechanisms.",
            "One synthetic dataset and seed; metrics are engineering evidence, not "
            "detection performance.",
            "Day-block bootstrap intervals; folds share history, so fold metrics are not "
            "independent.",
            "No GCP scoring, deployment, promotion or production validation.",
        ]}
    manifest_path = run_dir / "manifest.json"
    write_json(manifest_path, manifest)
    try:
        manifest.update(execution_provenance())
        config_bytes = Path(config_path).read_bytes()
        (run_dir / "config.yaml").write_bytes(config_bytes)
        config = parse_config(config_bytes)
        manifest["configuration"] = asdict(config)
        observations, labels, data_hashes = load_dataset(Path(dataset_dir))
        manifest["inputs"] = {"dataset_dir": str(Path(dataset_dir).resolve()),
                              "sha256": data_hashes,
                              "configuration_sha256": sha256_bytes(config_bytes)}
        feature_contract = contract()
        write_json(run_dir / "feature_contract.json", feature_contract)
        features = build_behavioural_features(observations)
        parts = make_partitions(features, config)
        test_ids = features["transaction_id"].to_numpy()[parts["test"]]
        dev_labels = labels.drop(index=test_ids)          # test labels sealed below
        sealed = SealedTestWindow(labels.loc[test_ids])
        del labels
        assignment = np.full(len(features), "unused_embargo", dtype=object)
        for fold in parts["folds"]:
            assignment[fold["validation"]] = f"validation_{fold['fold']}"
        assignment[parts["folds"][0]["train"]] = "train_only"
        assignment[parts["test"]] = "test"
        features.assign(partition=assignment)[["transaction_id", "transaction_timestamp",
                                               "partition"]].to_csv(
            run_dir / "partitions.csv", index=False)

        fold_rows, fold_models, oof = [], {}, []
        for fold in parts["folds"]:
            train, val = features.iloc[fold["train"]], features.iloc[fold["validation"]]
            y_train = dev_labels.loc[train["transaction_id"]].to_numpy(dtype=int)
            y_val = dev_labels.loc[val["transaction_id"]].to_numpy(dtype=int)
            for name in (*MODELS, REFERENCE):
                if y_train.min() == y_train.max() or y_val.min() == y_val.max():
                    raise ValueError(f"fold {fold['fold']} lacks a class")
                model = build_model(name, config).fit(train[list(MODEL_FEATURES)], y_train)
                scores = model.predict_proba(val[list(MODEL_FEATURES)])[:, 1]
                fold_models[(name, fold["fold"])] = model
                metrics = mv.point_metrics(y_val, scores, val["transaction_id"].to_numpy(),
                                           capacities=config.capacities, threshold=None,
                                           bins=config.ece_bins)
                fold_rows.append({"fold": fold["fold"], "model": name,
                                  "train_rows": len(train), "train_end":
                                  train["transaction_timestamp"].max().isoformat(),
                                  "validation_start":
                                  val["transaction_timestamp"].min().isoformat(),
                                  "validation_end":
                                  val["transaction_timestamp"].max().isoformat(),
                                  "metrics": metrics})
                oof.append(pd.DataFrame({"transaction_id": val["transaction_id"],
                                         "transaction_timestamp": val["transaction_timestamp"],
                                         "fold": fold["fold"], "model": name, "label": y_val,
                                         "score": scores}))
        write_json(run_dir / "fold_metrics.json", fold_rows)
        oof_frame = pd.concat(oof, ignore_index=True)
        oof_frame.to_csv(run_dir / "validation_predictions.csv", index=False,
                         float_format=EXACT_FLOAT)

        pooled = {}
        for name in (*MODELS, REFERENCE):
            part = oof_frame[oof_frame["model"] == name]
            y, p = part["label"].to_numpy(dtype=int), part["score"].to_numpy()
            ids = part["transaction_id"].to_numpy()
            pooled[name] = {
                "metrics": mv.point_metrics(y, p, ids, capacities=config.capacities,
                                            threshold=None, bins=config.ece_bins),
                "bootstrap": mv.bootstrap(y, p, ids, _days(part), capacities=config.capacities,
                                          threshold=None, bins=config.ece_bins,
                                          resamples=config.bootstrap_resamples,
                                          confidence=config.confidence, seed=config.seed)}
        decision = promote_challenger(pooled[CHAMPION], pooled[CHALLENGER], config)
        decision["reference_pooled_pr_auc"] = pooled[REFERENCE]["metrics"]["pr_auc"]
        selected, last = decision["selected"], config.folds
        last_val = oof_frame[(oof_frame["model"] == selected) & (oof_frame["fold"] == last)]
        threshold = capacity_threshold(last_val["score"].to_numpy(),
                                       last_val["transaction_id"].to_numpy(),
                                       config.operating_capacity)
        frozen = {"selected_model": selected, "cloud_portable": CLOUD_PORTABLE[selected],
                  "deployed_model": f"{selected} trained in fold {last}",
                  "threshold": threshold, "threshold_source":
                  f"fold {last} validation, capacity {config.operating_capacity}",
                  "operating_capacity": config.operating_capacity, "decision": decision,
                  "pooled_validation": pooled, "feature_contract_sha256":
                  feature_contract["contract_sha256"],
                  "validation_predictions_sha256":
                  sha256_bytes((run_dir / "validation_predictions.csv").read_bytes()),
                  "frozen_at": utc_now(), "test_labels_read": False}
        frozen_sha = _digest(frozen)
        write_json(run_dir / "selection.json", {**frozen, "selection_sha256": frozen_sha})
        manifest["selection_sha256"] = frozen_sha

        # ---- only now: score and evaluate the untouched test window, once -------------
        test = features.iloc[parts["test"]]
        model = fold_models[(selected, last)]
        scores = model.predict_proba(test[list(MODEL_FEATURES)])[:, 1]
        test_metrics, y_test = sealed.evaluate(frozen, frozen_sha, test_ids, scores, config,
                                               _days(test))
        manifest["test_evaluations"] = sealed.evaluations
        frame = pd.DataFrame({"transaction_id": test_ids, "label": y_test, "score": scores,
                              "currency": test["currency"].to_numpy(),
                              "channel": test["channel"].to_numpy(),
                              "corridor": (test["origin_country"] + "->" +
                                           test["destination_country"]).to_numpy(),
                              "corridor_type": np.where(
                                  test["origin_country"] == test["destination_country"],
                                  "domestic", "cross_border")})
        test_metrics["segments"] = {
            column: mv.segment_metrics(frame, column=column, threshold=threshold,
                                       bins=config.ece_bins, min_rows=config.segment_min_rows,
                                       min_class=config.segment_min_class)
            for column in ("currency", "channel", "corridor", "corridor_type")}
        test_metrics["segment_rule"] = {"min_rows": config.segment_min_rows,
                                        "min_positives": config.segment_min_class,
                                        "min_negatives": config.segment_min_class}
        write_json(run_dir / "test_metrics.json", test_metrics)
        frame.assign(alert=(scores >= threshold).astype(int)).to_csv(
            run_dir / "test_predictions.csv", index=False, float_format=EXACT_FLOAT)
        if selected == CHAMPION:
            write_json(run_dir / "model_parameters.json", _lr_parameters(model, threshold))
        else:
            write_json(run_dir / "model_parameters.json", {
                "model_type": "HistGradientBoostingClassifier", "cloud_portable": False,
                "feature_version": FEATURE_VERSION, "threshold": threshold,
                "n_iter": int(model.named_steps["classifier"].n_iter_)})
        manifest["artifacts_sha256"] = {p.name: sha256_bytes(p.read_bytes())
                                        for p in sorted(run_dir.iterdir()) if p != manifest_path}
        manifest.update(status="completed", completed_at=utc_now())
        write_json(manifest_path, manifest)
        return {"run_dir": run_dir, "manifest": manifest, "selection": frozen,
                "test_metrics": test_metrics}
    except Exception as error:
        manifest.update(status="failed", failed_at=utc_now(),
                        error={"type": type(error).__name__, "message": str(error)})
        write_json(manifest_path, manifest)
        raise


def evaluation_v2_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml evaluate-fraud-v2")
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    result = run_evaluation_v2(args.dataset_dir, args.config, args.run_id)
    selection, test = result["selection"], result["test_metrics"]
    print(f"Fraud evaluation v2 completed: {result['run_dir']}")
    print(json.dumps({"selected_model": selection["selected_model"],
                      "threshold": selection["threshold"],
                      "test_pr_auc": test["pr_auc"],
                      "test_pr_auc_ci": test["bootstrap"]["intervals"]["pr_auc"],
                      "test_at_threshold": test["at_threshold"]}, indent=1))

