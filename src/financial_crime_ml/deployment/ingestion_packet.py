"""M7B immutable, offline ingestion packet for the three synthetic BigQuery input tables.

A packet freezes the exact bytes of three verified M3 artifacts together with their
SHA-256, MD5, sizes, row counts, per-currency totals, bucket-relative object names,
stable BigQuery job IDs and strict load specifications. It performs no cloud call.

SHA-256 is the packet's integrity anchor. MD5 is recorded only because Cloud Storage
reports it (as base64 ``md5Hash``) for non-composite objects; it is a transfer check,
not a substitute for SHA-256. CRC32C is deliberately not computed (no approved library).
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import re
import shutil
from pathlib import Path
from uuid import uuid4

from financial_crime_ml.analytics.workflow import verified_source
from financial_crime_ml.data_generation.v2.contracts import (
    COUNTRIES,
    CURRENCY_EXPONENTS,
    SCHEMAS,
    parse_utc,
)
from financial_crime_ml.evaluation.manifest import sha256_bytes
from financial_crime_ml.ingestion.foundation.contracts import (
    CURATED_FIELDS,
    ENTITY_SCHEMAS,
    EVENT_FIELDS,
    LINEAGE_FIELDS,
    validate_snapshot,
)
from financial_crime_ml.ingestion.foundation.contracts import VERSION as M3_CONTRACT_VERSION
from financial_crime_ml.ingestion.foundation.pipeline import _schema_reasons
from financial_crime_ml.ingestion.load_data import REPO_ROOT

PACKET_FORMAT = "m7b-synthetic-ingestion-packet"
PACKET_VERSION = "1.0.0"
SCHEMA_PATH = REPO_ROOT / "infrastructure/schemas/v1/source_tables.json"
CONTROLS_PATH = REPO_ROOT / "infrastructure/pilot_controls.json"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "outputs/packets"
APPROVED_LOCATIONS = ("europe-west2",)
OBJECT_PREFIX = "m7/ingestion"
ID_HEX_LENGTH = 24  # 96 bits of the identity SHA-256 in names and job IDs

# Table -> (M3 artifact, entity contract table, raw M3 input file). Order is load order.
TABLES = {
    "customers": ("validated/customers.csv", "customers", "customers.csv"),
    "accounts": ("validated/accounts.csv", "accounts", "accounts.csv"),
    "curated_transactions": ("curated/transactions.csv", "transactions", "transactions.csv"),
}
EXPECTED_FIELDS = {
    "customers": (*ENTITY_SCHEMAS["customers"], *LINEAGE_FIELDS),
    "accounts": (*ENTITY_SCHEMAS["accounts"], *LINEAGE_FIELDS),
    "curated_transactions": CURATED_FIELDS,
}
# Outcome/label fields from the M2 contract, plus generic label vocabulary.
FORBIDDEN_FIELDS = frozenset(
    {*SCHEMAS["outcomes"], *SCHEMAS["evaluation_transactions"]}
    - set(SCHEMAS["transactions"])
) | {"label", "target", "is_fraud", "fraud_label", "outcome"}
FORBIDDEN_PATTERN = re.compile(r"(label|target|fraud|suspicious|outcome|scenario|episode)", re.I)
LOAD_SETTINGS = {
    "allowJaggedRows": False,
    "allowQuotedNewlines": False,
    "autodetect": False,
    "createDisposition": "CREATE_NEVER",
    "encoding": "UTF-8",
    "fieldDelimiter": ",",
    "ignoreUnknownValues": False,
    "maxBadRecords": 0,
    "quote": '"',
    "schemaUpdateOptions": [],
    "skipLeadingRows": 1,
    "sourceFormat": "CSV",
    "writeDisposition": "WRITE_EMPTY",
}
CONTROL_KEYS = (
    "max_jobs_per_approved_execution",
    "max_stored_bytes_all_versions",
    "max_total_upload_bytes",
    "max_transactions",
    "pilot_hours",
)
CHECKSUM_NOTE = (
    "sha256 is the integrity anchor. md5 is recorded only for comparison with the Cloud "
    "Storage md5Hash of a non-composite object; it is not interchangeable with sha256. "
    "crc32c is not computed."
)
PACKET_ID_RE = re.compile(rf"m7p-[0-9a-f]{{{ID_HEX_LENGTH}}}")
JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,1024}")
PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
DATASET_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,1023}")
PROTECTED = ("data", "outputs/sample", "reports/sample", "infrastructure", "src", "docs")
MANIFEST, SIDECAR, DATA_DIR = "manifest.json", "manifest.sha256", "data"


class PacketError(ValueError):
    """Any packet construction or verification failure."""


def canonical_json(payload: object) -> bytes:
    """One byte representation per value: sorted keys, no whitespace, ASCII, final newline."""
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                       allow_nan=False) + "\n").encode("ascii")


def _md5(content: bytes) -> tuple[str, str]:
    digest = hashlib.md5(content, usedforsecurity=False).digest()
    return digest.hex(), base64.b64encode(digest).decode("ascii")


# ----------------------------------------------------------------- inputs: schema and controls
def load_schemas(path: Path = SCHEMA_PATH) -> tuple[dict, bytes]:
    content = Path(path).read_bytes()
    schemas = json.loads(content)
    if not isinstance(schemas, dict) or not set(TABLES) <= set(schemas):
        raise PacketError("source schema file must define the three input tables")
    selected = {}
    for table, expected in EXPECTED_FIELDS.items():
        fields = schemas[table]
        if not isinstance(fields, list):
            raise PacketError(f"{table}: schema must be a list of fields")
        for field in fields:
            if not isinstance(field, dict) or set(field) != {"name", "type", "mode"}:
                raise PacketError(f"{table}: schema fields need exactly name, type and mode")
            _reject_forbidden(table, [field["name"]])
            if field["mode"] != "REQUIRED" or field["type"] not in ("STRING", "INT64",
                                                                     "TIMESTAMP"):
                raise PacketError(f"{table}.{field['name']}: unsupported type or mode")
        if tuple(field["name"] for field in fields) != expected:
            raise PacketError(f"{table}: schema fields differ from the M3 contract")
        selected[table] = fields
    return selected, content


def load_controls(path: Path = CONTROLS_PATH) -> tuple[dict, bytes]:
    content = Path(path).read_bytes()
    controls = json.loads(content)
    picked = {}
    for key in CONTROL_KEYS:
        value = controls.get(key)
        if type(value) is not int or value <= 0:
            raise PacketError(f"pilot control {key} must be a positive integer")
        picked[key] = value
    if controls.get("budget_is_spending_cap") is not False:
        raise PacketError("pilot controls must not claim the budget is a spending cap")
    return picked, content


def _reject_forbidden(table: str, names) -> None:
    bad = sorted(n for n in names if n in FORBIDDEN_FIELDS or FORBIDDEN_PATTERN.search(n))
    if bad:
        raise PacketError(f"{table}: forbidden outcome/label field(s): {', '.join(bad)}")


def validate_target(project_id: str, dataset_id: str, location: str) -> dict:
    if not isinstance(project_id, str) or not PROJECT_RE.fullmatch(project_id):
        raise PacketError("invalid project_id")
    if not isinstance(dataset_id, str) or not DATASET_RE.fullmatch(dataset_id):
        raise PacketError("invalid dataset_id")
    if location not in APPROVED_LOCATIONS:
        raise PacketError(f"location must be one of {APPROVED_LOCATIONS}")
    return {"dataset_id": dataset_id, "location": location, "project_id": project_id}


# ------------------------------------------------------------------- content analysis
def _parse_csv(table: str, content: bytes) -> tuple[list[str], list[dict]]:
    if content.startswith(b"\xef\xbb\xbf"):
        raise PacketError(f"{table}: byte-order mark not allowed")
    if b"\r" in content or not content.endswith(b"\n"):
        raise PacketError(f"{table}: rows must be LF-terminated with no carriage returns")
    try:
        text = content.decode("utf-8")
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error) as error:
        raise PacketError(f"{table}: not strict UTF-8 CSV: {error}") from error
    if not rows:
        raise PacketError(f"{table}: empty file")
    header, body = rows[0], rows[1:]
    _reject_forbidden(table, header)
    expected = list(EXPECTED_FIELDS[table])
    if header != expected:
        missing = [f for f in expected if f not in header]
        extra = [f for f in header if f not in expected]
        problem = (f"missing {missing}" if missing else f"unknown {extra}" if extra
                   else "column order differs")
        raise PacketError(f"{table}: header does not match the schema ({problem})")
    if not body:
        raise PacketError(f"{table}: no data rows")
    if text.count("\n") != len(rows):
        raise PacketError(f"{table}: quoted newlines are not allowed")
    records = []
    for number, values in enumerate(body, 1):
        if len(values) != len(header):
            raise PacketError(f"{table}: row {number} has {len(values)} fields")
        records.append(dict(zip(header, values, strict=True)))
    return header, records


def _check_types(table: str, schema: list, records: list[dict]) -> None:
    for number, row in enumerate(records, 1):
        for field in schema:
            value = row[field["name"]]
            if value == "":
                raise PacketError(f"{table}: row {number} has empty REQUIRED {field['name']}")
            if field["type"] == "TIMESTAMP":
                try:
                    parse_utc(value)
                except ValueError as error:
                    raise PacketError(f"{table}: row {number} {field['name']}: {error}") from None
            elif field["type"] == "INT64" and not re.fullmatch(r"[1-9][0-9]{0,17}", value):
                raise PacketError(f"{table}: row {number} {field['name']} is not a positive INT64")


def analyse_tables(contents: dict[str, bytes], schemas: dict, snapshot: dict) -> dict:
    """Validate schema, contract values, lineage and relationships; return per-table facts."""
    if set(contents) != set(TABLES):
        raise PacketError(f"exactly these tables are required: {sorted(TABLES)}")
    parsed = {}
    for table, content in contents.items():
        _, records = _parse_csv(table, content)
        _check_types(table, schemas[table], records)
        entity = TABLES[table][1]
        for number, row in enumerate(records, 1):
            reasons = _schema_reasons(row, entity)
            if table == "curated_transactions" and row["customer_country"] not in COUNTRIES:
                reasons.append("schema.customer_country")
            if reasons:
                raise PacketError(f"{table}: row {number} invalid: {', '.join(sorted(reasons))}")
        parsed[table] = records

    # Lineage: one snapshot, one raw source file per table, increasing source rows.
    for table, records in parsed.items():
        raw_sha = snapshot["raw_source_sha256"][TABLES[table][2]]
        previous = 0
        for number, row in enumerate(records, 1):
            for field in ("source_id", "source_time", "ingestion_time"):
                if row[field] != snapshot[field]:
                    raise PacketError(f"{table}: row {number} lineage {field} differs")
            if row["source_sha256"] != raw_sha:
                raise PacketError(f"{table}: row {number} lineage source_sha256 differs")
            if int(row["source_row"]) <= previous:
                raise PacketError(f"{table}: lineage source_row must strictly increase")
            previous = int(row["source_row"])
            if row[EVENT_FIELDS[TABLES[table][1]]] > snapshot["source_time"]:
                raise PacketError(f"{table}: row {number} event after the source snapshot")

    # Keys and relationships among the three loaded tables.
    keys = {}
    for table, records in parsed.items():
        key = next(iter(EXPECTED_FIELDS[table]))
        values = [row[key] for row in records]
        if len(set(values)) != len(values):
            raise PacketError(f"{table}: duplicate primary key {key}")
        keys[table] = {row[key]: row for row in records}
    customers, accounts = keys["customers"], keys["accounts"]
    for row in parsed["accounts"]:
        customer = customers.get(row["customer_id"])
        if customer is None:
            raise PacketError(f"accounts: reference.customer_id {row['account_id']}")
        if row["opened_at"] < customer["onboarded_at"]:
            raise PacketError(f"accounts: opened before customer {row['account_id']}")
    for row in parsed["curated_transactions"]:
        customer, account = customers.get(row["customer_id"]), accounts.get(row["account_id"])
        if customer is None or account is None:
            raise PacketError(f"curated_transactions: reference {row['transaction_id']}")
        if account["customer_id"] != row["customer_id"]:
            raise PacketError(f"curated_transactions: ownership {row['transaction_id']}")
        if account["base_currency"] != row["currency"]:
            raise PacketError(f"curated_transactions: currency {row['transaction_id']}")
        if customer["country"] != row["customer_country"]:
            raise PacketError(f"curated_transactions: customer_country {row['transaction_id']}")
        if row["transaction_timestamp"] < account["opened_at"]:
            raise PacketError(f"curated_transactions: before account {row['transaction_id']}")

    money: dict[str, dict] = {}
    for row in parsed["curated_transactions"]:
        entry = money.setdefault(row["currency"], {"amount_minor": 0, "row_count": 0})
        entry["amount_minor"] += int(row["amount_minor"])
        entry["row_count"] += 1
    return {
        "money": {c: {**v, "exponent": CURRENCY_EXPONENTS[c]} for c, v in sorted(money.items())},
        "row_counts": {table: len(records) for table, records in parsed.items()},
    }


# ------------------------------------------------------------------- manifest composition
def compose_manifest(contents: dict[str, bytes], *, target: dict, snapshot: dict,
                     schemas: dict, schema_bytes: bytes,
                     controls: dict, controls_bytes: bytes) -> dict:
    """Pure function of the packet bytes and configuration; builder and verifier both call it.

    Local M3 run provenance (run ID, manifest hash) is deliberately excluded: that manifest
    hashes timestamps and absolute paths, which would make the packet hash machine-dependent.
    The data and raw-source SHA-256 values pin the lineage instead.
    """
    facts = analyse_tables(contents, schemas, snapshot)
    sizes = {table: len(content) for table, content in contents.items()}
    total_bytes = sum(sizes.values())
    transactions = facts["row_counts"]["curated_transactions"]
    if transactions > controls["max_transactions"]:
        raise PacketError(f"{transactions} transactions exceed max_transactions "
                          f"{controls['max_transactions']}")
    if total_bytes > controls["max_total_upload_bytes"]:
        raise PacketError(f"{total_bytes} bytes exceed max_total_upload_bytes")
    if total_bytes > controls["max_stored_bytes_all_versions"]:
        raise PacketError(f"{total_bytes} bytes exceed max_stored_bytes_all_versions")
    if len(TABLES) > controls["max_jobs_per_approved_execution"]:
        raise PacketError("load jobs exceed max_jobs_per_approved_execution")

    tables = {}
    for table, content in contents.items():
        md5_hex, md5_b64 = _md5(content)
        tables[table] = {
            "columns": list(EXPECTED_FIELDS[table]),
            "file": f"{DATA_DIR}/{table}.csv",
            "md5_base64": md5_b64,
            "md5_hex": md5_hex,
            "row_count": facts["row_counts"][table],
            "sha256": sha256_bytes(content),
            "size_bytes": sizes[table],
            "source_artifact": TABLES[table][0],
        }
    identity = {
        "classification": "synthetic",
        "controls": {**controls, "file_sha256": sha256_bytes(controls_bytes)},
        "load_settings": LOAD_SETTINGS,
        "m3_contract_version": M3_CONTRACT_VERSION,
        "packet_format": PACKET_FORMAT,
        "packet_version": PACKET_VERSION,
        "schema_file_sha256": sha256_bytes(schema_bytes),
        "source_snapshot": snapshot,
        "tables": {t: {k: v for k, v in e.items() if k != "file"} for t, e in tables.items()},
        "target": target,
    }
    identity_sha256 = sha256_bytes(canonical_json(identity))
    short = identity_sha256[:ID_HEX_LENGTH]
    packet_id = f"m7p-{short}"
    objects = {table: f"{OBJECT_PREFIX}/{packet_id}/{table}.csv" for table in TABLES}
    load_jobs = {
        table: {
            "job_id": f"m7_load_{short}_{table}",
            "location": target["location"],
            "configuration": {"load": {
                **LOAD_SETTINGS,
                "destinationTable": {"datasetId": target["dataset_id"],
                                     "projectId": target["project_id"], "tableId": table},
                "schema": {"fields": schemas[table]},
                "sourceObject": objects[table],
                "sourceUriTemplate": "gs://{bucket}/" + objects[table],
            }},
        }
        for table in TABLES
    }
    _check_load_jobs(load_jobs, objects, target)
    return {
        "checksum_note": CHECKSUM_NOTE,
        "identity": identity,
        "identity_sha256": identity_sha256,
        "limits": {
            "load_jobs": len(TABLES),
            "query_bytes_billed": 0,
            "total_upload_bytes": total_bytes,
            "transactions": transactions,
        },
        "load_jobs": load_jobs,
        "objects": {"bucket": "bound_at_execution_from_custody_inputs", "names": objects,
                    "precondition": "ifGenerationMatch=0"},
        "packet_id": packet_id,
        "reconciliation": {"amount_minor_by_currency": facts["money"],
                           "row_counts": facts["row_counts"]},
        "tables": tables,
    }


def _check_load_jobs(load_jobs: dict, objects: dict, target: dict) -> None:
    """Independent structural check, applied before any comparison with a recomputation."""
    if not isinstance(load_jobs, dict) or set(load_jobs) != set(TABLES):
        raise PacketError("load jobs must cover exactly the three input tables")
    ids = [spec.get("job_id") for spec in load_jobs.values() if isinstance(spec, dict)]
    if len(ids) != len(TABLES) or len(set(ids)) != len(ids):
        raise PacketError("duplicate or missing load job IDs")
    if not isinstance(objects, dict) or len(set(objects.values())) != len(TABLES):
        raise PacketError("duplicate or missing object names")
    for table, spec in load_jobs.items():
        if not isinstance(spec["job_id"], str) or not JOB_ID_RE.fullmatch(spec["job_id"]):
            raise PacketError(f"{table}: invalid job ID")
        load = spec["configuration"]["load"]
        if not isinstance(load, dict):
            raise PacketError(f"{table}: load configuration malformed")
        if spec.get("location") != target["location"]:
            raise PacketError(f"{table}: load job location differs from the target")
        for key, value in LOAD_SETTINGS.items():
            if load.get(key) != value:
                raise PacketError(f"{table}: load setting {key} must be {value!r}")
        destination = load.get("destinationTable", {})
        if destination != {"datasetId": target["dataset_id"],
                           "projectId": target["project_id"], "tableId": table}:
            raise PacketError(f"{table}: load destination differs from the target")
        if load.get("sourceObject") != objects.get(table):
            raise PacketError(f"{table}: load source object differs")


# ------------------------------------------------------------------- build and verify
def _confined(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise PacketError(f"path escapes the packet directory: {path}")
    return resolved


def _check_output_root(root: Path, source_run: Path) -> Path:
    root = Path(root).resolve()
    for protected in PROTECTED:
        if root.is_relative_to((REPO_ROOT / protected).resolve()):
            raise PacketError(f"packets cannot be written under {protected}")
    if root.is_relative_to(Path(source_run).resolve()):
        raise PacketError("packets cannot be written inside the source run")
    return root


def build_packet(source_run: Path, *, project_id: str, dataset_id: str,
                 location: str = "europe-west2", output_root: Path = DEFAULT_OUTPUT_ROOT,
                 schema_path: Path = SCHEMA_PATH, controls_path: Path = CONTROLS_PATH) -> dict:
    """Build into a private temporary directory, verify it, then publish atomically."""
    target = validate_target(project_id, dataset_id, location)
    source_run = Path(source_run)
    root = _check_output_root(output_root, source_run)
    schemas, schema_bytes = load_schemas(schema_path)
    controls, controls_bytes = load_controls(controls_path)
    m3, _, data, m3_manifest_bytes = verified_source(source_run)
    if m3["status"] != "completed":
        raise PacketError("source run must be 'completed' (quarantined rejects need review)")
    contents = {}
    for table, (artifact, _, _) in TABLES.items():
        content = (source_run.resolve() / artifact).read_bytes()
        if sha256_bytes(content) != m3["artifacts_sha256"][artifact]:
            raise PacketError(f"{artifact} changed after source verification")
        contents[table] = content
    snapshot = {"ingestion_time": m3["ingestion_time"],
                "raw_source_sha256": {TABLES[t][2]: m3["input_sha256"][TABLES[t][2]]
                                      for t in TABLES},
                "source_id": m3["source_id"], "source_time": m3["source_time"]}
    validate_snapshot(snapshot["source_id"], snapshot["source_time"], snapshot["ingestion_time"])
    provenance = {"m3_manifest_sha256": sha256_bytes(m3_manifest_bytes),
                  "m3_run_id": m3["run_id"]}  # local record only; not part of the packet
    manifest = compose_manifest(contents, target=target, snapshot=snapshot, schemas=schemas,
                                schema_bytes=schema_bytes, controls=controls,
                                controls_bytes=controls_bytes)
    reconciliation = data["reconciliation"]
    m3_money = reconciliation["transactions"]["curated_money"]["amount_minor_by_currency"]
    packet_money = {c: v["amount_minor"]
                    for c, v in manifest["reconciliation"]["amount_minor_by_currency"].items()}
    if packet_money != m3_money:
        raise PacketError("packet money differs from M3 curated reconciliation")
    expected_rows = {"customers": reconciliation["customers"]["validated_rows"],
                     "accounts": reconciliation["accounts"]["validated_rows"],
                     "curated_transactions": reconciliation["transactions"]["curated_rows"]}
    if manifest["reconciliation"]["row_counts"] != expected_rows:
        raise PacketError("packet row counts differ from M3 reconciliation")

    manifest_bytes = canonical_json(manifest)
    manifest_sha256 = sha256_bytes(manifest_bytes)
    final = root / manifest["packet_id"]
    if final.exists() or final.is_symlink():
        existing = verify_packet(final, schema_path=schema_path, controls_path=controls_path)
        if existing["manifest_sha256"] != manifest_sha256:
            raise PacketError("packet identity collision: an existing packet has this ID "
                              "with a different manifest")
        return {**existing, "packet_dir": final, "provenance": provenance, "reused": True}
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".staging-{manifest['packet_id']}-{uuid4().hex}"
    staging.mkdir()
    try:
        (staging / DATA_DIR).mkdir()
        for table, content in contents.items():
            _write_new(_confined(staging, staging / manifest["tables"][table]["file"]), content)
        _write_new(staging / MANIFEST, manifest_bytes)
        _write_new(staging / SIDECAR, f"{manifest_sha256}  {MANIFEST}\n".encode("ascii"))
        verify_packet(staging, expected_manifest_sha256=manifest_sha256,
                      schema_path=schema_path, controls_path=controls_path,
                      directory_name=manifest["packet_id"])
        staging.rename(final)  # fails rather than replacing a non-empty directory
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"manifest": manifest, "manifest_sha256": manifest_sha256, "packet_dir": final,
            "packet_id": manifest["packet_id"], "provenance": provenance, "reused": False}


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)


def verify_packet(packet_dir: Path, *, expected_manifest_sha256: str | None = None,
                  schema_path: Path = SCHEMA_PATH, controls_path: Path = CONTROLS_PATH,
                  directory_name: str | None = None) -> dict:
    """Independently re-derive the whole manifest from the packet's own bytes."""
    try:
        return _verify(Path(packet_dir), expected_manifest_sha256, schema_path,
                       controls_path, directory_name)
    except (AttributeError, TypeError, KeyError) as error:
        raise PacketError(f"manifest malformed: {type(error).__name__}: {error}") from None


def _verify(packet_dir: Path, expected_manifest_sha256: str | None, schema_path: Path,
            controls_path: Path, directory_name: str | None) -> dict:
    if packet_dir.is_symlink() or not packet_dir.is_dir():
        raise PacketError("packet directory missing or a symbolic link")
    expected_files = {MANIFEST, SIDECAR, *(f"{DATA_DIR}/{t}.csv" for t in TABLES)}
    found = set()
    for path in packet_dir.rglob("*"):
        relative = path.relative_to(packet_dir).as_posix()
        if path.is_symlink():
            raise PacketError(f"symbolic link in packet: {relative}")
        if path.is_dir():
            if relative != DATA_DIR:
                raise PacketError(f"unexpected directory in packet: {relative}")
            continue
        found.add(relative)
    if found != expected_files:
        missing, extra = sorted(expected_files - found), sorted(found - expected_files)
        raise PacketError(f"packet files differ: missing {missing}, unexpected {extra}")

    manifest_bytes = (packet_dir / MANIFEST).read_bytes()
    manifest_sha256 = sha256_bytes(manifest_bytes)
    if (packet_dir / SIDECAR).read_bytes() != f"{manifest_sha256}  {MANIFEST}\n".encode():
        raise PacketError("manifest SHA-256 sidecar does not match the manifest")
    if expected_manifest_sha256 is not None and manifest_sha256 != expected_manifest_sha256:
        raise PacketError("manifest SHA-256 differs from the approved value")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as error:
        raise PacketError(f"manifest is not JSON: {error}") from None
    if not isinstance(manifest, dict) or canonical_json(manifest) != manifest_bytes:
        raise PacketError("manifest is not in canonical form")
    packet_id = manifest.get("packet_id")
    if not isinstance(packet_id, str) or not PACKET_ID_RE.fullmatch(packet_id):
        raise PacketError("invalid packet_id")
    if (directory_name or packet_dir.name) != packet_id:
        raise PacketError("packet directory name differs from packet_id")
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        raise PacketError("manifest identity missing")
    target = identity.get("target", {})
    validate_target(target.get("project_id"), target.get("dataset_id"), target.get("location"))
    _check_load_jobs(manifest["load_jobs"], manifest["objects"]["names"], target)

    contents = {table: _confined(packet_dir, packet_dir / DATA_DIR / f"{table}.csv").read_bytes()
                for table in TABLES}
    schemas, schema_bytes = load_schemas(schema_path)
    controls, controls_bytes = load_controls(controls_path)
    snapshot = identity.get("source_snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "ingestion_time", "raw_source_sha256", "source_id", "source_time"
    } or set(snapshot["raw_source_sha256"]) != {entry[2] for entry in TABLES.values()}:
        raise PacketError("manifest source snapshot malformed")
    validate_snapshot(snapshot["source_id"], snapshot["source_time"], snapshot["ingestion_time"])
    recomputed = compose_manifest(contents, target=target, snapshot=snapshot, schemas=schemas,
                                  schema_bytes=schema_bytes, controls=controls,
                                  controls_bytes=controls_bytes)
    for table in TABLES:
        if recomputed["tables"][table] != manifest.get("tables", {}).get(table):
            raise PacketError(f"{table}: packet bytes do not match the manifest")
    if recomputed != manifest:
        differing = sorted(k for k in set(recomputed) | set(manifest)
                           if recomputed.get(k) != manifest.get(k))
        raise PacketError(f"manifest differs from recomputation: {differing}")
    return {"manifest": manifest, "manifest_sha256": manifest_sha256, "packet_id": packet_id}


# ------------------------------------------------------------------- CLI
def _summary(result: dict) -> None:
    manifest = result["manifest"]
    print(f"Packet: {result['packet_id']}")
    print(f"Manifest SHA-256: {result['manifest_sha256']}")
    for table, entry in manifest["tables"].items():
        print(f"  {table}: {entry['row_count']} rows, {entry['size_bytes']} bytes, "
              f"sha256 {entry['sha256']}")
    for currency, entry in manifest["reconciliation"]["amount_minor_by_currency"].items():
        print(f"  {currency}: {entry['row_count']} rows, {entry['amount_minor']} minor units")
    print(f"Total upload bytes: {manifest['limits']['total_upload_bytes']}")


def build_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml build-ingestion-packet")
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--location", default="europe-west2")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args(argv)
    try:
        result = build_packet(**vars(args))
    except (ValueError, OSError, KeyError, csv.Error) as error:
        parser.exit(1, f"Packet rejected: {error}\n")
    print(f"Packet {'reused' if result['reused'] else 'built'}: {result['packet_dir']}")
    _summary(result)
    print(f"Local source record (not in packet): M3 run {result['provenance']['m3_run_id']}, "
          f"manifest sha256 {result['provenance']['m3_manifest_sha256']}")


def verify_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml verify-ingestion-packet")
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256")
    args = parser.parse_args(argv)
    try:
        result = verify_packet(args.packet_dir,
                               expected_manifest_sha256=args.expected_manifest_sha256)
    except (ValueError, OSError, KeyError, csv.Error) as error:
        parser.exit(1, f"Packet verification failed: {error}\n")
    print("Packet verified.")
    _summary(result)
