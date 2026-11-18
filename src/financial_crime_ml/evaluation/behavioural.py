"""M9A point-in-time behavioural features (strictly past-only, event-time ordered).

Every behavioural value for a transaction at UTC instant ``t`` is computed only from
*observations* (never labels) of transactions with event time strictly earlier than ``t``.
Transactions sharing the same instant never see each other, so the result does not depend
on how ties are ordered. Rows are returned in the deterministic order
``(transaction_timestamp, transaction_id)``.

The definitions are written so they can be reproduced exactly in BigQuery over
``curated_transactions`` (see ``FEATURE_SPECS``); M9A establishes those requirements and an
offline SQL reference check, but implements no cloud scoring.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd

from financial_crime_ml.data_generation.v2.contracts import CURRENCY_EXPONENTS
from financial_crime_ml.evaluation.contracts import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    build_transaction_features,
)

FEATURE_VERSION = "m9a-features-1.0.0"
NO_HISTORY_SECONDS = 30 * 86400  # cap and sentinel for "seconds since previous"
# Only these observation columns are read; all exist in the BigQuery curated_transactions.
INPUT_COLUMNS = (
    "transaction_id", "customer_id", "account_id", "beneficiary_id", "device_id",
    "transaction_timestamp", "amount_minor", "amount", "currency", "transaction_type",
    "merchant_category", "origin_country", "destination_country", "channel",
)
LABEL_LIKE = ("is_suspicious", "episode_id", "scenario_type")

# name -> exact definition. Windows are half-open [t - w, t) in whole seconds; in GoogleSQL
# over UNIX_MICROS(transaction_timestamp): RANGE BETWEEN w*1e6 PRECEDING AND 1 PRECEDING.
FEATURE_SPECS: dict[str, dict[str, Any]] = {
    "acct_txn_count_10m": {"partition": ["account_id"], "window_seconds": 600,
                           "aggregate": "COUNT(*)", "empty": 0, "type": "INT64"},
    "acct_txn_count_1h": {"partition": ["account_id"], "window_seconds": 3600,
                          "aggregate": "COUNT(*)", "empty": 0, "type": "INT64"},
    "acct_txn_count_24h": {"partition": ["account_id"], "window_seconds": 86400,
                           "aggregate": "COUNT(*)", "empty": 0, "type": "INT64"},
    "cust_txn_count_24h": {"partition": ["customer_id"], "window_seconds": 86400,
                           "aggregate": "COUNT(*)", "empty": 0, "type": "INT64"},
    "acct_same_amount_count_24h": {"partition": ["account_id", "amount_minor"],
                                   "window_seconds": 86400, "aggregate": "COUNT(*)",
                                   "empty": 0, "type": "INT64"},
    "acct_history_count_30d": {"partition": ["account_id"], "window_seconds": 2592000,
                               "aggregate": "COUNT(*)", "empty": 0, "type": "INT64"},
    "acct_amount_log_ratio_30d": {
        "partition": ["account_id"], "window_seconds": 2592000,
        "aggregate": "LN(amount_minor + 1) - LN(CAST(SUM(amount_minor) AS FLOAT64) / "
                     "COUNT(*) + 1)",
        "empty": 0.0, "type": "FLOAT64",
        "note": "same account => same currency; mean is exact SUM/COUNT in FLOAT64"},
    "acct_seconds_since_prev_log": {
        "partition": ["account_id"], "window_seconds": None,
        "aggregate": f"LN(1 + LEAST(t - MAX(prior t), {NO_HISTORY_SECONDS}))",
        "empty": f"LN(1 + {NO_HISTORY_SECONDS})", "type": "FLOAT64",
        "note": "prior = strictly earlier instant on the same account (unbounded window)"},
    "acct_has_prev": {"partition": ["account_id"], "window_seconds": None,
                      "aggregate": "IF(COUNT(*) > 0, 1, 0)", "empty": 0, "type": "INT64"},
    "acct_new_beneficiary": {"partition": ["account_id", "beneficiary_id"],
                             "window_seconds": None, "aggregate": "IF(COUNT(*) = 0, 1, 0)",
                             "empty": 1, "type": "INT64"},
    "cust_new_device": {"partition": ["customer_id", "device_id"], "window_seconds": None,
                        "aggregate": "IF(COUNT(*) = 0, 1, 0)", "empty": 1, "type": "INT64"},
    "is_round_amount": {"partition": None, "window_seconds": None,
                        "aggregate": "IF(MOD(amount_minor, 100 * POW(10, exponent)) = 0, 1, 0)",
                        "empty": None, "type": "INT64",
                        "note": "row-local; exponent from the currency (JPY 0, others 2)"},
}
BEHAVIOURAL_FEATURES = tuple(FEATURE_SPECS)
MODEL_NUMERIC_FEATURES = (*NUMERIC_FEATURES, *BEHAVIOURAL_FEATURES)
MODEL_CATEGORICAL_FEATURES = CATEGORICAL_FEATURES
MODEL_FEATURES = (*MODEL_NUMERIC_FEATURES, *MODEL_CATEGORICAL_FEATURES)

CLOUD_REPRODUCTION = {
    "source_table": "curated_transactions (the M7D-verified input table)",
    "ordering": "(transaction_timestamp, transaction_id); ties share an instant and never "
                "see each other (strictly earlier event time only)",
    "time_unit": "UNIX_MICROS(transaction_timestamp); inputs are whole seconds",
    "history_scope": "all rows in the scored table with earlier event time. Values equal the "
                     "offline values only when the cloud history equals the offline history "
                     "(same transaction set); cold-start rows have empty-history defaults",
    "row_local_m1": {
        "transaction_amount_log": "LN(1 + CAST(amount AS FLOAT64)) (amount is the canonical "
                                  "decimal STRING; matches Python float parsing)",
        "transaction_hour": "EXTRACT(HOUR FROM transaction_timestamp AT TIME ZONE 'UTC')",
        "transaction_day_of_week": "MOD(EXTRACT(DAYOFWEEK FROM transaction_timestamp) + 5, 7) "
                                   "(Monday = 0, as pandas)",
        "is_cross_border_transaction": "IF(origin_country != destination_country, 1, 0)",
    },
    "categorical": "exact STRING values; categories unseen in training map to all-zero "
                   "one-hot columns (logistic regression)",
    "parity_requirement": "INT64 features exact; FLOAT64 features within 1e-12 absolute",
    "not_implemented": "M9A defines and checks these offline only; no cloud scoring",
}


def contract() -> dict[str, Any]:
    body = {"feature_version": FEATURE_VERSION, "input_columns": list(INPUT_COLUMNS),
            "row_local_numeric": list(NUMERIC_FEATURES),
            "categorical": list(MODEL_CATEGORICAL_FEATURES),
            "behavioural": FEATURE_SPECS, "model_features": list(MODEL_FEATURES),
            "labels_excluded": list(LABEL_LIKE), "cloud_reproduction": CLOUD_REPRODUCTION}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return {**body, "contract_sha256": digest}


def order_observations(transactions: pd.DataFrame) -> pd.DataFrame:
    """Observation columns only (labels are never read), deterministically ordered."""
    missing = sorted(set(INPUT_COLUMNS) - set(transactions.columns))
    if missing:
        raise ValueError(f"Missing observation columns: {missing}")
    data = transactions.loc[:, INPUT_COLUMNS].copy()
    for column in ("customer_id", "account_id", "beneficiary_id", "device_id"):
        if not data[column].map(lambda v: isinstance(v, str) and bool(v)).all():
            raise ValueError(f"{column} must contain nonempty strings")
    data["amount_minor"] = pd.to_numeric(data["amount_minor"], errors="raise").astype("int64")
    if (data["amount_minor"] < 0).any():
        raise ValueError("amount_minor must be nonnegative")
    if not data["currency"].isin(list(CURRENCY_EXPONENTS)).all():
        raise ValueError("unsupported currency")
    ts = pd.to_datetime(data["transaction_timestamp"], format="ISO8601", utc=True)
    data["transaction_timestamp"] = ts
    if data["transaction_id"].duplicated().any():
        raise ValueError("transaction_id must be unique")
    return data.sort_values(["transaction_timestamp", "transaction_id"],
                            kind="stable").reset_index(drop=True)


def _epoch_seconds(ts: pd.Series) -> np.ndarray:
    seconds = ts.astype("int64").to_numpy() // 10**9
    if (ts.dt.microsecond != 0).any() or (ts.dt.nanosecond != 0).any():
        raise ValueError("timestamps must be whole seconds")
    return seconds


def _grouped(data: pd.DataFrame, keys: list[str], t: np.ndarray, window: int | None,
             values: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per row: count and sum of strictly earlier rows of the same group in [t-window, t),
    and the latest strictly earlier instant (-1 if none). Window None = unbounded."""
    n = len(data)
    count, total = np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)
    previous = np.full(n, -1, dtype=np.int64)
    for _, index in data.groupby(keys, sort=False).indices.items():
        index = np.asarray(index)  # rows are globally ordered, so group order is chronological
        times = t[index]
        right = np.searchsorted(times, times, side="left")  # strictly earlier instants
        left = (np.zeros_like(right) if window is None
                else np.searchsorted(times, times - window, side="left"))
        count[index] = right - left
        has = right > 0
        previous[index[has]] = times[right[has] - 1]
        if values is not None:
            cumulative = np.concatenate(([0], np.cumsum(values[index], dtype=np.int64)))
            total[index] = cumulative[right] - cumulative[left]
    return count, total, previous


def build_behavioural_features(transactions: pd.DataFrame) -> pd.DataFrame:
    """Observations -> ordered frame of ids/time plus every model feature (no labels)."""
    data = order_observations(transactions)
    t = _epoch_seconds(data["transaction_timestamp"])
    amounts = data["amount_minor"].to_numpy(dtype=np.int64)
    out = pd.DataFrame({"transaction_id": data["transaction_id"],
                        "transaction_timestamp": data["transaction_timestamp"]})
    row_local = build_transaction_features(
        data.assign(transaction_timestamp=data["transaction_timestamp"].map(
            lambda v: v.isoformat())))
    for column in (*NUMERIC_FEATURES, *CATEGORICAL_FEATURES):
        out[column] = row_local[column].to_numpy()
    for name, spec in FEATURE_SPECS.items():
        if name in ("acct_amount_log_ratio_30d", "acct_seconds_since_prev_log",
                    "acct_has_prev", "is_round_amount"):
            continue
        count, _, _ = _grouped(data, spec["partition"], t, spec["window_seconds"])
        out[name] = (count == 0).astype(np.int64) if name.endswith(("_new_beneficiary",
                                                                     "_new_device")) else count
    count, total, _ = _grouped(data, ["account_id"], t, 2592000, amounts)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = np.where(count > 0, total.astype(np.float64) / np.maximum(count, 1), 0.0)
    out["acct_amount_log_ratio_30d"] = np.where(
        count > 0, np.log(amounts.astype(np.float64) + 1.0) - np.log(mean + 1.0), 0.0)
    _, _, previous = _grouped(data, ["account_id"], t, None)
    gap = np.where(previous >= 0, np.minimum(t - previous, NO_HISTORY_SECONDS),
                   NO_HISTORY_SECONDS)
    out["acct_seconds_since_prev_log"] = np.log1p(gap.astype(np.float64))
    out["acct_has_prev"] = (previous >= 0).astype(np.int64)
    unit = data["currency"].map(lambda c: 100 * 10 ** CURRENCY_EXPONENTS[c]).to_numpy()
    out["is_round_amount"] = (amounts % unit == 0).astype(np.int64)
    return out.loc[:, ["transaction_id", "transaction_timestamp", *MODEL_FEATURES]]
