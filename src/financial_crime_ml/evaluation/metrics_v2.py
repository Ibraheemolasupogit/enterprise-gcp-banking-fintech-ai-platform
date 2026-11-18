"""M9A metrics: ranking, alert-capacity, calibration, day-block bootstrap CIs, segments.

Definitions (fixed, documented in docs/modelling.md):
* PR-AUC: scikit-learn ``average_precision_score``.
* Brier: mean squared error of probabilities.
* ECE: ``bins`` equal-width probability bins on [0, 1]; sum of |mean score - observed rate|
  weighted by bin share. The last bin is closed at 1.
* Capacity c: alert the top ``ceil(c * n)`` rows by score; ties broken by transaction_id
  ascending, so the result is deterministic.
* Threshold t: alert rows with score >= t.
* Bootstrap: resample whole UTC calendar days with replacement (transactions in one burst
  share a day, so the intervals respect that dependence); resamples lacking either class
  are skipped and counted.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    if not len(y):
        raise ValueError("ECE needs rows")
    index = np.minimum((p * bins).astype(int), bins - 1)
    total = 0.0
    for b in range(bins):
        mask = index == b
        if mask.any():
            total += mask.mean() * abs(float(p[mask].mean()) - float(y[mask].mean()))
    return float(total)


def capacity_alerts(scores: np.ndarray, ids: np.ndarray, capacity: float) -> np.ndarray:
    if not 0 < capacity <= 1:
        raise ValueError("capacity must be in (0, 1]")
    k = math.ceil(capacity * len(scores))
    # Score descending, then transaction_id ascending.
    order = np.lexsort((np.asarray(ids, dtype=str), -np.asarray(scores, dtype=float)))
    alerts = np.zeros(len(scores), dtype=bool)
    alerts[order[:k]] = True
    return alerts


def _rates(y: np.ndarray, alerts: np.ndarray) -> dict[str, float]:
    tp = int((alerts & (y == 1)).sum())
    alerted, positives = int(alerts.sum()), int((y == 1).sum())
    return {"precision": tp / alerted if alerted else 0.0,
            "recall": tp / positives if positives else 0.0,
            "alert_rate": alerted / len(y) if len(y) else 0.0, "alerts": alerted,
            "true_positives": tp}


def point_metrics(y: np.ndarray, p: np.ndarray, ids: np.ndarray, *, capacities: tuple,
                  threshold: float | None, bins: int) -> dict[str, Any]:
    y = np.asarray(y, dtype=int)
    out: dict[str, Any] = {"rows": int(len(y)), "positives": int(y.sum()),
                           "pr_auc": float(average_precision_score(y, p)),
                           "roc_auc": float(roc_auc_score(y, p)), "brier": brier(y, p),
                           "ece": ece(y, p, bins), "prevalence": float(y.mean())}
    out["at_capacity"] = {str(c): _rates(y, capacity_alerts(p, ids, c)) for c in capacities}
    if threshold is not None:
        out["at_threshold"] = {"threshold": threshold, **_rates(y, p >= threshold)}
    return out


def _flatten(metrics: dict[str, Any]) -> dict[str, float]:
    flat = {k: metrics[k] for k in ("pr_auc", "roc_auc", "brier", "ece")}
    for c, rates in metrics["at_capacity"].items():
        flat[f"precision@{c}"] = rates["precision"]
        flat[f"recall@{c}"] = rates["recall"]
    if "at_threshold" in metrics:
        for k in ("precision", "recall", "alert_rate"):
            flat[f"{k}@threshold"] = metrics["at_threshold"][k]
    return flat


def bootstrap(y: np.ndarray, p: np.ndarray, ids: np.ndarray, days: np.ndarray, *,
              capacities: tuple, threshold: float | None, bins: int, resamples: int,
              confidence: float, seed: int) -> dict[str, Any]:
    """Percentile CIs from day-block resamples; deterministic for a seed."""
    rng = np.random.default_rng(seed)
    unique = np.unique(days)
    by_day = {d: np.flatnonzero(days == d) for d in unique}
    samples: dict[str, list[float]] = {}
    skipped = 0
    for _ in range(resamples):
        chosen = rng.choice(unique, size=len(unique), replace=True)
        index = np.concatenate([by_day[d] for d in chosen])
        ys = y[index]
        if ys.min() == ys.max():
            skipped += 1
            continue
        # Duplicated rows keep distinct tie-break keys so capacity stays deterministic.
        keys = np.char.add(ids[index].astype(str), np.arange(len(index)).astype(str))
        flat = _flatten(point_metrics(ys, p[index], keys, capacities=capacities,
                                      threshold=threshold, bins=bins))
        for k, v in flat.items():
            samples.setdefault(k, []).append(v)
    alpha = (1 - confidence) / 2
    intervals = {k: [float(np.quantile(v, alpha)), float(np.quantile(v, 1 - alpha))]
                 for k, v in samples.items()}
    return {"method": "day-block percentile bootstrap", "resamples": resamples,
            "used": resamples - skipped, "skipped_single_class": skipped,
            "confidence": confidence, "days": int(len(unique)), "intervals": intervals}


def segment_metrics(frame: pd.DataFrame, *, column: str, threshold: float, bins: int,
                    min_rows: int, min_class: int) -> dict[str, Any]:
    """``frame`` has columns label, score, ``column``. Metrics only with enough support."""
    result = {}
    for value, part in sorted(frame.groupby(column), key=lambda kv: str(kv[0])):
        y, p = part["label"].to_numpy(dtype=int), part["score"].to_numpy(dtype=float)
        support = {"rows": int(len(y)), "positives": int(y.sum()),
                   "negatives": int(len(y) - y.sum())}
        if (support["rows"] < min_rows or support["positives"] < min_class
                or support["negatives"] < min_class):
            result[str(value)] = {**support, "status": "insufficient_support"}
            continue
        result[str(value)] = {**support, "status": "reported",
                              "pr_auc": float(average_precision_score(y, p)),
                              "brier": brier(y, p), "ece": ece(y, p, bins),
                              **{f"{k}@threshold": v for k, v in
                                 _rates(y, p >= threshold).items()
                                 if k in ("precision", "recall", "alert_rate")}}
    return result
