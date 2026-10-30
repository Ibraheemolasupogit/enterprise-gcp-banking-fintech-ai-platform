"""Leakage invariants and temporal boundaries on controlled observations."""

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from financial_crime_ml.evaluation.contracts import (
    FEATURE_ALLOWLIST,
    EvaluationConfig,
    build_transaction_features,
    chronological_split,
    prepare_transactions,
)


@pytest.fixture
def observations() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "transaction_id": [f"T{i:03d}" for i in range(60)],
            "transaction_timestamp": pd.date_range("2025-01-01", periods=60, freq="4h").astype(str),
            "amount": np.arange(60) * 7 + 10,
            "currency": ["GBP"] * 60,
            "transaction_type": ["bank_transfer"] * 60,
            "merchant_category": ["grocery"] * 60,
            "origin_country": ["GB"] * 60,
            "destination_country": ["GB", "US"] * 30,
            "channel": ["web"] * 60,
            "is_suspicious": [int(i % 4 == 0) for i in range(60)],
        }
    )


def test_feature_allowlist_is_independent_of_all_labels_and_extra_columns(observations):
    expected = build_transaction_features(observations)
    changed = observations.copy()
    changed["is_suspicious"] = 1 - changed["is_suspicious"]
    changed["suspicious_pattern"] = "mule_account_style_behaviour"
    changed["account_suspicious_transaction_rate"] = 1.0
    changed["account_total_transaction_amount"] = 1e15
    changed["mule_activity_indicator"] = 1
    assert tuple(expected.columns) == FEATURE_ALLOWLIST
    assert_frame_equal(expected, build_transaction_features(changed))
    assert_frame_equal(expected, build_transaction_features(changed.drop(columns="is_suspicious")))


def test_future_records_and_future_categories_cannot_change_earlier_features(observations):
    earlier = observations.iloc[:40].copy()
    future = observations.iloc[40:].copy()
    future["amount"] = 1e15
    future["channel"] = "unseen_future_channel"
    combined = pd.concat([earlier, future], ignore_index=True)
    assert_frame_equal(
        build_transaction_features(earlier), build_transaction_features(combined).iloc[:40]
    )
    shuffled = combined.sample(frac=1, random_state=7)
    assert_frame_equal(
        build_transaction_features(combined),
        build_transaction_features(shuffled).sort_index(),
    )


def test_chronological_split_is_disjoint_complete_and_invariant_to_input_order(observations):
    ordered = prepare_transactions(observations.sample(frac=1, random_state=7))
    assert_frame_equal(ordered, prepare_transactions(observations))
    parts = chronological_split(ordered, EvaluationConfig())
    assert [len(frame) for frame in parts.values()] == [36, 12, 12]
    assert (
        parts["train"].transaction_timestamp.max() < parts["validation"].transaction_timestamp.min()
    )
    assert (
        parts["validation"].transaction_timestamp.max() < parts["test"].transaction_timestamp.min()
    )
    ids = [set(frame.transaction_id) for frame in parts.values()]
    assert not ids[0] & ids[1] and not ids[1] & ids[2] and not ids[0] & ids[2]
    assert set.union(*ids) == set(observations.transaction_id)


def test_equal_timestamps_never_cross_partition_boundaries(observations):
    observations.loc[35:37, "transaction_timestamp"] = observations.loc[35, "transaction_timestamp"]
    parts = chronological_split(prepare_transactions(observations), EvaluationConfig())
    assert len(parts["train"]) == 38
    assert (
        parts["train"].transaction_timestamp.max() < parts["validation"].transaction_timestamp.min()
    )


@pytest.mark.parametrize(
    "partition,indices", [("train", (0, 36)), ("validation", (36, 48)), ("test", (48, 60))]
)
def test_single_class_partitions_fail_explicitly(observations, partition, indices):
    observations.loc[indices[0] : indices[1] - 1, "is_suspicious"] = 0
    with pytest.raises(ValueError, match=f"{partition} partition must contain both target classes"):
        chronological_split(prepare_transactions(observations), EvaluationConfig())


@pytest.mark.parametrize(
    "column,value",
    [
        ("amount", np.inf),
        ("amount", -1),
        ("amount", np.nan),
        ("transaction_timestamp", "invalid"),
        ("currency", ""),
        ("is_suspicious", "unknown"),
    ],
)
def test_invalid_observations_or_targets_fail(observations, column, value):
    observations[column] = observations[column].astype(object)
    observations.loc[0, column] = value
    with pytest.raises(ValueError):
        prepare_transactions(observations)


def test_duplicate_ids_and_insufficient_time_windows_fail(observations):
    duplicate = observations.copy()
    duplicate.loc[1, "transaction_id"] = duplicate.loc[0, "transaction_id"]
    with pytest.raises(ValueError, match="unique"):
        prepare_transactions(duplicate)
    observations["transaction_timestamp"] = "2025-01-01"
    with pytest.raises(ValueError, match="empty"):
        chronological_split(prepare_transactions(observations), EvaluationConfig())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"train_fraction": 0.9},
        {"validation_fraction": 0},
        {"train_fraction": float("nan")},
        {"seed": -1},
        {"threshold_candidates": ()},
        {"threshold_candidates": (0.7, 0.3)},
        {"threshold_candidates": (0.5, 0.5)},
        {"threshold_candidates": (float("nan"),)},
    ],
)
def test_invalid_configuration_fails(kwargs):
    with pytest.raises(ValueError):
        EvaluationConfig(**kwargs)
