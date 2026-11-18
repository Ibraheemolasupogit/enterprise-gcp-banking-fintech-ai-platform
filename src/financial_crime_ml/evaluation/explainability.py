"""M9B explainability for the frozen M9A champion (no retraining of the champion).

* Contributions: from the frozen ``model_parameters.json`` only. For each decision,
  ``logit = intercept + sum_j coef_j * (x_j - mean_j) / scale_j + sum_f coef_{f=value}``;
  one contribution per model input feature (numeric: its scaled term; categorical: the
  coefficient of the active category, 0 for a category unseen in training). Checked against
  the original model's stored probabilities.
* Reason codes: the top three risk-increasing contributions of each alert, ranked by
  contribution descending, ties by the fixed feature-contract order.
* Permutation importance and rank stability: on validation folds only, using replicas of
  M9A's fold models that are accepted only if they reproduce M9A's stored validation
  predictions exactly (the deployed champion is never refit or replaced).
"""

from __future__ import annotations

import io
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.inspection import permutation_importance

from financial_crime_ml.evaluation import evaluation_v2 as ev
from financial_crime_ml.evaluation.behavioural import (
    FEATURE_VERSION,
    MODEL_CATEGORICAL_FEATURES,
    MODEL_FEATURES,
    MODEL_NUMERIC_FEATURES,
    build_behavioural_features,
    contract,
)
from financial_crime_ml.evaluation.manifest import sha256_bytes

REASON_VERSION = "m9b-reasons-1.0.0"
RECONSTRUCTION_TOLERANCE = 1e-9
PROBABILITY_TOLERANCE = 1e-12
NO_FURTHER_REASON = ("R00", "No further risk-increasing factor")
# Stable, human-readable mapping (code, text). Order of MODEL_FEATURES breaks ties.
REASONS: dict[str, tuple[str, str]] = {
    "transaction_amount_log": ("R01", "Transaction amount"),
    "transaction_hour": ("R02", "Time of day (UTC hour)"),
    "transaction_day_of_week": ("R03", "Day of week"),
    "is_cross_border_transaction": ("R04", "Cross-border payment"),
    "acct_txn_count_10m": ("R10", "Payments from this account in the previous 10 minutes"),
    "acct_txn_count_1h": ("R11", "Payments from this account in the previous hour"),
    "acct_txn_count_24h": ("R12", "Payments from this account in the previous 24 hours"),
    "cust_txn_count_24h": ("R13", "Payments by this customer in the previous 24 hours"),
    "acct_same_amount_count_24h": ("R14", "Repeated identical amounts in the previous 24 hours"),
    "acct_history_count_30d": ("R15", "Account activity in the previous 30 days"),
    "acct_amount_log_ratio_30d": ("R16", "Amount compared with this account's 30-day average"),
    "acct_seconds_since_prev_log": ("R17", "Time since this account's previous payment"),
    "acct_has_prev": ("R18", "Account has earlier payment history"),
    "acct_new_beneficiary": ("R19", "First payment from this account to this beneficiary"),
    "cust_new_device": ("R20", "First use of this device by the customer"),
    "is_round_amount": ("R21", "Round payment amount"),
    "currency": ("R30", "Payment currency"),
    "transaction_type": ("R31", "Payment type"),
    "merchant_category": ("R32", "Merchant category"),
    "origin_country": ("R33", "Origin country"),
    "destination_country": ("R34", "Destination country"),
    "channel": ("R35", "Payment channel"),
}


class ExplainabilityError(ValueError):
    """An M9A artifact, reproduction or reconstruction check failed (fail closed)."""


# --------------------------------------------------------------------------- M9A inputs
def load_m9a(run_dir: Path) -> dict[str, Any]:
    """Read a completed M9A run, verifying every artifact hash and the frozen selection."""
    run_dir = Path(run_dir).resolve()
    manifest = json.loads((run_dir / "manifest.json").read_bytes())
    if manifest.get("milestone") != "M9A" or manifest.get("status") != "completed" or \
            manifest.get("test_evaluations") != 1:
        raise ExplainabilityError("require a completed M9A run with exactly one test evaluation")
    contents = {}
    for name, digest in manifest["artifacts_sha256"].items():
        content = (run_dir / name).read_bytes()
        if sha256_bytes(content) != digest:
            raise ExplainabilityError(f"M9A artifact hash mismatch: {name}")
        contents[name] = content
    selection = json.loads(contents["selection.json"])
    frozen = {k: v for k, v in selection.items() if k != "selection_sha256"}
    if not (ev._digest(frozen) == selection["selection_sha256"] == manifest["selection_sha256"]):
        raise ExplainabilityError("selection record digest mismatch")
    params = json.loads(contents["model_parameters.json"])
    if selection["selected_model"] != ev.CHAMPION or params.get("model_type") != \
            "LogisticRegression":
        raise ExplainabilityError("M9B explains a logistic-regression champion only")
    feature_contract = json.loads(contents["feature_contract.json"])
    if feature_contract["contract_sha256"] != contract()["contract_sha256"] or \
            params["feature_version"] != FEATURE_VERSION:
        raise ExplainabilityError("feature contract differs from the code: features would "
                                  "not be the ones the champion was trained on")
    return {"run_dir": run_dir, "manifest": manifest, "selection": selection,
            "params": params, "contents": contents,
            "test_metrics": json.loads(contents["test_metrics.json"]),
            "fold_metrics": json.loads(contents["fold_metrics.json"]),
            "feature_contract": feature_contract}


def _csv(content: bytes) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(content), float_precision="round_trip",
                       dtype={"transaction_id": str})


def load_features(m9a: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Recompute features from the hashed M9A dataset; labels are not read here."""
    inputs = m9a["manifest"]["inputs"]
    directory = Path(inputs["dataset_dir"])
    content = (directory / "transactions.csv").read_bytes()
    if sha256_bytes(content) != inputs["sha256"]["transactions.csv"]:
        raise ExplainabilityError("dataset transactions.csv differs from the M9A run")
    features = build_behavioural_features(pd.read_csv(io.BytesIO(content), dtype=str))
    partitions = _csv(m9a["contents"]["partitions.csv"])
    if partitions["transaction_id"].tolist() != features["transaction_id"].tolist():
        raise ExplainabilityError("recomputed row order differs from M9A partitions")
    return features, partitions


# --------------------------------------------------------------------------- contributions
def contributions(features: pd.DataFrame, params: dict) -> pd.DataFrame:
    """One column per model feature plus intercept and logit (sum), for every row."""
    if list(params["numeric_features"]) != list(MODEL_NUMERIC_FEATURES) or \
            list(params["categorical_features"]) != list(MODEL_CATEGORICAL_FEATURES):
        raise ExplainabilityError("model parameters do not match the feature contract")
    coef = np.asarray(params["coefficients"], dtype=float)
    names = params["transformed_features"]
    n_numeric = len(MODEL_NUMERIC_FEATURES)
    if len(coef) != len(names) or len(params["scaler_mean"]) != n_numeric:
        raise ExplainabilityError("model parameter shapes are inconsistent")
    out = pd.DataFrame({"transaction_id": features["transaction_id"].to_numpy()})
    out["intercept"] = float(params["intercept"])
    for j, name in enumerate(MODEL_NUMERIC_FEATURES):
        x = features[name].to_numpy(dtype=float)
        out[name] = coef[j] * ((x - params["scaler_mean"][j]) / params["scaler_scale"][j])
    offset = n_numeric
    for name, categories in zip(MODEL_CATEGORICAL_FEATURES, params["categories"], strict=True):
        lookup = {value: coef[offset + k] for k, value in enumerate(categories)}
        out[name] = features[name].map(lookup).fillna(0.0).astype(float)  # unseen -> 0
        offset += len(categories)
    if offset != len(coef):
        raise ExplainabilityError("category vocabularies do not cover the coefficients")
    parts = out[["intercept", *MODEL_FEATURES]].to_numpy()
    out["logit"] = np.array([math.fsum(row) for row in parts])
    return out


def design_logit(features: pd.DataFrame, params: dict) -> np.ndarray:
    """Independent computation: explicit transformed design matrix times coefficients."""
    columns = [((features[n].to_numpy(dtype=float) - m) / s) for n, m, s in
               zip(MODEL_NUMERIC_FEATURES, params["scaler_mean"], params["scaler_scale"],
                   strict=True)]
    for name, categories in zip(MODEL_CATEGORICAL_FEATURES, params["categories"], strict=True):
        values = features[name].to_numpy()
        columns += [(values == c).astype(float) for c in categories]
    return np.column_stack(columns) @ np.asarray(params["coefficients"]) + params["intercept"]


def stored_logit(p: np.ndarray) -> np.ndarray:
    p = np.asarray(p, dtype=float)
    if ((p <= 0) | (p >= 1)).any():
        raise ExplainabilityError("stored probabilities must be strictly inside (0, 1)")
    return np.log(p) - np.log1p(-p)


def reconstruction_report(contrib: pd.DataFrame, features: pd.DataFrame, params: dict,
                          stored: pd.DataFrame, label: str) -> dict[str, Any]:
    """Contributions vs the independent design product and vs the original model output."""
    merged = contrib.merge(stored[["transaction_id", "score"]], on="transaction_id",
                           validate="one_to_one")
    if len(merged) != len(stored):
        raise ExplainabilityError(f"{label}: rows differ from the stored predictions")
    rows = features.set_index("transaction_id").loc[merged["transaction_id"]].reset_index()
    sum_vs_design = float(np.max(np.abs(merged["logit"] - design_logit(rows, params))))
    sum_vs_model = float(np.max(np.abs(merged["logit"].to_numpy()
                                       - stored_logit(merged["score"].to_numpy()))))
    prob = 1.0 / (1.0 + np.exp(-merged["logit"].to_numpy()))
    prob_diff = float(np.max(np.abs(prob - merged["score"].to_numpy())))
    report = {"rows": len(merged), "max_abs_logit_vs_design": sum_vs_design,
              "max_abs_logit_vs_original_model": sum_vs_model,
              "max_abs_probability_vs_original_model": prob_diff,
              "tolerance_logit": RECONSTRUCTION_TOLERANCE,
              "tolerance_probability": PROBABILITY_TOLERANCE}
    if max(sum_vs_design, sum_vs_model) > RECONSTRUCTION_TOLERANCE or \
            prob_diff > PROBABILITY_TOLERANCE:
        raise ExplainabilityError(f"{label}: contributions do not reconstruct the model "
                                  f"({report})")
    return report


# --------------------------------------------------------------------------- reason codes
def reason_codes(contrib: pd.DataFrame, features: pd.DataFrame, alerts: pd.DataFrame,
                 top: int = 3) -> pd.DataFrame:
    """``alerts``: transaction_id, score. Exactly ``top`` codes per alert, R00-padded."""
    order = {name: i for i, name in enumerate(MODEL_FEATURES)}
    by_id = contrib.set_index("transaction_id")
    values = features.set_index("transaction_id")
    rows = []
    for tid, score in alerts[["transaction_id", "score"]].itertuples(index=False):
        c = by_id.loc[tid]
        ranked = sorted(((float(c[f]), f) for f in MODEL_FEATURES if c[f] > 0),
                        key=lambda item: (-item[0], order[item[1]]))
        for rank in range(1, top + 1):
            if rank <= len(ranked):
                value, feature = ranked[rank - 1]
                code, text = REASONS[feature]
                raw = values.loc[tid, feature]
                rows.append({"transaction_id": tid, "score": score, "rank": rank,
                             "code": code, "feature": feature, "value": raw,
                             "contribution": value, "text": text})
            else:
                rows.append({"transaction_id": tid, "score": score, "rank": rank,
                             "code": NO_FURTHER_REASON[0], "feature": "", "value": "",
                             "contribution": 0.0, "text": NO_FURTHER_REASON[1]})
    return pd.DataFrame(rows, columns=["transaction_id", "score", "rank", "code", "feature",
                                       "value", "contribution", "text"])


# --------------------------------------------------------------------------- fold replicas
def fold_replicas(m9a: dict, features: pd.DataFrame, partitions: pd.DataFrame,
                  dev_labels: pd.Series) -> dict[tuple[str, int], Any]:
    """Refit M9A's fold models with M9A's code, config and rows; accept them only if they
    reproduce M9A's stored validation predictions exactly. Never the deployed champion."""
    config = ev.parse_config(m9a["contents"]["config.yaml"])
    parts = ev.make_partitions(features, config)
    oof = _csv(m9a["contents"]["validation_predictions.csv"])
    replicas = {}
    for fold in parts["folds"]:
        train, val = features.iloc[fold["train"]], features.iloc[fold["validation"]]
        expected = partitions.iloc[fold["validation"]]["partition"].unique().tolist()
        if expected != [f"validation_{fold['fold']}"]:
            raise ExplainabilityError("fold boundaries differ from M9A")
        y_train = dev_labels.loc[train["transaction_id"]].to_numpy(dtype=int)
        for name in ev.MODELS:
            model = ev.build_model(name, config).fit(train[list(MODEL_FEATURES)], y_train)
            got = model.predict_proba(val[list(MODEL_FEATURES)])[:, 1]
            want = oof[(oof["model"] == name) & (oof["fold"] == fold["fold"])]
            if want["transaction_id"].tolist() != val["transaction_id"].tolist() or \
                    not np.array_equal(got, want["score"].to_numpy()):
                raise ExplainabilityError(f"{name} fold {fold['fold']} replica does not "
                                          "reproduce M9A validation predictions exactly")
            replicas[(name, fold["fold"])] = (model, val, want["label"].to_numpy(dtype=int))
    champion = replicas[(ev.CHAMPION, config.folds)][0].named_steps["classifier"]
    if not np.array_equal(champion.coef_[0], np.asarray(m9a["params"]["coefficients"])):
        raise ExplainabilityError("fold replica differs from the frozen champion parameters")
    return replicas


def _top(vector: np.ndarray, k: int) -> set[str]:
    """The k most important features; ties by the fixed feature-contract order."""
    order = sorted(range(len(MODEL_FEATURES)), key=lambda i: (-float(vector[i]), i))
    return {MODEL_FEATURES[i] for i in order[:k]}


def spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    """Spearman rank correlation (average ranks for ties); None if either is constant."""
    ra = pd.Series(a).rank(method="average").to_numpy()
    rb = pd.Series(b).rank(method="average").to_numpy()
    if np.std(ra) == 0 or np.std(rb) == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


def importance(replicas: dict, *, repeats: int, seed: int) -> dict[str, Any]:
    """Permutation importance (PR-AUC drop) on each validation fold, per model."""
    result: dict[str, Any] = {}
    for (name, fold), (model, val, labels) in sorted(replicas.items()):
        found = permutation_importance(model, val[list(MODEL_FEATURES)], labels,
                                       scoring="average_precision", n_repeats=repeats,
                                       random_state=seed + fold)
        result.setdefault(name, {})[str(fold)] = {
            "mean": dict(zip(MODEL_FEATURES, found.importances_mean.tolist(), strict=True)),
            "std": dict(zip(MODEL_FEATURES, found.importances_std.tolist(), strict=True))}
    return result


MATERIALITY = 0.005  # supplementary metric; threshold chosen after seeing M9B results


def stability(imp: dict[str, Any], top_k: int = 5,
              materiality: float = MATERIALITY) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, folds in imp.items():
        keys = sorted(folds)
        vectors = {k: np.array([folds[k]["mean"][f] for f in MODEL_FEATURES]) for k in keys}
        pairs = {f"{a}-{b}": spearman(vectors[a], vectors[b])
                 for i, a in enumerate(keys) for b in keys[i + 1:]}
        valid = [v for v in pairs.values() if v is not None]
        mean = np.mean([vectors[k] for k in keys], axis=0)
        ranking = sorted(MODEL_FEATURES, key=lambda f: (-mean[MODEL_FEATURES.index(f)],
                                                        MODEL_FEATURES.index(f)))
        tops = [_top(vectors[k], top_k) for k in keys]
        material = [i for i, _ in enumerate(MODEL_FEATURES)
                    if max(abs(vectors[k][i]) for k in keys) >= materiality]
        material_pairs = {f"{a}-{b}": spearman(vectors[a][material], vectors[b][material])
                          for i, a in enumerate(keys) for b in keys[i + 1:]}
        material_valid = [v for v in material_pairs.values() if v is not None]
        out[name] = {"pairwise_spearman": pairs,
                     "mean_spearman": float(np.mean(valid)) if valid else None,
                     "supplementary_material_features": {
                         "definition": f"features with |mean importance| >= {materiality} in "
                                       "at least one fold; defined after seeing M9B results "
                                       "(not pre-registered)",
                         "features": [MODEL_FEATURES[i] for i in material],
                         "pairwise_spearman": material_pairs,
                         "mean_spearman": float(np.mean(material_valid))
                         if material_valid else None},
                     "mean_importance_ranking": ranking,
                     f"top_{top_k}_common_to_all_folds": sorted(set.intersection(*tops),
                                                                key=MODEL_FEATURES.index)}
    return out

