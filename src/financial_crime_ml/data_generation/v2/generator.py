"""Seeded entity histories, observable episodes, then separate synthetic outcomes."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from financial_crime_ml.data_generation.v2.contracts import (
    COUNTRIES,
    CURRENCY_EXPONENTS,
    SCHEMAS,
    SyntheticConfig,
    format_amount,
    format_utc,
    parse_utc,
)


def generate_v2(config: SyntheticConfig) -> dict[str, pd.DataFrame]:
    """Generate observations without passing target/helper labels into feature functions."""
    rng = np.random.default_rng(config.seed)
    start, end = parse_utc(config.event_start), parse_utc(config.event_end)
    duration = int((end - start).total_seconds())
    customers = []
    for index in range(config.customers):
        customers.append(
            {
                "customer_id": f"C2-{index:08d}",
                "country": str(rng.choice(COUNTRIES)),
                "onboarded_at": format_utc(start - timedelta(days=int(rng.integers(30, 731)))),
            }
        )
    accounts = []
    profiles = []
    for index in range(config.accounts):
        owner = customers[index % config.customers]
        onboarded = parse_utc(owner["onboarded_at"])
        opening_offset = int(rng.integers(0, int((start - onboarded).total_seconds())))
        currency = str(rng.choice(config.currencies))
        accounts.append(
            {
                "account_id": f"A2-{index:08d}",
                "customer_id": owner["customer_id"],
                "base_currency": currency,
                "opened_at": format_utc(onboarded + timedelta(seconds=opening_offset)),
            }
        )
        profiles.append(int(rng.integers(10, 401)) * 10 ** CURRENCY_EXPONENTS[currency])

    beneficiaries = []
    account_beneficiaries: dict[str, list[dict]] = {row["account_id"]: [] for row in accounts}
    for index in range(config.beneficiaries):
        account = accounts[index % config.accounts]
        opened = parse_utc(account["opened_at"])
        # First beneficiary per account is available before the event window; extras can be new.
        last = start if index < config.accounts else start + timedelta(seconds=duration * 4 // 5)
        seen = opened + timedelta(
            seconds=int(rng.integers(0, int((last - opened).total_seconds())))
        )
        row = {
            "beneficiary_id": f"B2-{index:08d}",
            "account_id": account["account_id"],
            "beneficiary_country": str(rng.choice(COUNTRIES)),
            "first_seen_at": format_utc(seen),
        }
        beneficiaries.append(row)
        account_beneficiaries[account["account_id"]].append(row)
    devices = []
    customer_devices: dict[str, list[dict]] = {row["customer_id"]: [] for row in customers}
    for index in range(config.devices):
        owner = customers[index % config.customers]
        onboarded = parse_utc(owner["onboarded_at"])
        last = start if index < config.customers else start + timedelta(seconds=duration * 4 // 5)
        seen = onboarded + timedelta(
            seconds=int(rng.integers(0, int((last - onboarded).total_seconds())))
        )
        row = {
            "device_id": f"D2-{index:08d}",
            "customer_id": owner["customer_id"],
            "ip_country": owner["country"]
            if index < config.customers
            else str(rng.choice(COUNTRIES)),
            "first_seen_at": format_utc(seen),
        }
        devices.append(row)
        customer_devices[owner["customer_id"]].append(row)

    events = []
    episode_number = 0

    def emit_episode(kind: str, count: int) -> list[dict]:
        """Create concrete observations: same-account bursts, repetitions or amount changes."""
        nonlocal episode_number
        episode_number += 1
        account_index = int(rng.integers(config.accounts))
        account = accounts[account_index]
        timestamp = start + timedelta(seconds=int(rng.integers(0, duration - 1800)))
        eligible_beneficiaries = [
            row
            for row in account_beneficiaries[account["account_id"]]
            if row["first_seen_at"] <= format_utc(timestamp)
        ]
        eligible_devices = [
            row
            for row in customer_devices[account["customer_id"]]
            if row["first_seen_at"] <= format_utc(timestamp)
        ]
        beneficiary = (
            eligible_beneficiaries[0]
            if rng.random() < 0.8
            else eligible_beneficiaries[int(rng.integers(len(eligible_beneficiaries)))]
        )
        device = (
            eligible_devices[0]
            if rng.random() < 0.8
            else eligible_devices[int(rng.integers(len(eligible_devices)))]
        )
        currency = account["base_currency"]
        scale = 10 ** CURRENCY_EXPONENTS[currency]
        round_amount = int(rng.choice([100, 200, 500])) * scale
        rows = []
        for offset in range(count):
            amount = max(1, profiles[account_index] * int(rng.integers(20, 201)) // 100)
            if kind == "amount_spike":
                amount = profiles[account_index] * int(rng.integers(8, 16))
            elif kind == "round_payments":
                amount = round_amount
            row = {
                "customer_id": account["customer_id"],
                "account_id": account["account_id"],
                "beneficiary_id": beneficiary["beneficiary_id"],
                "device_id": device["device_id"],
                "transaction_timestamp": format_utc(
                    timestamp
                    + timedelta(seconds=offset * (300 if kind == "round_payments" else 60))
                ),
                "amount_minor": amount,
                "amount": format_amount(amount, currency),
                "currency": currency,
                "transaction_type": str(rng.choice(["card_payment", "bank_transfer"])),
                "merchant_category": str(
                    rng.choice(["grocery", "travel", "electronics", "professional_services"])
                ),
                "origin_country": device["ip_country"],
                "destination_country": beneficiary["beneficiary_country"],
                "channel": str(rng.choice(["web", "mobile", "branch"], p=[0.45, 0.5, 0.05])),
            }
            rows.append(row)
        return rows

    def add_episode(kind: str, count: int, label: int, scenario: str) -> None:
        observations = emit_episode(kind, count)
        # Outcomes are attached after observations exist, never emitted as predictor flags.
        for observation in observations:
            events.append(
                (
                    observation,
                    {
                        "is_suspicious": label,
                        "episode_id": f"E2-{episode_number:08d}",
                        "scenario_type": scenario,
                    },
                )
            )

    benign_count = config.transactions - config.fraud_count
    # Benign controls share the same burst/repetition/spike mechanisms and amount ranges.
    control_budget = benign_count // 10
    while control_budget >= 7:
        add_episode("velocity_burst", 4, 0, "benign_velocity_burst")
        add_episode("round_payments", 3, 0, "benign_round_payments")
        control_budget -= 7
        benign_count -= 7
    for _ in range(benign_count):
        kind = "amount_spike" if rng.random() < 0.1 else "background"
        add_episode(kind, 1, 0, "benign_amount_spike" if kind == "amount_spike" else "background")
    remaining = config.fraud_count
    while remaining:
        for kind, count in (("velocity_burst", 4), ("round_payments", 3), ("amount_spike", 1)):
            if remaining >= count:
                add_episode(kind, count, 1, kind)
                remaining -= count
    # Assign IDs after chronological sorting; provenance stays out of observations.
    events.sort(key=lambda item: item[0]["transaction_timestamp"])
    transactions, outcomes = [], []
    for index, (observation, outcome) in enumerate(events):
        identifier = f"T2-{index:08d}"
        transactions.append({"transaction_id": identifier, **observation})
        outcomes.append({"transaction_id": identifier, **outcome})
    data = {
        name: pd.DataFrame(rows, columns=SCHEMAS[name])
        for name, rows in {
            "customers": customers,
            "accounts": accounts,
            "beneficiaries": beneficiaries,
            "devices": devices,
            "transactions": transactions,
            "outcomes": outcomes,
        }.items()
    }
    data["evaluation_transactions"] = data["transactions"].merge(
        data["outcomes"][["transaction_id", "is_suspicious"]],
        on="transaction_id",
        validate="one_to_one",
        how="left",
    )
    return data
