"""Transaction-time feature contract and chronological partitioning."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

import numpy as np
import pandas as pd

NUMERIC_FEATURES = (
    "transaction_amount_log",
    "transaction_hour",
    "transaction_day_of_week",
    "is_cross_border_transaction",
)
CATEGORICAL_FEATURES = (
    "currency",
    "transaction_type",
    "merchant_category",
    "origin_country",
    "destination_country",
    "channel",
)
FEATURE_ALLOWLIST = (*NUMERIC_FEATURES, *CATEGORICAL_FEATURES)
TARGET_COLUMN = "is_suspicious"
OBSERVATION_COLUMNS = (
    "transaction_id",
    "transaction_timestamp",
    "amount",
    *CATEGORICAL_FEATURES,
)


@dataclass(frozen=True)
class EvaluationConfig:
    """Fixed model/feature contract; only split, seed and threshold grid vary."""

    seed: int = 42
    train_fraction: float = 0.6
    validation_fraction: float = 0.2
    threshold_candidates: tuple[float, ...] = tuple(i / 20 for i in range(1, 20))

    def __post_init__(self) -> None:
        if type(self.seed) is not int or not 0 <= self.seed < 2**32:
            raise ValueError("seed must be an integer between 0 and 2**32 - 1.")
        fractions = (self.train_fraction, self.validation_fraction)
        if any(not isfinite(v) or not 0 < v < 1 for v in fractions) or sum(fractions) >= 1:
            raise ValueError("Positive train/validation fractions must sum to less than 1.")
        grid = self.threshold_candidates
        if not grid or any(not isfinite(v) or not 0 < v < 1 for v in grid):
            raise ValueError("threshold_candidates must contain finite values between 0 and 1.")
        if tuple(sorted(set(grid))) != grid:
            raise ValueError("threshold_candidates must be sorted and unique.")


def validate_observations(transactions: pd.DataFrame) -> pd.DataFrame:
    """Validate only transaction observations; ignore all labels/helper/extra fields."""
    missing = set(OBSERVATION_COLUMNS) - set(transactions.columns)
    if missing:
        raise ValueError(f"Missing transaction columns: {sorted(missing)}")
    if transactions.empty:
        raise ValueError("Transactions must not be empty.")
    data = transactions.loc[:, OBSERVATION_COLUMNS].copy()
    for column in ("transaction_id", *CATEGORICAL_FEATURES):
        values = data[column]
        if not values.map(lambda v: isinstance(v, str) and bool(v.strip())).all():
            raise ValueError(f"{column} must contain nonempty strings.")
    if data["transaction_id"].duplicated().any():
        raise ValueError("transaction_id must be unique.")
    timestamps = pd.to_datetime(
        data["transaction_timestamp"], format="ISO8601", errors="coerce", utc=True
    )
    if timestamps.isna().any():
        raise ValueError("transaction_timestamp must contain valid timestamps.")
    amounts = pd.to_numeric(data["amount"], errors="coerce")
    if not np.isfinite(amounts.to_numpy(dtype=float)).all() or amounts.lt(0).any():
        raise ValueError("amount must contain finite, nonnegative numbers.")
    data["transaction_timestamp"] = timestamps
    data["amount"] = amounts.astype(float)
    return data


def build_transaction_features(transactions: pd.DataFrame) -> pd.DataFrame:
    """Build row-local observations without labels, joins, fitting or aggregates."""
    data = validate_observations(transactions)
    features = data.loc[:, CATEGORICAL_FEATURES].copy()
    features["transaction_amount_log"] = np.log1p(data["amount"])
    features["transaction_hour"] = data["transaction_timestamp"].dt.hour
    features["transaction_day_of_week"] = data["transaction_timestamp"].dt.dayofweek
    features["is_cross_border_transaction"] = (
        data["origin_country"] != data["destination_country"]
    ).astype(int)
    return features.loc[:, FEATURE_ALLOWLIST]


def prepare_transactions(transactions: pd.DataFrame) -> pd.DataFrame:
    """Validate outcomes separately, then order observations deterministically in UTC."""
    data = validate_observations(transactions)
    if TARGET_COLUMN not in transactions:
        raise ValueError(f"Missing target column: {TARGET_COLUMN}")
    labels = (
        transactions[TARGET_COLUMN]
        .astype(str)
        .str.lower()
        .map({"true": 1, "false": 0, "1": 1, "0": 0})
    )
    if labels.isna().any():
        raise ValueError(f"{TARGET_COLUMN} must contain only booleans or binary 0/1 labels.")
    data[TARGET_COLUMN] = labels.astype(int)
    return data.sort_values(["transaction_timestamp", "transaction_id"], kind="stable").reset_index(
        drop=True
    )


def chronological_split(ordered: pd.DataFrame, config: EvaluationConfig) -> dict[str, pd.DataFrame]:
    """Split whole timestamp groups; never shuffle or stratify across time."""
    timestamps = ordered["transaction_timestamp"]
    if not timestamps.is_monotonic_increasing:
        raise ValueError("Transactions must be chronologically ordered before splitting.")
    count = len(ordered)
    train_end = max(1, int(count * config.train_fraction))
    while train_end < count and timestamps.iloc[train_end] == timestamps.iloc[train_end - 1]:
        train_end += 1
    validation_end = max(
        train_end + 1, int(count * (config.train_fraction + config.validation_fraction))
    )
    while validation_end < count and (
        timestamps.iloc[validation_end] == timestamps.iloc[validation_end - 1]
    ):
        validation_end += 1
    partitions = {
        "train": ordered.iloc[:train_end].copy(),
        "validation": ordered.iloc[train_end:validation_end].copy(),
        "test": ordered.iloc[validation_end:].copy(),
    }
    for name, partition in partitions.items():
        if partition.empty:
            raise ValueError(f"{name} partition is empty; need three distinct time windows.")
        if set(partition[TARGET_COLUMN].unique()) != {0, 1}:
            raise ValueError(f"{name} partition must contain both target classes.")
    return partitions
