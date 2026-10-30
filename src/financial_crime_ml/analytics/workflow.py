"""Verify an M3 snapshot and build deterministic, reconciled SQLite marts."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

from financial_crime_ml.analytics.contracts import COLUMNS, GRAINS, VERSION, bigquery_schemas
from financial_crime_ml.evaluation.manifest import (
    execution_provenance,
    sha256_bytes,
    utc_now,
    write_json,
)
from financial_crime_ml.ingestion.foundation.contracts import (
    CURATED_FIELDS,
    ENTITY_SCHEMAS,
    LINEAGE_FIELDS,
    TABLES,
    description,
    validate_snapshot,
)
from financial_crime_ml.ingestion.foundation.pipeline import transform
from financial_crime_ml.ingestion.load_data import REPO_ROOT

SQL_PATH = REPO_ROOT / "sql/analytics/v1/marts.sqlite.sql"


def _csv(content: bytes, fields) -> list[dict]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8")), strict=True)
    if reader.fieldnames != list(fields):
        raise ValueError("Input CSV schema/order does not match M3 contract")
    rows = list(reader)
    if any(set(row) != set(fields) or any(value in (None, "") for value in row.values())
           for row in rows):
        raise ValueError("Malformed or null M3 CSV record")
    return rows


def verified_source(directory: Path) -> tuple[dict, dict, dict, bytes]:
    """Read exact bytes once; rederive accepted records rather than trusting declared success."""
    directory = directory.resolve()
    manifest_bytes = (directory / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("status") not in ("completed", "completed_with_rejects") or (
        manifest.get("contract_version") != "1.0.0"
    ):
        raise ValueError("Require a completed M3 foundation v1 run")
    validate_snapshot(manifest["source_id"], manifest["source_time"], manifest["ingestion_time"])
    contents = {}
    for relative, digest in manifest["artifacts_sha256"].items():
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or path == directory / "manifest.json":
            raise ValueError("Unsafe source artifact path")
        content = path.read_bytes()
        if sha256_bytes(content) != digest:
            raise ValueError(f"Source artifact hash mismatch: {relative}")
        contents[relative] = content
    if json.loads(contents["contract.json"]) != json.loads(json.dumps(description())):
        raise ValueError("Unsupported M3 contract description")
    raw = {table: contents[f"raw/{table}.csv"] for table in TABLES}
    for table in TABLES:
        if sha256_bytes(raw[table]) != manifest["input_sha256"][f"{table}.csv"]:
            raise ValueError("M3 raw source hash mismatch")
    accepted, _, totals = transform(raw, manifest["source_time"])
    validated = {}
    for table in TABLES:
        fields = (*ENTITY_SCHEMAS[table], *LINEAGE_FIELDS)
        rows = _csv(contents[f"validated/{table}.csv"], fields)
        expected = [{**record["row"], "source_id": manifest["source_id"],
                     "source_time": manifest["source_time"],
                     "ingestion_time": manifest["ingestion_time"],
                     "source_row": str(record["source_row"]),
                     "source_sha256": manifest["input_sha256"][f"{table}.csv"]}
                    for record in accepted[table]]
        if rows != expected:
            raise ValueError(f"Validated {table} does not match raw lineage")
        validated[table] = rows
    customers = {row["customer_id"]: row for row in validated["customers"]}
    curated = _csv(contents["curated/transactions.csv"], CURATED_FIELDS)
    expected = [{**row, "customer_country": customers[row["customer_id"]]["country"]}
                for row in validated["transactions"]]
    if curated != expected:
        raise ValueError("Curated transactions do not match validated observations/join")
    totals["transactions"].update(curated_rows=len(curated),
                                  curated_money=totals["transactions"]["validated_money"])
    if json.loads(contents["reconciliation.json"]) != totals:
        raise ValueError("M3 reconciliation mismatch")
    return manifest, validated, {"rows": curated, "reconciliation": totals}, manifest_bytes


def _build(connection: sqlite3.Connection, validated: dict, curated: list, sql: str) -> dict:
    for table, fields, rows in (
        ("curated_transactions", CURATED_FIELDS, curated),
        ("customers", tuple(ENTITY_SCHEMAS["customers"]), validated["customers"]),
        ("accounts", tuple(ENTITY_SCHEMAS["accounts"]), validated["accounts"]),
    ):
        primary = "transaction_id" if table == "curated_transactions" else fields[0]
        columns = [f'"{field}" {"INTEGER" if field == "amount_minor" else "TEXT"} NOT NULL'
                   + (" PRIMARY KEY" if field == primary else "") for field in fields]
        connection.execute(f'CREATE TABLE {table} ({",".join(columns)})')
        connection.executemany(f'INSERT INTO {table} VALUES ({",".join("?" for _ in fields)})',
                               [[row[field] for field in fields] for row in rows])
    connection.executescript(sql)
    results = {}
    for table, grain in GRAINS.items():
        fields = COLUMNS[table]
        if [row[1] for row in connection.execute(f"PRAGMA table_info({table})")] != list(fields):
            raise ValueError(f"Mart schema mismatch: {table}")
        connection.execute(f'CREATE UNIQUE INDEX {table}_grain ON {table} ({",".join(grain)})')
        nulls = " OR ".join(f'"{field}" IS NULL' for field in fields)
        if connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {nulls}").fetchone()[0]:
            raise ValueError(f"Null mart field: {table}")
        results[table] = [dict(zip(fields, row, strict=True)) for row in connection.execute(
            f'SELECT * FROM {table} ORDER BY {",".join(grain)}')]
    return results


def build_marts(source_run: Path, run_id: str | None = None, *,
                runs_root: Path = REPO_ROOT / "outputs/runs") -> dict:
    source, validated, data, manifest_bytes = verified_source(Path(source_run))
    run_id = run_id or f"analytics-{uuid4().hex}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
        raise ValueError("Invalid run_id")
    root = Path(runs_root).resolve()
    if any(root.is_relative_to(REPO_ROOT / protected) for protected in
           ("data/sample", "data/processed", "outputs/sample", "reports/sample")):
        raise ValueError("Cannot write analytics under historical samples")
    sql_bytes = SQL_PATH.read_bytes()
    provenance = execution_provenance()
    settings = {"contract_version": VERSION, "source_manifest_sha256": sha256_bytes(manifest_bytes),
                "source_fingerprint": provenance["source_fingerprint_sha256"],
                "sql_sha256": sha256_bytes(sql_bytes)}
    signature = sha256_bytes(json.dumps(settings, sort_keys=True).encode())
    directory = root / run_id
    if directory.exists():
        manifest = json.loads((directory / "manifest.json").read_bytes())
        if manifest["signature"] != signature or manifest["status"] != "completed":
            raise ValueError("Run collision: changed source, SQL or implementation; or failed run")
        actual = {str(p.relative_to(directory)): sha256_bytes(p.read_bytes())
                  for p in directory.rglob("*") if p.is_file()
                  and p.relative_to(directory) != Path("manifest.json")}
        if actual != manifest["artifacts_sha256"]:
            raise ValueError("Analytics artifact integrity failure")
        return {"run_dir": directory, "manifest": manifest, "reused": True}
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {**settings, **provenance, "signature": signature,
                "run_id": run_id, "status": "running",
                "started_at": utc_now(), "source_run": str(Path(source_run).resolve()),
                "source_run_id": source["run_id"], "source_status": source["status"],
                "sqlite_version": sqlite3.sqlite_version}
    write_json(directory / "manifest.json", manifest)
    try:
        (directory / "marts.sqlite.sql").write_bytes(sql_bytes)
        (directory / "source_manifest.json").write_bytes(manifest_bytes)
        write_json(directory / "bigquery_schemas.json", bigquery_schemas())
        with sqlite3.connect(directory / "analytics.sqlite") as connection:
            results = _build(connection, validated, data["rows"], sql_bytes.decode())
            expected = {}
            for row in data["rows"]:
                count, amount = expected.get(row["currency"], (0, 0))
                expected[row["currency"]] = (count + 1, amount + int(row["amount_minor"]))
            reconciliation = {}
            for table, rows in results.items():
                totals = {}
                for row in rows:
                    count, amount = totals.get(row["currency"], (0, 0))
                    totals[row["currency"]] = (count + row["transaction_count"],
                                               amount + row["total_amount_minor"])
                if totals != expected:
                    raise ValueError(f"Mart reconciliation failed: {table}")
                with (directory / f"{table}.csv").open("w", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=COLUMNS[table], lineterminator="\n")
                    writer.writeheader()
                    writer.writerows(rows)
                reconciliation[table] = {"row_count": len(rows), "grain": GRAINS[table],
                                         "count_and_amount_minor_by_currency": totals}
            write_json(directory / "reconciliation.json", reconciliation)
            # Map every observed transaction to each mart grain using the same UTC event date.
            lineage = []
            for row in data["rows"]:
                keys = {**row, "event_date": row["transaction_timestamp"][:10]}
                for table, grain in GRAINS.items():
                    lineage.append({"mart": table, "grain": {f: keys[f] for f in grain},
                                    "transaction_id": row["transaction_id"],
                                    "source_row": row["source_row"],
                                    "source_sha256": row["source_sha256"]})
            (directory / "lineage.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in lineage))
        manifest["status"] = "completed"
    except Exception as error:
        manifest.update(status="failed", error={"type": type(error).__name__, "detail": str(error)})
    manifest["finished_at"] = utc_now()
    manifest["artifacts_sha256"] = {
        str(p.relative_to(directory)): sha256_bytes(p.read_bytes())
        for p in sorted(directory.rglob("*")) if p.is_file()
        and p.relative_to(directory) != Path("manifest.json")}
    write_json(directory / "manifest.json", manifest)
    return {"run_dir": directory, "manifest": manifest, "reused": False}


def analytics_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml build-financial-marts")
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    try:
        result = build_marts(**vars(args))
    except (ValueError, OSError, KeyError, csv.Error) as error:
        parser.exit(1, f"Analytics rejected: {error}\n")
    print(f"Financial marts {result['manifest']['status']}: {result['run_dir']}")
    print(f"Reused: {result['reused']}")
    if result["manifest"]["status"] != "completed":
        parser.exit(1, f"{result['manifest']['error']}\n")
