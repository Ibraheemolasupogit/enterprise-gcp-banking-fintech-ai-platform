"""Determinism and adversarial temporal, relationship, precision and feature checks."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from financial_crime_ml.data_generation.v2.contracts import (
    CURRENCY_EXPONENTS,
    MAX_AMOUNT_MINOR,
    SyntheticConfig,
    format_amount,
    format_utc,
    parse_utc,
)
from financial_crime_ml.data_generation.v2.generator import generate_v2
from financial_crime_ml.data_generation.v2.validation import validate_v2
from financial_crime_ml.evaluation.contracts import FEATURE_ALLOWLIST, build_transaction_features


@pytest.fixture
def config():
    return SyntheticConfig(
        customers=6,
        accounts=10,
        beneficiaries=20,
        devices=12,
        transactions=300,
        fraud_prevalence=0.12,
    )


@pytest.fixture
def data(config):
    return generate_v2(config)


def assert_failure(data, config, check):
    report = validate_v2(data, config)
    assert report["status"] == "failed"
    matches = [issue for issue in report["issues"] if issue["check"] == check]
    assert matches, report["issues"]
    assert all(issue["count"] > 0 and issue["detail"] for issue in matches)
    return matches


def test_valid_bundle_is_complete_and_deterministic(data, config):
    report = validate_v2(data, config)
    assert report["status"] == "passed", report["issues"]
    assert sum(report["checks"].values()) == 0
    assert report["suspicious_transaction_count"] == 36
    assert report["realised_prevalence"] == 0.12
    assert report["cross_currency_total"] is None
    repeated = generate_v2(config)
    for name in data:
        assert_frame_equal(data[name], repeated[name])
    changed = generate_v2(replace(config, seed=7))
    assert not data["transactions"].equals(changed["transactions"])


def test_behavioural_episodes_have_observable_structure_and_benign_controls(data):
    joined = data["transactions"].merge(data["outcomes"], on="transaction_id")
    for scenario, count, span in (("velocity_burst", 4, 180), ("round_payments", 3, 600)):
        for prefix in ("", "benign_"):
            groups = list(joined[joined.scenario_type.eq(prefix + scenario)].groupby("episode_id"))
            assert groups
            for _, group in groups:
                assert len(group) == count
                assert group.account_id.nunique() == group.beneficiary_id.nunique() == 1
                times = pd.to_datetime(group.transaction_timestamp, utc=True)
                assert (times.max() - times.min()).total_seconds() == span
                if scenario == "round_payments":
                    assert group.amount.nunique() == 1
                    assert Decimal(group.amount.iloc[0]) % 1 == 0
    assert {"amount_spike", "benign_amount_spike"} <= set(joined.scenario_type)


def test_outcomes_are_not_observations_and_cannot_change_m1_features(data):
    observations = data["transactions"]
    assert not {"is_suspicious", "episode_id", "scenario_type", "suspicious_pattern"} & set(
        observations.columns
    )
    expected = build_transaction_features(observations)
    evaluation = data["evaluation_transactions"].copy()
    evaluation["is_suspicious"] = 1 - evaluation.is_suspicious
    assert_frame_equal(expected, build_transaction_features(evaluation))
    assert tuple(expected.columns) == FEATURE_ALLOWLIST
    earlier = observations.iloc[:100]
    assert_frame_equal(build_transaction_features(earlier), expected.iloc[:100])


@pytest.mark.parametrize(
    "field",
    [
        "suspicious_pattern",
        "account_suspicious_transaction_rate",
        "fraud_probability",
    ],
)
def test_uncontracted_predictor_fields_are_rejected(data, config, field):
    data["transactions"][field] = 1
    assert_failure(data, config, "columns")


@pytest.mark.parametrize(
    "table,key,parent,time",
    [
        ("customers", "customer_id", "customers", "onboarded_at"),
        ("accounts", "account_id", "accounts", "opened_at"),
        ("beneficiaries", "beneficiary_id", "beneficiaries", "first_seen_at"),
        ("devices", "device_id", "devices", "first_seen_at"),
    ],
)
def test_referenced_entity_cannot_be_created_after_event(data, config, table, key, parent, time):
    transaction = data["transactions"].iloc[0]
    identifier = transaction[key]
    future = format_utc(parse_utc(transaction.transaction_timestamp) + timedelta(seconds=1))
    data[table].loc[data[table][key].eq(identifier), time] = future
    issues = assert_failure(data, config, f"not_before_{parent}_{time}")
    assert any(
        transaction.transaction_id in issue["sample_ids"]
        for issue in issues
        if issue["dataset"] == "transactions"
    )


@pytest.mark.parametrize(
    "table,key,parent,foreign,time,parent_time",
    [
        ("accounts", "account_id", "customers", "customer_id", "opened_at", "onboarded_at"),
        ("beneficiaries", "beneficiary_id", "accounts", "account_id", "first_seen_at", "opened_at"),
        ("devices", "device_id", "customers", "customer_id", "first_seen_at", "onboarded_at"),
    ],
)
def test_entity_creation_chain_cannot_run_backwards(
    data, config, table, key, parent, foreign, time, parent_time
):
    row = data[table].iloc[0]
    parent_key = next(iter(data[parent].columns))
    timestamp = data[parent].loc[data[parent][parent_key].eq(row[foreign]), parent_time].item()
    data[table].loc[data[table][key].eq(row[key]), time] = format_utc(
        parse_utc(timestamp) - timedelta(seconds=1)
    )
    assert_failure(data, config, f"not_before_{parent}_{parent_time}")


@pytest.mark.parametrize(
    "table,column,parent,prefix",
    [
        ("accounts", "customer_id", "customers", "C2-"),
        ("beneficiaries", "account_id", "accounts", "A2-"),
        ("devices", "customer_id", "customers", "C2-"),
        ("transactions", "customer_id", "customers", "C2-"),
        ("transactions", "account_id", "accounts", "A2-"),
        ("transactions", "beneficiary_id", "beneficiaries", "B2-"),
        ("transactions", "device_id", "devices", "D2-"),
        ("outcomes", "transaction_id", "transactions", "T2-"),
        ("evaluation_transactions", "transaction_id", "transactions", "T2-"),
    ],
)
def test_invalid_foreign_keys_produce_counted_reports(data, config, table, column, parent, prefix):
    data[table].loc[0, column] = prefix + "99999999"
    assert_failure(data, config, f"foreign_key_{column}_{parent}")


@pytest.mark.parametrize(
    "column,parent,parent_key,lookup,check",
    [
        (
            "customer_id",
            "customers",
            "customer_id",
            None,
            "matches_customer_id_accounts_customer_id",
        ),
        (
            "beneficiary_id",
            "beneficiaries",
            "beneficiary_id",
            "account_id",
            "matches_account_id_beneficiaries_account_id",
        ),
        (
            "device_id",
            "devices",
            "device_id",
            "customer_id",
            "matches_customer_id_devices_customer_id",
        ),
    ],
)
def test_valid_foreign_keys_still_require_correct_ownership(
    data, config, column, parent, parent_key, lookup, check
):
    tx = data["transactions"].iloc[0]
    owner_column = lookup or parent_key
    expected = tx[lookup or "customer_id"]
    other = data[parent].loc[data[parent][owner_column].ne(expected)].iloc[0]
    data["transactions"].loc[0, column] = other[parent_key]
    assert_failure(data, config, check)


@pytest.mark.parametrize(
    "value",
    [
        "2025-01-01",
        "2025-01-01T00:00:00",
        "2025-01-01T00:00:00+00:00",
        "2025-01-01T01:00:00+01:00",
        "2025-01-01T00:00:00.001Z",
        "2025-02-30T00:00:00Z",
    ],
)
def test_utc_whole_second_lexical_contract(data, config, value):
    data["transactions"].loc[0, "transaction_timestamp"] = value
    assert_failure(data, config, "type_transaction_timestamp")


def test_event_window_is_bounded_and_end_exclusive(data, config):
    data["transactions"].loc[0, "transaction_timestamp"] = config.event_end
    assert_failure(data, config, "before_cutoff_transaction_timestamp")
    data["transactions"].loc[0, "transaction_timestamp"] = format_utc(
        parse_utc(config.event_start) - timedelta(seconds=1)
    )
    assert_failure(data, config, "event_start")


@pytest.mark.parametrize("value", [0, -1, "1.0", "01", MAX_AMOUNT_MINOR + 1, None, True])
def test_minor_units_must_be_positive_canonical_bounded_integers(data, config, value):
    data["transactions"]["amount_minor"] = data["transactions"].amount_minor.astype(object)
    data["transactions"].loc[0, "amount_minor"] = value
    assert_failure(data, config, "type_amount_minor")


@pytest.mark.parametrize("value", ["1.001", "1e2", "NaN", "-1.00", 1.25, None])
def test_decimal_precision_and_lexical_money_cannot_be_silently_rounded(data, config, value):
    data["transactions"].loc[0, "amount"] = value
    assert_failure(data, config, "exact_money")


@pytest.mark.parametrize("currency,amount", [("GBP", "1.01"), ("JPY", "101"), ("AED", "1.01")])
def test_supported_currency_precision_is_exact(currency, amount):
    assert format_amount(101, currency) == amount
    cfg = SyntheticConfig(
        customers=1, accounts=1, beneficiaries=1, devices=1, transactions=30, currencies=(currency,)
    )
    tables = generate_v2(cfg)
    assert validate_v2(tables, cfg)["status"] == "passed"
    for row in tables["transactions"].itertuples():
        assert Decimal(row.amount) * 10 ** CURRENCY_EXPONENTS[row.currency] == row.amount_minor


def test_account_currency_and_country_references_must_match(data, config):
    tx = data["transactions"].iloc[0]
    data["transactions"].loc[0, "currency"] = "USD" if tx.currency != "USD" else "GBP"
    assert_failure(data, config, "matches_currency_accounts_base_currency")
    data["transactions"].loc[0, "currency"] = "BTC"
    assert_failure(data, config, "type_currency")
    data["transactions"].loc[0, "origin_country"] = "US" if tx.origin_country != "US" else "GB"
    assert_failure(data, config, "matches_origin_country_devices_ip_country")
    data["transactions"].loc[0, "destination_country"] = (
        "US" if tx.destination_country != "US" else "GB"
    )
    assert_failure(data, config, "matches_destination_country_beneficiaries_beneficiary_country")


def test_supported_but_unconfigured_currency_is_rejected(data, config):
    data["accounts"].loc[0, "base_currency"] = "AED"
    assert "AED" not in config.currencies
    assert_failure(data, config, "configured_base_currency")


@pytest.mark.parametrize("prevalence", [0, 0.01, 0.125, 1])
def test_prevalence_is_configurable_and_not_forced_to_balance_time_splits(config, prevalence):
    cfg = replace(config, fraud_prevalence=prevalence)
    report = validate_v2(generate_v2(cfg), cfg)
    assert report["status"] == "passed", report["issues"]
    assert report["suspicious_transaction_count"] == cfg.fraud_count
    assert cfg.fraud_count == {0: 0, 0.01: 3, 0.125: 38, 1: 300}[prevalence]


def test_missing_duplicate_empty_tables_and_export_tampering_fail(data, config):
    missing = {**data, "accounts": data["accounts"].drop(columns="opened_at")}
    assert_failure(missing, config, "columns")
    absent = {name: frame for name, frame in data.items() if name != "devices"}
    assert_failure(absent, config, "required_table")
    empty = {**data, "transactions": data["transactions"].iloc[:0]}
    assert_failure(empty, config, "nonempty_table")
    data["customers"].loc[1, "customer_id"] = data["customers"].loc[0, "customer_id"]
    assert_failure(data, config, "unique_primary_key")
    data["evaluation_transactions"].loc[0, "amount"] = "99.99"
    assert_failure(data, config, "observation_amount")


def test_broken_behavioural_episode_cannot_pass_as_meaningful_scenario(data, config):
    episode = data["outcomes"].loc[data["outcomes"].scenario_type.eq("velocity_burst")].iloc[0]
    ids = data["outcomes"].loc[data["outcomes"].episode_id.eq(episode.episode_id), "transaction_id"]
    rows = data["transactions"].transaction_id.isin(ids)
    data["transactions"].loc[rows, "transaction_timestamp"] = config.event_start
    assert_failure(data, config, "observable_episode_structure")


@pytest.mark.parametrize(
    "changes",
    [
        {"contract_version": "3"},
        {"seed": -1},
        {"customers": 0},
        {"transactions": True},
        {"accounts": 1},
        {"beneficiaries": 1},
        {"devices": 1},
        {"fraud_prevalence": -0.1},
        {"fraud_prevalence": float("nan")},
        {"fraud_prevalence": True},
        {"currencies": ("BTC",)},
        {"currencies": ("USD", "USD")},
        {"event_end": "2025-01-01T01:00:00Z"},
        {"event_start": "2025-01-01"},
    ],
)
def test_impossible_or_unsupported_configurations_are_rejected(config, changes):
    with pytest.raises(ValueError):
        replace(config, **changes)
