"""Compare seven downloaded CSV exports with verified M3/M4 reference artifacts offline."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

from financial_crime_ml.analytics.contracts import GRAINS, VERSION, bigquery_schemas
from financial_crime_ml.analytics.workflow import verified_source
from financial_crime_ml.evaluation.manifest import sha256_bytes
from financial_crime_ml.ingestion.load_data import REPO_ROOT


def normalize(value: str, field_type: str) -> str | int:
    """Lossless typed comparison; never pass money or counts through a float."""
    if value is None or value == "":
        raise ValueError("NULL or empty required value")
    if field_type == "INT64":
        if not re.fullmatch(r"-?[0-9]+", value):
            raise ValueError("INT64 must be an exact decimal integer")
        result = int(value)
        if not -(2**63) <= result < 2**63:
            raise ValueError("INT64 overflow")
        return result
    if field_type == "DATE":
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Invalid DATE")
        return date.fromisoformat(value).isoformat()
    if field_type == "TIMESTAMP":
        # Accept common BigQuery CSV UTC formats and explicit RFC3339 offsets only.
        text = value.removesuffix(" UTC") + ("+00:00" if value.endswith(" UTC") else "")
        if not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.0{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})", text
        ):
            raise ValueError("TIMESTAMP must have an explicit zone and whole-second precision")
        return datetime.fromisoformat(text).astimezone(UTC).isoformat(timespec="seconds")
    if field_type != "STRING":
        raise ValueError(f"Unsupported field type: {field_type}")
    return value


def canonical_rows(content: bytes, schema: list[dict], grain: tuple) -> list[dict]:
    fields = [field["name"] for field in schema]
    reader = csv.DictReader(io.StringIO(content.decode("utf-8")), strict=True)
    if reader.fieldnames != fields:
        raise ValueError("CSV schema/order mismatch")
    rows = []
    keys = set()
    for row in reader:
        if set(row) != set(fields):
            raise ValueError("Malformed CSV field count")
        normalized = {f["name"]: normalize(row[f["name"]], f["type"]) for f in schema}
        key = tuple(normalized[field] for field in grain)
        if key in keys:
            raise ValueError("Duplicate key/grain")
        keys.add(key)
        rows.append(normalized)
    return sorted(rows, key=lambda row: tuple(row[field] for field in grain))


def reference_artifacts(directory: Path, source_manifest: bytes) -> tuple[bytes, dict]:
    """Read hashed payloads once; refuse manifest traversal and unlisted payloads."""
    directory = directory.resolve()
    manifest_bytes = (directory / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("status") != "completed" or manifest.get("contract_version") != VERSION:
        raise ValueError("Require a completed M4 analytics v1 run")
    if manifest.get("source_manifest_sha256") != sha256_bytes(source_manifest):
        raise ValueError("M4 source manifest mismatch")
    contents = {}
    for relative, digest in manifest["artifacts_sha256"].items():
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or path == directory / "manifest.json":
            raise ValueError("Unsafe reference artifact path")
        content = path.read_bytes()
        if sha256_bytes(content) != digest:
            raise ValueError(f"M4 artifact hash mismatch: {relative}")
        contents[relative] = content
    actual = {str(path.relative_to(directory)) for path in directory.rglob("*")
              if path.is_file() and path != directory / "manifest.json"}
    if actual != set(contents):
        raise ValueError("Unlisted M4 reference artifacts")
    if contents["source_manifest.json"] != source_manifest:
        raise ValueError("Embedded M3 manifest mismatch")
    if json.loads(contents["bigquery_schemas.json"]) != json.loads(json.dumps(bigquery_schemas())):
        raise ValueError("Unsupported M4 target schema")
    return manifest_bytes, contents


def _totals(rows: list[dict]) -> dict:
    totals = {}
    for row in rows:
        count, amount = totals.get(row["currency"], (0, 0))
        totals[row["currency"]] = (count + row.get("transaction_count", 1),
                                  amount + row.get("total_amount_minor", row.get("amount_minor")))
    return totals


def reconcile(source_run: Path, reference_run: Path, export_dir: Path) -> dict:
    """No cloud calls or writes. Export files require separate approved acquisition."""
    _, validated, curated, source_bytes = verified_source(Path(source_run))
    controls = json.loads((REPO_ROOT / "infrastructure/pilot_controls.json").read_text())
    if len(curated["rows"]) > controls["max_transactions"]:
        raise ValueError("Transaction volume exceeds pilot limit")
    reference_bytes, artifacts = reference_artifacts(Path(reference_run), source_bytes)
    schemas = json.loads((REPO_ROOT / "infrastructure/schemas/v1/source_tables.json").read_text())
    schemas.update({name: spec["fields"] for name, spec in bigquery_schemas()["tables"].items()})
    export_dir = Path(export_dir).resolve()
    if {path.name for path in export_dir.glob("*.csv")} != {f"{name}.csv" for name in schemas}:
        raise ValueError("Require exactly seven named CSV exports")
    if sum(path.stat().st_size for path in export_dir.glob("*.csv")) > (
        controls["max_stored_bytes_all_versions"]
    ):
        raise ValueError("Export volume exceeds pilot limit")
    expected_totals = _totals([{**row, "amount_minor": int(row["amount_minor"])}
                               for row in curated["rows"]])
    tables = {}
    for table, schema in schemas.items():
        grain = GRAINS.get(table, (schema[0]["name"],))
        if table in GRAINS:
            expected = canonical_rows(artifacts[f"{table}.csv"], schema, grain)
        else:
            records = curated["rows"] if table == "curated_transactions" else validated[table]
            expected = sorted(
                [{f["name"]: normalize(row[f["name"]], f["type"]) for f in schema}
                 for row in records], key=lambda row: tuple(row[field] for field in grain))
        path = (export_dir / f"{table}.csv").resolve()
        if not path.is_relative_to(export_dir):
            raise ValueError("Unsafe export path")
        content = path.read_bytes()
        actual = canonical_rows(content, schema, grain)
        if actual != expected:
            raise ValueError(f"Row/value reconciliation failed: {table}")
        totals = None
        if table in GRAINS or table == "curated_transactions":
            totals = _totals(actual)
            if totals != expected_totals:
                raise ValueError(f"Currency reconciliation failed: {table}")
        tables[table] = {
            "row_count": len(actual), "export_sha256": sha256_bytes(content),
            "logical_sha256": sha256_bytes(json.dumps(actual, sort_keys=True).encode()),
            "count_and_amount_minor_by_currency": totals,
        }
    return {"status": "matched", "scope": "offline CSV values only; no cloud provenance check",
            "cloud_execution_verified": False, "source_manifest_sha256": sha256_bytes(source_bytes),
            "reference_manifest_sha256": sha256_bytes(reference_bytes), "tables": tables}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--reference-run", type=Path, required=True)
    parser.add_argument("--export-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = reconcile(**vars(args))
    except (ValueError, OSError, KeyError, TypeError, csv.Error) as error:
        parser.exit(1, f"Reconciliation rejected: {error}\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
