"""Local immutable snapshot ingestion, row quarantine and currency reconciliation."""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from pathlib import Path
from uuid import uuid4

from financial_crime_ml.data_generation.v2.contracts import (
    CURRENCY_EXPONENTS,
    ENUMS,
    MAX_AMOUNT_MINOR,
    format_amount,
    parse_utc,
)
from financial_crime_ml.evaluation.manifest import (
    execution_provenance,
    sha256_bytes,
    utc_now,
    write_json,
)
from financial_crime_ml.ingestion.foundation.contracts import (
    CURATED_FIELDS,
    ENTITY_SCHEMAS,
    EVENT_FIELDS,
    LINEAGE_FIELDS,
    TABLES,
    VERSION,
    description,
    validate_snapshot,
)
from financial_crime_ml.ingestion.load_data import REPO_ROOT


def _read(content: bytes, table: str) -> list[dict]:
    """Record ordinal is one-based excluding header (not physical line for quoted CSV)."""
    reader = csv.reader(io.StringIO(content.decode("utf-8")), strict=True)
    header = next(reader, [])
    expected = list(ENTITY_SCHEMAS[table])
    valid_header = len(header) == len(expected) and set(header) == set(expected)
    records = []
    for ordinal, values in enumerate(reader, 1):
        reasons = []
        if not valid_header:
            reasons.append("schema.header")
        if len(values) != len(header):
            reasons.append("schema.row_width")
        row = dict(zip(header, values, strict=False)) if valid_header else {}
        records.append({"source_row": ordinal, "values": values, "row": row, "reasons": reasons})
    if not valid_header and not records:
        raise ValueError(f"{table}: invalid empty CSV header")
    return records


def _schema_reasons(row: dict, table: str) -> list[str]:
    reasons = []
    for field, kind in ENTITY_SCHEMAS[table].items():
        value = row.get(field)
        valid = isinstance(value, str) and bool(value)
        if valid and kind.startswith("id:"):
            valid = bool(re.fullmatch(re.escape(kind[3:]) + r"\d{8}", value))
        elif valid and kind in ENUMS:
            valid = value in ENUMS[kind]
        elif valid and kind == "timestamp":
            try:
                parse_utc(value)
            except ValueError:
                valid = False
        elif valid and kind == "minor":
            valid = bool(re.fullmatch(r"[1-9]\d{0,9}", value))
            valid = valid and int(value) <= MAX_AMOUNT_MINOR
        if not valid:
            reasons.append(f"schema.{field}")
    if table == "transactions" and not any(
        reason in reasons for reason in ("schema.amount_minor", "schema.currency")
    ):
        if row.get("amount") != format_amount(int(row["amount_minor"]), row["currency"]):
            reasons.append("money.precision")
    return reasons


def _write_csv(path: Path, fields, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _money(records: list[dict]) -> dict:
    totals, unaccountable = {}, 0
    for record in records:
        row = record["row"]
        currency, minor = row.get("currency"), row.get("amount_minor", "")
        if currency in CURRENCY_EXPONENTS and re.fullmatch(r"[1-9]\d{0,9}", minor):
            if int(minor) <= MAX_AMOUNT_MINOR:
                totals[currency] = totals.get(currency, 0) + int(minor)
                continue
        unaccountable += 1
    return {"amount_minor_by_currency": dict(sorted(totals.items())),
            "unaccountable_rows": unaccountable}


def transform(contents: dict[str, bytes], source_time: str) -> tuple[dict, dict, dict]:
    """Validate parents first; rejects cascade to children without repairing observations."""
    accepted, rejected, indexes, reconciliation = {}, {}, {}, {}
    for table in TABLES:
        records = _read(contents[table], table)
        key = next(iter(ENTITY_SCHEMAS[table]))
        counts = Counter(record["row"].get(key) for record in records)
        accepted[table], rejected[table], indexes[table] = [], [], {}
        for record in records:
            row, reasons = record["row"], record["reasons"]
            reasons.extend(_schema_reasons(row, table))
            if row.get(key) and counts[row[key]] > 1:
                reasons.append("duplicate.primary_key")
            if not reasons:
                event = row[EVENT_FIELDS[table]]
                if event > source_time:
                    reasons.append("time.after_source_snapshot")
                parents = {
                    "accounts": (("customers", "customer_id"),),
                    "beneficiaries": (("accounts", "account_id"),),
                    "devices": (("customers", "customer_id"),),
                    "transactions": (("customers", "customer_id"), ("accounts", "account_id"),
                                     ("beneficiaries", "beneficiary_id"), ("devices", "device_id")),
                }.get(table, ())
                refs = {}
                for parent, field in parents:
                    ref = indexes[parent].get(row[field])
                    if ref is None:
                        reasons.append(f"reference.{field}")
                    else:
                        refs[parent] = ref
                        if event < ref[EVENT_FIELDS[parent]]:
                            reasons.append(f"time.before_{parent}")
                if table == "transactions":
                    for parent, field in (("accounts", "customer_id"),
                                          ("devices", "customer_id"),
                                          ("beneficiaries", "account_id")):
                        if parent in refs and refs[parent][field] != row[field]:
                            reasons.append(f"ownership.{parent}")
                    for parent, left, right in (
                        ("accounts", "base_currency", "currency"),
                        ("devices", "ip_country", "origin_country"),
                        ("beneficiaries", "beneficiary_country", "destination_country"),
                    ):
                        if parent in refs and refs[parent][left] != row[right]:
                            reasons.append(f"consistency.{right}")
            if reasons:
                record["reasons"] = sorted(set(reasons))
                rejected[table].append(record)
            else:
                accepted[table].append(record)
                indexes[table][row[key]] = row
        summary = {"source_rows": len(records), "validated_rows": len(accepted[table]),
                   "rejected_rows": len(rejected[table])}
        if table == "transactions":
            summary.update(source_money=_money(records), validated_money=_money(accepted[table]),
                           rejected_money=_money(rejected[table]))
        reconciliation[table] = summary
    return accepted, rejected, reconciliation


def ingest(
    dataset_dir: Path, *, source_id: str, source_time: str, ingestion_time: str,
    run_id: str | None = None, runs_root: Path = REPO_ROOT / "outputs/runs",
) -> dict:
    """Immutable reruns: same run ID + signature returns verified existing evidence."""
    validate_snapshot(source_id, source_time, ingestion_time)
    run_id = run_id or f"foundation-{uuid4().hex}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
        raise ValueError("Invalid run_id")
    root = Path(runs_root).resolve()
    for protected in ("data/sample", "data/processed", "outputs/sample", "reports/sample"):
        if root.is_relative_to(REPO_ROOT / protected):
            raise ValueError("Outputs cannot overwrite historical samples")
    contents = {table: (Path(dataset_dir) / f"{table}.csv").read_bytes() for table in TABLES}
    inputs = {f"{table}.csv": sha256_bytes(value) for table, value in contents.items()}
    provenance = execution_provenance()
    settings = {"contract_version": VERSION, "source_id": source_id,
                "source_time": source_time, "ingestion_time": ingestion_time,
                "input_sha256": inputs,
                "source_fingerprint": provenance["source_fingerprint_sha256"]}
    signature = sha256_bytes(json.dumps(settings, sort_keys=True).encode())
    directory = root / run_id
    if directory.exists():
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest["signature"] != signature or manifest["status"] not in (
            "completed", "completed_with_rejects"
        ):
            raise ValueError("Run ID collision: settings, input or source changed; or failed run")
        actual = {str(p.relative_to(directory)): sha256_bytes(p.read_bytes())
                  for p in directory.rglob("*") if p.is_file() and p.name != "manifest.json"}
        if actual != manifest["artifacts_sha256"]:
            raise ValueError("Existing run integrity check failed")
        return {"run_dir": directory, "manifest": manifest, "reused": True}
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {**settings, **provenance, "signature": signature, "run_id": run_id,
                "status": "running", "started_at": utc_now(),
                "source_directory": str(Path(dataset_dir).resolve())}
    write_json(directory / "manifest.json", manifest)
    try:
        for stage in ("raw", "validated", "curated", "quarantine"):
            (directory / stage).mkdir()
        for table, content in contents.items():
            (directory / "raw" / f"{table}.csv").write_bytes(content)
        write_json(directory / "contract.json", description())
        accepted, rejected, totals = transform(contents, source_time)
        for table in TABLES:
            lineage_rows = []
            for record in accepted[table]:
                lineage_rows.append({**record["row"], "source_id": source_id,
                                     "source_time": source_time, "ingestion_time": ingestion_time,
                                     "source_row": record["source_row"],
                                     "source_sha256": inputs[f"{table}.csv"]})
            _write_csv(directory / "validated" / f"{table}.csv",
                       (*ENTITY_SCHEMAS[table], *LINEAGE_FIELDS), lineage_rows)
            with (directory / "quarantine" / f"{table}.jsonl").open("w") as handle:
                for record in rejected[table]:
                    handle.write(json.dumps({**record, "source_id": source_id,
                                             "source_sha256": inputs[f"{table}.csv"]},
                                            sort_keys=True) + "\n")
        customers = {r["row"]["customer_id"]: r["row"] for r in accepted["customers"]}
        with (directory / "validated/transactions.csv").open(newline="") as handle:
            curated = [{**row, "customer_country": customers[row["customer_id"]]["country"]}
                       for row in csv.DictReader(handle)]
        _write_csv(directory / "curated/transactions.csv", CURATED_FIELDS, curated)
        totals["transactions"]["curated_rows"] = len(curated)
        totals["transactions"]["curated_money"] = _money([{"row": row} for row in curated])
        columns = [
            f'"{field}" {"INTEGER" if field in ("amount_minor", "source_row") else "TEXT"}'
            + " NOT NULL" + (" PRIMARY KEY" if field == "transaction_id" else "")
            for field in CURATED_FIELDS
        ]
        (directory / "curated/schema.sql").write_text(
            "CREATE TABLE curated_transactions (\n  " + ",\n  ".join(columns) + "\n);\n"
        )
        for table, summary in totals.items():
            if summary["source_rows"] != summary["validated_rows"] + summary["rejected_rows"]:
                raise ValueError(f"Row reconciliation failed: {table}")
        tx = totals["transactions"]
        if tx["curated_rows"] != tx["validated_rows"] or (
            tx["curated_money"] != tx["validated_money"]
        ):
            raise ValueError("Curated reconciliation failed")
        for currency, value in tx["source_money"]["amount_minor_by_currency"].items():
            if value != sum(tx[stage]["amount_minor_by_currency"].get(currency, 0)
                            for stage in ("validated_money", "rejected_money")):
                raise ValueError(f"Monetary reconciliation failed: {currency}")
        if tx["source_money"]["unaccountable_rows"] != sum(
            tx[stage]["unaccountable_rows"] for stage in ("validated_money", "rejected_money")
        ):
            raise ValueError("Unaccountable monetary row reconciliation failed")
        write_json(directory / "reconciliation.json", totals)
        manifest["status"] = (
            "completed_with_rejects" if any(rejected.values()) else "completed"
        )
    except Exception as error:
        manifest.update(status="failed", error={"type": type(error).__name__, "detail": str(error)})
    manifest["finished_at"] = utc_now()
    manifest["artifacts_sha256"] = {
        str(p.relative_to(directory)): sha256_bytes(p.read_bytes())
        for p in sorted(directory.rglob("*")) if p.is_file() and p.name != "manifest.json"
    }
    write_json(directory / "manifest.json", manifest)
    return {"run_dir": directory, "manifest": manifest, "reused": False}
