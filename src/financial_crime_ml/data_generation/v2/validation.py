"""Strict v2 validation with counted, located failures instead of silent repairs."""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

from financial_crime_ml.data_generation.v2.contracts import (
    CURRENCY_EXPONENTS,
    ENUMS,
    FRAUD_SCENARIOS,
    MAX_AMOUNT_MINOR,
    RELATIONSHIPS,
    SCHEMAS,
    VERSION,
    SyntheticConfig,
    format_amount,
    parse_utc,
)


class DataIntegrityError(ValueError):
    """Carry the full validation report for failed datasets."""

    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__(f"Synthetic v2 validation failed: {report['issue_count']} failed checks.")


def load_v2(
    directory: Path, *, contents: dict[str, bytes] | None = None
) -> dict[str, pd.DataFrame]:
    """Preserve lexical money/timestamps; never coerce invalid tokens into valid values."""
    return {
        name: pd.read_csv(
            directory / f"{name}.csv" if contents is None else BytesIO(contents[f"{name}.csv"]),
            dtype=str,
            keep_default_na=False,
        )
        for name in SCHEMAS
    }


def validate_v2(data: dict[str, pd.DataFrame], config: SyntheticConfig) -> dict[str, Any]:
    """Validate closed schemas, ownership, chronology, money, outcomes and M1 export."""
    issues: list[dict[str, Any]] = []
    checks: dict[str, int] = {}
    usable = {}
    timestamps = {}

    def record(table: str, rule: str, mask: pd.Series | int, detail: str) -> None:
        count = int(mask if isinstance(mask, int) else mask.sum())
        checks[f"{table}.{rule}"] = count
        if count:
            samples = []
            if isinstance(mask, pd.Series) and table in data:
                key = next(iter(SCHEMAS[table]))
                if key in data[table]:
                    samples = data[table].loc[mask, key].astype(str).head(10).tolist()
            issues.append(
                {
                    "dataset": table,
                    "check": rule,
                    "count": count,
                    "sample_ids": samples,
                    "detail": detail,
                }
            )

    def valid_minor(value: Any) -> bool:
        text = str(value)
        return bool(re.fullmatch(r"[1-9]\d{0,9}", text)) and int(text) <= MAX_AMOUNT_MINOR

    for extra in sorted(set(data) - set(SCHEMAS)):
        record(extra, "unexpected_table", 1, "Table is outside the closed v2 contract.")
    for name, schema in SCHEMAS.items():
        frame = data.get(name)
        if not isinstance(frame, pd.DataFrame):
            record(name, "required_table", 1, "Required table is missing or is not a DataFrame.")
            continue
        missing = set(schema) - set(frame.columns)
        extra = set(frame.columns) - set(schema)
        duplicates = int(frame.columns.duplicated().sum())
        record(
            name,
            "columns",
            len(missing) + len(extra) + duplicates,
            f"Missing: {sorted(missing)}; unexpected: {sorted(extra)}; "
            f"duplicate headers: {duplicates}",
        )
        if missing or duplicates:
            continue
        if frame.empty:
            record(name, "nonempty_table", 1, "Contract tables must not be empty.")
            continue
        usable[name] = frame
        key = next(iter(schema))
        record(name, "unique_primary_key", frame[key].duplicated(), "Primary keys must be unique.")
        expected = (
            config.transactions
            if name in ("transactions", "outcomes", "evaluation_transactions")
            else getattr(config, name)
        )
        record(
            name,
            "configured_volume",
            int(len(frame) != expected),
            f"Expected {expected} rows; observed {len(frame)}.",
        )
        for column, kind in schema.items():
            values = frame[column]
            if kind.startswith("id:"):
                prefix = re.escape(kind.removeprefix("id:"))
                valid = values.map(
                    lambda value, prefix=prefix: (
                        isinstance(value, str) and bool(re.fullmatch(prefix + r"\d{8}", value))
                    )
                )
            elif kind == "timestamp":

                def is_timestamp(value: Any) -> bool:
                    try:
                        parse_utc(value)
                        return True
                    except (ValueError, TypeError):
                        return False

                valid = values.map(is_timestamp)
                timestamps[name, column] = pd.to_datetime(
                    values.where(valid), format="%Y-%m-%dT%H:%M:%SZ", errors="coerce", utc=True
                )
            elif kind == "minor":
                valid = values.map(valid_minor)
            elif kind == "decimal":
                valid = values.map(
                    lambda value: (
                        isinstance(value, str) and bool(re.fullmatch(r"\d+(?:\.\d+)?", value))
                    )
                )
            else:
                valid = values.map(lambda value, kind=kind: str(value) in ENUMS[kind])
            record(name, f"type_{column}", ~valid, f"Required contract type: {kind}.")
            if kind == "currency":
                record(
                    name,
                    f"configured_{column}",
                    ~values.isin(config.currencies),
                    "Currency must belong to the configured run subset.",
                )

    for child, foreign_key, parent, primary_key in RELATIONSHIPS:
        if child in usable and parent in usable:
            record(
                child,
                f"foreign_key_{foreign_key}_{parent}",
                ~usable[child][foreign_key].isin(usable[parent][primary_key]),
                f"Every {foreign_key} must reference {parent}.{primary_key}.",
            )

    def parent_values(child: str, foreign_key: str, parent: str, value: str) -> pd.Series:
        key = next(iter(SCHEMAS[parent]))
        lookup = usable[parent].drop_duplicates(key).set_index(key, drop=False)[value]
        return usable[child][foreign_key].map(lookup)

    def relation_equal(child: str, own: str, foreign_key: str, parent: str, value: str) -> None:
        if child in usable and parent in usable:
            record(
                child,
                f"matches_{own}_{parent}_{value}",
                ~usable[child][own]
                .astype(str)
                .eq(parent_values(child, foreign_key, parent, value).astype(str)),
                f"{own} must match {parent}.{value} for the referenced entity.",
            )

    relation_equal("transactions", "customer_id", "account_id", "accounts", "customer_id")
    relation_equal("transactions", "account_id", "beneficiary_id", "beneficiaries", "account_id")
    relation_equal("transactions", "customer_id", "device_id", "devices", "customer_id")
    relation_equal("transactions", "currency", "account_id", "accounts", "base_currency")
    relation_equal("transactions", "origin_country", "device_id", "devices", "ip_country")
    relation_equal(
        "transactions",
        "destination_country",
        "beneficiary_id",
        "beneficiaries",
        "beneficiary_country",
    )

    start, end = (
        pd.Timestamp(parse_utc(config.event_start)),
        pd.Timestamp(parse_utc(config.event_end)),
    )
    for (name, column), times in timestamps.items():
        record(
            name, f"before_cutoff_{column}", times.ge(end), "All timestamps must precede event_end."
        )
        if name in ("transactions", "evaluation_transactions"):
            record(
                name,
                "event_start",
                times.lt(start),
                "Transactions must be inside the event window.",
            )

    def not_before(child: str, time: str, foreign_key: str, parent: str, parent_time: str) -> None:
        if child in usable and parent in usable:
            earlier = pd.to_datetime(
                parent_values(child, foreign_key, parent, parent_time),
                format="%Y-%m-%dT%H:%M:%SZ",
                errors="coerce",
                utc=True,
            )
            record(
                child,
                f"not_before_{parent}_{parent_time}",
                timestamps[child, time].lt(earlier),
                f"{time} cannot precede referenced {parent}.{parent_time}.",
            )

    not_before("accounts", "opened_at", "customer_id", "customers", "onboarded_at")
    not_before("beneficiaries", "first_seen_at", "account_id", "accounts", "opened_at")
    not_before("devices", "first_seen_at", "customer_id", "customers", "onboarded_at")
    for parent, key, time in (
        ("customers", "customer_id", "onboarded_at"),
        ("accounts", "account_id", "opened_at"),
        ("beneficiaries", "beneficiary_id", "first_seen_at"),
        ("devices", "device_id", "first_seen_at"),
    ):
        not_before("transactions", "transaction_timestamp", key, parent, time)

    if "transactions" in usable:
        times = timestamps["transactions", "transaction_timestamp"]
        record(
            "transactions",
            "chronological_order",
            int(not times.is_monotonic_increasing),
            "Transaction rows must be ordered by event time.",
        )
    for name in ("transactions", "evaluation_transactions"):
        if name not in usable:
            continue
        frame = usable[name]
        invalid = frame.apply(
            lambda row: (
                not (
                    valid_minor(row.amount_minor)
                    and isinstance(row.currency, str)
                    and row.currency in CURRENCY_EXPONENTS
                    and isinstance(row.amount, str)
                    and row.amount == format_amount(int(str(row.amount_minor)), row.currency)
                )
            ),
            axis=1,
        )
        record(
            name,
            "exact_money",
            invalid,
            "amount must be the canonical decimal string for positive amount_minor in currency.",
        )

    for name in ("outcomes", "evaluation_transactions"):
        if name in usable and "transactions" in usable:
            missing = ~usable["transactions"].transaction_id.isin(usable[name].transaction_id)
            record(
                "transactions",
                f"covered_by_{name}",
                missing,
                "Every observation needs exactly one outcome and one M1 export row.",
            )
    if "evaluation_transactions" in usable and "transactions" in usable:
        for column in SCHEMAS["transactions"]:
            reference = parent_values(
                "evaluation_transactions", "transaction_id", "transactions", column
            )
            record(
                "evaluation_transactions",
                f"observation_{column}",
                ~usable["evaluation_transactions"][column].astype(str).eq(reference.astype(str)),
                "Export must reproduce the source observation exactly.",
            )
    relation_equal(
        "evaluation_transactions", "is_suspicious", "transaction_id", "outcomes", "is_suspicious"
    )

    fraud_count = 0
    scenario_counts = {}
    if "outcomes" in usable:
        outcomes = usable["outcomes"]
        labels = outcomes.is_suspicious.astype(str)
        fraud_count = int(labels.eq("1").sum())
        scenario_counts = {str(k): int(v) for k, v in outcomes.scenario_type.value_counts().items()}
        record(
            "outcomes",
            "configured_fraud_count",
            int(fraud_count != config.fraud_count),
            f"Expected {config.fraud_count} suspicious outcomes; found {fraud_count}.",
        )
        record(
            "outcomes",
            "scenario_outcome",
            labels.eq("1").ne(outcomes.scenario_type.isin(FRAUD_SCENARIOS)),
            "Outcome must describe the injected scenario; it is never a predictor.",
        )
        if "transactions" in usable:
            tx = (
                usable["transactions"].drop_duplicates("transaction_id").set_index("transaction_id")
            )
            bad = []
            for episode, group in outcomes.groupby("episode_id", sort=True):
                kind = str(group.scenario_type.iloc[0]).removeprefix("benign_")
                size = {"velocity_burst": 4, "round_payments": 3}.get(kind, 1)
                valid = group.scenario_type.nunique() == 1 and group.is_suspicious.nunique() == 1
                if size == 1:
                    if not valid or len(group) != 1:
                        bad.append(str(episode))
                    continue
                rows = tx.reindex(group.transaction_id)
                valid = valid and len(rows) == size and rows.account_id.nunique() == 1
                if size > 1:
                    times = pd.to_datetime(
                        rows.transaction_timestamp,
                        format="%Y-%m-%dT%H:%M:%SZ",
                        errors="coerce",
                        utc=True,
                    )
                    valid = valid and times.notna().all() and times.nunique() == size
                    valid = (
                        valid
                        and (times.max() - times.min()).total_seconds()
                        <= (180 if kind == "velocity_burst" else 600)
                        and rows.device_id.nunique() == rows.beneficiary_id.nunique() == 1
                    )
                if kind == "round_payments":
                    valid = (
                        valid
                        and rows.amount_minor.nunique() == 1
                        and rows.apply(
                            lambda row: (
                                valid_minor(row.amount_minor)
                                and row.currency in CURRENCY_EXPONENTS
                                and int(str(row.amount_minor))
                                % 10 ** CURRENCY_EXPONENTS[row.currency]
                                == 0
                            ),
                            axis=1,
                        ).all()
                    )
                if not valid:
                    bad.append(str(episode))
            record(
                "outcomes",
                "observable_episode_structure",
                len(bad),
                f"Invalid episode sizes, references, time spans or repeated amounts: {bad[:10]}",
            )

    totals = {}
    if "transactions" in usable:
        for currency, group in usable["transactions"].groupby("currency"):
            totals[str(currency)] = sum(int(str(v)) for v in group.amount_minor if valid_minor(v))
    return {
        "contract_version": VERSION,
        "status": "failed" if issues else "passed",
        "issue_count": len(issues),
        "issues": issues,
        "checks": checks,
        "row_counts": {
            name: len(frame) for name, frame in data.items() if isinstance(frame, pd.DataFrame)
        },
        "suspicious_transaction_count": fraud_count,
        "realised_prevalence": fraud_count / config.transactions,
        "scenario_counts": scenario_counts,
        "amount_minor_totals_by_currency": totals,
        "cross_currency_total": None,
    }
