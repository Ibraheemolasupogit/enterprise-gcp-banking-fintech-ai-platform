"""M9C portable model specification and the Python reference scorer.

The specification is built only from hash-verified M9A/M9B evidence (the champion is never
refit). It is serialised as canonical JSON (sorted keys, no whitespace, floats in Python's
round-trip ``repr``) and identified by the SHA-256 of those bytes.

Scoring rules fixed by the specification (shared by Python and SQL):
* input validity: every contract input column non-NULL, string columns non-empty, currency
  in the supported exponent list. Invalid rows are excluded from history and get NULL
  scores; they are reported, never silently repaired.
* logit = intercept + Σ numeric coef·(x − mean)/scale + Σ coef(active category); an unseen
  category contributes 0.
* probability = 1 / (1 + exp(−logit)).
* score_ppm = FLOOR(probability × 1,000,000 + 0.5) as INT64 (round half up; 0..1,000,000).
* alert: spec v1 ``probability >= threshold`` (FLOAT64); spec v2 (approved Option B)
  ``score_ppm >= threshold_ppm`` (INT64), with threshold_ppm = to_ppm(M9A threshold).
* reason codes (alerts only): positive contributions ranked by contribution descending,
  then fixed feature order; three codes, padded with R00.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from financial_crime_ml.data_generation.v2.contracts import CURRENCY_EXPONENTS
from financial_crime_ml.evaluation import explainability as xp
from financial_crime_ml.evaluation.behavioural import (
    INPUT_COLUMNS,
    MODEL_CATEGORICAL_FEATURES,
    MODEL_FEATURES,
    MODEL_NUMERIC_FEATURES,
    build_behavioural_features,
)
from financial_crime_ml.evaluation.manifest import sha256_bytes

SPEC_V1 = "m9c-spec-1.0.0"   # historical: FLOAT64 alert rule (m9c-export-1/-2)
SPEC_V2 = "m9c-spec-2.0.0"   # approved Option B: INT64 score_ppm alert rule
SPEC_VERSION = SPEC_V2
APPROVAL_PATH = Path(__file__).resolve().parents[3] / "configs/approvals/DR-M9B-m9a-eval-1.json"
PPM = 1_000_000
STRING_INPUTS = tuple(c for c in INPUT_COLUMNS if c not in ("amount_minor",
                                                            "transaction_timestamp"))
REASON_SLOTS = 3


class SpecError(ValueError):
    """The specification or its evidence failed verification."""


def canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def to_ppm(probability: float) -> int:
    """Round half up to an integer number of parts per million (0..1,000,000)."""
    if not (isinstance(probability, float | int) and 0.0 <= probability <= 1.0):
        raise SpecError("probability must be within [0, 1]")
    return int(math.floor(float(probability) * PPM + 0.5))


# --------------------------------------------------------------------------- evidence
def load_m9b(run_dir: Path, m9a_manifest_sha256: str) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    manifest = json.loads((run_dir / "manifest.json").read_bytes())
    if manifest.get("milestone") != "M9B" or manifest.get("status") != "completed":
        raise SpecError("require a completed M9B run")
    if manifest.get("m9a_manifest_sha256") != m9a_manifest_sha256:
        raise SpecError("M9B run was produced from a different M9A run")
    contents = {}
    for name, digest in manifest["artifacts_sha256"].items():
        content = (run_dir / name).read_bytes()
        if sha256_bytes(content) != digest:
            raise SpecError(f"M9B artifact hash mismatch: {name}")
        contents[name] = content
    return {"run_dir": run_dir, "manifest": manifest, "contents": contents,
            "manifest_sha256": sha256_bytes((run_dir / "manifest.json").read_bytes())}


def load_approval(path: Path, m9a: dict[str, Any], m9b: dict[str, Any]) -> dict[str, Any]:
    """The limited approval of DR-M9B, bound by hash to the M9B decision record."""
    content = Path(path).read_bytes()
    record = json.loads(content)
    decision = json.loads(m9b["contents"]["decision_record.json"])
    policy = record.get("deployment_policy_change") or {}
    checks = {
        "decision_record_id": record.get("decision_record_id") == decision["id"],
        "decision_record_sha256": record.get("decision_record_sha256") ==
        sha256_bytes(m9b["contents"]["decision_record.json"]),
        "selection_sha256": record.get("selection_sha256") ==
        m9a["selection"]["selection_sha256"],
        "decision": record.get("decision") == decision["decision"],
        "status": record.get("status") == "approved_limited",
        "production_pending": record.get("production_approval") == "pending",
        "option_b": policy.get("option") == "B" and policy.get("spec_version") == SPEC_V2,
        "threshold_ppm": policy.get("threshold_ppm") == to_ppm(m9a["selection"]["threshold"]),
    }
    failed = sorted(k for k, ok in checks.items() if not ok)
    if failed:
        raise SpecError(f"approval record does not match the evidence: {failed}")
    return {"record": record, "sha256": sha256_bytes(content)}


def build_spec(m9a: dict[str, Any], m9b: dict[str, Any], *, version: str = SPEC_VERSION,
               approval: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic specification from the frozen parameters and M9B mapping. ``version``
    selects the decision rule only; every model term is identical across versions."""
    if version not in (SPEC_V1, SPEC_V2):
        raise SpecError(f"unknown spec version {version}")
    if version == SPEC_V2 and approval is None:
        raise SpecError("spec v2 requires the approved deployment-policy record")
    params, selection = m9a["params"], m9a["selection"]
    coef = list(params["coefficients"])
    numeric = [{"name": name, "mean": params["scaler_mean"][j],
                "scale": params["scaler_scale"][j], "coefficient": coef[j]}
               for j, name in enumerate(MODEL_NUMERIC_FEATURES)]
    offset, categorical = len(MODEL_NUMERIC_FEATURES), []
    for name, values in zip(MODEL_CATEGORICAL_FEATURES, params["categories"], strict=True):
        categorical.append({"name": name, "categories": [
            {"value": v, "coefficient": coef[offset + k]} for k, v in enumerate(values)]})
        offset += len(values)
    mapping = json.loads(m9b["contents"]["reason_code_mapping.json"])
    if mapping["version"] != xp.REASON_VERSION or {k: tuple(v) for k, v in
                                                   mapping["codes"].items()} != xp.REASONS:
        raise SpecError("M9B reason mapping differs from the code")
    decision = json.loads(m9b["contents"]["decision_record.json"])
    contract = m9a["feature_contract"]
    currencies = sorted(CURRENCY_EXPONENTS)
    spec = {
        "spec_version": version,
        "provenance": {
            "m9a_run_id": m9a["manifest"]["run_id"],
            "m9a_manifest_sha256": sha256_bytes((m9a["run_dir"] / "manifest.json")
                                                .read_bytes()),
            "m9b_run_id": m9b["manifest"]["run_id"],
            "m9b_manifest_sha256": m9b["manifest_sha256"],
            "selection_sha256": selection["selection_sha256"],
            "model_parameters_sha256": m9a["manifest"]["artifacts_sha256"][
                "model_parameters.json"],
            "decision_record_id": decision["id"],
            "decision_approval_status": decision["approval"]["status"]},
        "feature_contract": contract,
        "input_validity": {
            "required_non_null": list(INPUT_COLUMNS),
            "non_empty_strings": list(STRING_INPUTS),
            "currency_exponents": {c: CURRENCY_EXPONENTS[c] for c in currencies},
            "invalid_rows": "excluded from history; NULL score, alert and reason codes"},
        "model": {
            "family": "logistic_regression", "feature_version": params["feature_version"],
            "intercept": params["intercept"], "numeric": numeric,
            "categorical": categorical, "unseen_category_contribution": 0.0},
        "decision": {
            "threshold": selection["threshold"],
            "threshold_ppm": to_ppm(selection["threshold"]),
            "alert_rule": "probability >= threshold (FLOAT64 comparison)",
            "probability": "1 / (1 + EXP(-logit))",
            "score_ppm": "CAST(FLOOR(probability * 1000000 + 0.5) AS INT64)"},
        "reason_codes": {
            "version": mapping["version"], "slots": REASON_SLOTS,
            "ranking": "positive contributions only; contribution descending, then "
                       "feature order ascending",
            "padding": list(xp.NO_FURTHER_REASON),
            "features": [{"name": f, "order": i, "code": xp.REASONS[f][0],
                          "text": xp.REASONS[f][1]} for i, f in enumerate(MODEL_FEATURES)]},
    }
    if version == SPEC_V2:
        assert approval is not None
        record, threshold = approval["record"], selection["threshold"]
        spec["provenance"]["decision_approval_status"] = record["status"]
        spec["provenance"]["approval"] = {
            "record_sha256": approval["sha256"], "approved_on": record["approved_on"],
            "scope": record["scope"], "not_approved": record["not_approved"],
            "production_approval": record["production_approval"]}
        spec["decision"] = {
            "m9a_threshold": threshold, "threshold_ppm": to_ppm(threshold),
            "alert_rule": "score_ppm >= threshold_ppm (INT64 comparison)",
            "probability": "1 / (1 + EXP(-logit))",
            "score_ppm": "CAST(FLOOR(probability * 1000000 + 0.5) AS INT64)",
            "policy": "approved deployment policy, Option B (DR-M9B-m9a-eval-1)",
            "quantisation": "alerts iff probability rounds (half up) to >= threshold_ppm, i.e. "
                            "probability >= (threshold_ppm - 0.5) / 1e6 up to FLOAT64 rounding "
                            "of probability * 1e6; resolution 1 ppm"}
    return spec


def uses_ppm_rule(spec: dict[str, Any]) -> bool:
    return spec["spec_version"] == SPEC_V2


def alert_flags(spec: dict[str, Any], probability: np.ndarray, score_ppm: np.ndarray) -> np.ndarray:
    """The specification's alert rule (0/1)."""
    if uses_ppm_rule(spec):
        return (np.asarray(score_ppm, dtype=np.int64) >=
                spec["decision"]["threshold_ppm"]).astype(int)
    return (np.asarray(probability, dtype=float) >= spec["decision"]["threshold"]).astype(int)


def spec_sha256(spec: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(spec)).hexdigest()


def params_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Back to the M9A parameter layout (for exact reuse of the M9B contribution code)."""
    model = spec["model"]
    return {
        "numeric_features": [n["name"] for n in model["numeric"]],
        "scaler_mean": [n["mean"] for n in model["numeric"]],
        "scaler_scale": [n["scale"] for n in model["numeric"]],
        "categorical_features": [c["name"] for c in model["categorical"]],
        "categories": [[v["value"] for v in c["categories"]] for c in model["categorical"]],
        "coefficients": [n["coefficient"] for n in model["numeric"]] +
                        [v["coefficient"] for c in model["categorical"]
                         for v in c["categories"]],
        "transformed_features": [f"numeric__{n['name']}" for n in model["numeric"]] +
                                [f"categorical__{c['name']}_{v['value']}"
                                 for c in model["categorical"] for v in c["categories"]],
        "intercept": model["intercept"]}


# --------------------------------------------------------------------------- reference scorer
def valid_mask(transactions: pd.DataFrame, spec: dict[str, Any]) -> pd.Series:
    rules = spec["input_validity"]
    mask = pd.Series(True, index=transactions.index)
    for column in rules["required_non_null"]:
        values = transactions[column]
        mask &= values.notna()
        if column in rules["non_empty_strings"]:
            mask &= values.astype(str).str.len() > 0
    return mask & transactions["currency"].isin(list(rules["currency_exponents"]))


def score_python(spec: dict[str, Any], transactions: pd.DataFrame) -> pd.DataFrame:
    """Reference implementation of every rule in the specification (labels never read)."""
    data = transactions.loc[:, list(INPUT_COLUMNS)].copy()
    data = data.replace({"": None})
    mask = valid_mask(data, spec)
    features = build_behavioural_features(data[mask])
    contrib = xp.contributions(features, params_from_spec(spec))
    out = features.copy()
    for name in MODEL_FEATURES:
        out[f"c_{name}"] = contrib[name].to_numpy()
    out["logit"] = contrib["logit"].to_numpy()
    out["probability"] = 1.0 / (1.0 + np.exp(-out["logit"].to_numpy()))
    out["score_ppm"] = [to_ppm(float(p)) for p in out["probability"]]
    out["alert"] = alert_flags(spec, out["probability"].to_numpy(), out["score_ppm"].to_numpy())
    alerts = out.loc[out["alert"] == 1, ["transaction_id", "probability"]].rename(
        columns={"probability": "score"})
    codes = xp.reason_codes(contrib, features, alerts, top=REASON_SLOTS)
    wide = codes.pivot(index="transaction_id", columns="rank", values="code")
    for slot in range(1, REASON_SLOTS + 1):
        mapped = out["transaction_id"].map(wide[slot] if slot in wide else {}).astype(object)
        mapped[out["alert"] != 1] = None  # reason codes exist for alerts only
        out[f"reason_code_{slot}"] = mapped
    out["input_valid"] = 1
    invalid = pd.DataFrame({"transaction_id": data.loc[~mask, "transaction_id"],
                            "input_valid": 0})
    return pd.concat([out, invalid], ignore_index=True)


def read_predictions(content: bytes) -> pd.DataFrame:
    """M9A prediction files without their label column (labels are never loaded)."""
    header = pd.read_csv(io.BytesIO(content), nrows=0).columns
    wanted = [c for c in ("transaction_id", "score", "alert", "fold", "model") if c in header]
    return pd.read_csv(io.BytesIO(content), usecols=wanted, dtype={"transaction_id": str},
                       float_precision="round_trip")
