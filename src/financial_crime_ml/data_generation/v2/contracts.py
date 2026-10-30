"""Closed v2 schemas, UTC time bounds and exact account-currency money rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from math import isfinite
from typing import Any

VERSION = "2.0.0"
CURRENCY_EXPONENTS = {"GBP": 2, "USD": 2, "EUR": 2, "SGD": 2, "AED": 2, "JPY": 0}
MAX_AMOUNT_MINOR = 1_000_000_000
COUNTRIES = ("GB", "US", "DE", "FR", "NL", "SG")
SCENARIOS = (
    "background",
    "benign_velocity_burst",
    "benign_round_payments",
    "benign_amount_spike",
    "velocity_burst",
    "round_payments",
    "amount_spike",
)
FRAUD_SCENARIOS = ("velocity_burst", "round_payments", "amount_spike")

# Every field is required; no extra columns (especially predictor helper labels).
SCHEMAS = {
    "customers": {
        "customer_id": "id:C2-",
        "country": "country",
        "onboarded_at": "timestamp",
    },
    "accounts": {
        "account_id": "id:A2-",
        "customer_id": "id:C2-",
        "base_currency": "currency",
        "opened_at": "timestamp",
    },
    "beneficiaries": {
        "beneficiary_id": "id:B2-",
        "account_id": "id:A2-",
        "beneficiary_country": "country",
        "first_seen_at": "timestamp",
    },
    "devices": {
        "device_id": "id:D2-",
        "customer_id": "id:C2-",
        "ip_country": "country",
        "first_seen_at": "timestamp",
    },
    "transactions": {
        "transaction_id": "id:T2-",
        "customer_id": "id:C2-",
        "account_id": "id:A2-",
        "beneficiary_id": "id:B2-",
        "device_id": "id:D2-",
        "transaction_timestamp": "timestamp",
        "amount_minor": "minor",
        "amount": "decimal",
        "currency": "currency",
        "transaction_type": "transaction_type",
        "merchant_category": "merchant_category",
        "origin_country": "country",
        "destination_country": "country",
        "channel": "channel",
    },
    "outcomes": {
        "transaction_id": "id:T2-",
        "is_suspicious": "binary",
        "episode_id": "id:E2-",
        "scenario_type": "scenario",
    },
}
SCHEMAS["evaluation_transactions"] = {**SCHEMAS["transactions"], "is_suspicious": "binary"}
ENUMS = {
    "country": COUNTRIES,
    "currency": tuple(CURRENCY_EXPONENTS),
    "binary": ("0", "1"),
    "scenario": SCENARIOS,
    "transaction_type": ("card_payment", "bank_transfer"),
    "merchant_category": ("grocery", "travel", "electronics", "professional_services"),
    "channel": ("web", "mobile", "branch"),
}
RELATIONSHIPS = (
    ("accounts", "customer_id", "customers", "customer_id"),
    ("beneficiaries", "account_id", "accounts", "account_id"),
    ("devices", "customer_id", "customers", "customer_id"),
    ("transactions", "customer_id", "customers", "customer_id"),
    ("transactions", "account_id", "accounts", "account_id"),
    ("transactions", "beneficiary_id", "beneficiaries", "beneficiary_id"),
    ("transactions", "device_id", "devices", "device_id"),
    ("outcomes", "transaction_id", "transactions", "transaction_id"),
    ("evaluation_transactions", "transaction_id", "transactions", "transaction_id"),
)


def parse_utc(value: str) -> datetime:
    """Accept only explicit UTC Z timestamps with whole-second precision."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value):
        raise ValueError("Timestamp must be YYYY-MM-DDTHH:MM:SSZ (UTC, whole seconds).")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def format_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_amount(minor: int, currency: str) -> str:
    """Convert integer minor units to a canonical decimal string, without float arithmetic."""
    exponent = CURRENCY_EXPONENTS[currency]
    scale = 10**exponent
    whole, fraction = divmod(minor, scale)
    return f"{whole}.{fraction:0{exponent}d}" if exponent else str(whole)


@dataclass(frozen=True)
class SyntheticConfig:
    contract_version: str = VERSION
    seed: int = 42
    customers: int = 100
    accounts: int = 150
    beneficiaries: int = 300
    devices: int = 200
    transactions: int = 5000
    fraud_prevalence: float = 0.08
    event_start: str = "2025-01-01T00:00:00Z"
    event_end: str = "2026-01-01T00:00:00Z"
    currencies: tuple[str, ...] = ("GBP", "USD", "EUR", "JPY")

    def __post_init__(self) -> None:
        if self.contract_version != VERSION:
            raise ValueError(f"Only contract_version {VERSION} is supported.")
        if type(self.seed) is not int or not 0 <= self.seed < 2**32:
            raise ValueError("seed must be an integer in [0, 2**32).")
        for name in ("customers", "accounts", "beneficiaries", "devices", "transactions"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 1_000_000:
                raise ValueError(f"{name} must be an integer between 1 and 1,000,000.")
        if self.accounts < self.customers or self.beneficiaries < self.accounts:
            raise ValueError("Need at least one account per customer and beneficiary per account.")
        if self.devices < self.customers:
            raise ValueError("Need at least one device per customer.")
        if (
            isinstance(self.fraud_prevalence, bool)
            or not isinstance(self.fraud_prevalence, (int, float))
            or not isfinite(self.fraud_prevalence)
            or not 0 <= self.fraud_prevalence <= 1
        ):
            raise ValueError("fraud_prevalence must be finite and between 0 and 1.")
        if (parse_utc(self.event_end) - parse_utc(self.event_start)).total_seconds() < 86400:
            raise ValueError(
                "event_end must be at least one day after event_start (exclusive end)."
            )
        if (
            not self.currencies
            or len(set(self.currencies)) != len(self.currencies)
            or any(currency not in CURRENCY_EXPONENTS for currency in self.currencies)
        ):
            raise ValueError("currencies must be a nonempty unique subset of supported currencies.")

    @property
    def fraud_count(self) -> int:
        return int(
            (Decimal(str(self.fraud_prevalence)) * self.transactions).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )


def config_from_mapping(raw: Any) -> SyntheticConfig:
    if not isinstance(raw, dict):
        raise ValueError("Synthetic configuration must be a mapping.")
    unknown = set(raw) - set(SyntheticConfig.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unsupported synthetic settings: {sorted(unknown)}")
    settings = dict(raw)
    if "currencies" in settings:
        settings["currencies"] = tuple(settings["currencies"])
    return SyntheticConfig(**settings)


def contract_description() -> dict[str, Any]:
    return {
        "contract_version": VERSION,
        "schemas": SCHEMAS,
        "enums": ENUMS,
        "relationships": RELATIONSHIPS,
        "currency_exponents": CURRENCY_EXPONENTS,
        "money": {
            "max_amount_minor": MAX_AMOUNT_MINOR,
            "positive_only": True,
            "currency_equals_account_base": True,
            "fx_conversion": False,
        },
        "time": {
            "format": "YYYY-MM-DDTHH:MM:SSZ",
            "end_exclusive": True,
            "naive_timestamps_allowed": False,
        },
        "outcomes_are_predictors": False,
    }
