"""M9C monitoring reference profile (no labels) and PSI against fixed reference bins.

Reference window: the champion's own validation window (M9A fold 3), scored with the frozen
specification. Bins are fixed here so later cloud scores can be compared exactly:
* numeric features: interior cut points at the reference deciles (deduplicated); bins are
  ``(-inf, c1], (c1, c2], …, (ck, +inf)``;
* categorical features: reference categories plus ``__other__`` for anything unseen;
* score: fixed ppm edges, plus reference deciles; alert rate at the frozen threshold.
PSI = Σ (a − e) ln(a / e) with each proportion floored at ``EPSILON``. Bands: < 0.10 stable,
0.10–0.25 moderate shift, >= 0.25 significant shift (the M9 proposal's bands).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from financial_crime_ml.evaluation.behavioural import (
    MODEL_CATEGORICAL_FEATURES,
    MODEL_NUMERIC_FEATURES,
)

EPSILON = 1e-6
PSI_BANDS = {"stable_below": 0.10, "significant_at_or_above": 0.25}
SCORE_EDGES_PPM = [0, 10_000, 20_000, 50_000, 100_000, 200_000, 300_000, 500_000, 1_000_000]
OTHER = "__other__"


def _numeric_bins(values: np.ndarray) -> list[float]:
    cuts = np.quantile(values, np.linspace(0.1, 0.9, 9))
    return sorted({float(c) for c in cuts})


def numeric_proportions(values: np.ndarray, cuts: list[float]) -> list[float]:
    index = np.searchsorted(np.asarray(cuts), values, side="left")  # (c_{i-1}, c_i]
    counts = np.bincount(index, minlength=len(cuts) + 1)
    return (counts / max(len(values), 1)).tolist()


def categorical_proportions(values: pd.Series, categories: list[str]) -> dict[str, float]:
    mapped = values.where(values.isin(categories), OTHER)
    shares = mapped.value_counts(normalize=True)
    return {c: float(shares.get(c, 0.0)) for c in [*categories, OTHER]}


def score_proportions(ppm: np.ndarray) -> list[float]:
    edges = np.asarray(SCORE_EDGES_PPM)
    index = np.clip(np.searchsorted(edges, ppm, side="right") - 1, 0, len(edges) - 2)
    return (np.bincount(index, minlength=len(edges) - 1) / max(len(ppm), 1)).tolist()


def psi(expected: list[float], actual: list[float]) -> float:
    if len(expected) != len(actual):
        raise ValueError("PSI needs the same bins")
    e = np.maximum(np.asarray(expected, dtype=float), EPSILON)
    a = np.maximum(np.asarray(actual, dtype=float), EPSILON)
    return float(np.sum((a - e) * np.log(a / e)))


def band(value: float) -> str:
    if value < PSI_BANDS["stable_below"]:
        return "stable"
    return "moderate" if value < PSI_BANDS["significant_at_or_above"] else "significant"


def build_profile(scored: pd.DataFrame, *, window: str, spec_sha256: str) -> dict[str, Any]:
    """``scored``: valid rows from ``score_python`` (features, score_ppm, alert)."""
    numeric = {}
    for name in MODEL_NUMERIC_FEATURES:
        values = scored[name].to_numpy(dtype=float)
        cuts = _numeric_bins(values)
        numeric[name] = {"cuts": cuts, "proportions": numeric_proportions(values, cuts),
                         "mean": float(values.mean()), "std": float(values.std()),
                         "min": float(values.min()), "max": float(values.max())}
    categorical = {}
    for name in MODEL_CATEGORICAL_FEATURES:
        categories = sorted(scored[name].astype(str).unique().tolist())
        categorical[name] = {"categories": categories, "proportions":
                             categorical_proportions(scored[name].astype(str), categories)}
    ppm = scored["score_ppm"].to_numpy(dtype=np.int64)
    return {"reference_window": window, "model_spec_sha256": spec_sha256,
            "rows": int(len(scored)), "labels_used": False,
            "numeric": numeric, "categorical": categorical,
            "score_ppm": {"edges": SCORE_EDGES_PPM, "proportions": score_proportions(ppm),
                          "deciles": [int(v) for v in np.quantile(
                              ppm, np.linspace(0.1, 0.9, 9), method="lower")],
                          "mean": float(ppm.mean())},
            "alert_rate": float(scored["alert"].mean()),
            "psi": {"epsilon": EPSILON, "bands": PSI_BANDS,
                    "formula": "sum((a - e) * ln(a / e)) over fixed reference bins"}}


def compare(profile: dict[str, Any], current: pd.DataFrame) -> dict[str, Any]:
    """PSI of a new scored window against the reference profile."""
    out: dict[str, Any] = {}
    for name, ref in profile["numeric"].items():
        value = psi(ref["proportions"],
                    numeric_proportions(current[name].to_numpy(dtype=float), ref["cuts"]))
        out[name] = {"psi": value, "band": band(value)}
    for name, ref in profile["categorical"].items():
        actual = categorical_proportions(current[name].astype(str), ref["categories"])
        # pair by category, never by position (a JSON round trip may reorder keys)
        if set(actual) != set(ref["proportions"]):
            raise ValueError(f"{name}: categories differ from the reference profile")
        value = psi([ref["proportions"][k] for k in actual], [actual[k] for k in actual])
        out[name] = {"psi": value, "band": band(value)}
    value = psi(profile["score_ppm"]["proportions"],
                score_proportions(current["score_ppm"].to_numpy(dtype=np.int64)))
    out["score_ppm"] = {"psi": value, "band": band(value)}
    out["alert_rate"] = {"reference": profile["alert_rate"],
                         "current": float(current["alert"].mean())}
    return out
