"""M8: build the four analytics marts in the pilot dataset, as the runner, from M7D inputs.

Offline and pure except for writing a new bundle directory. Nothing here calls a cloud API.

* **Reference:** the M4 SQLite mart SQL executed over the verified M7B packet (the same bytes
  M7D proved byte-identical in BigQuery). The BigQuery SELECT bodies of the M5 transactional
  script are also executed in SQLite (only ``DATE(x, 'UTC')`` is translated) and must give the
  same rows, so the executable SQL itself is tested offline.
* **Execution:** the existing M5 script ``infrastructure/sql/marts.v1.insert.proposed.sql``
  (BEGIN; 4 DELETE; 4 INSERT; COMMIT) with only the approved identifiers substituted. It is
  idempotent: a re-run replaces the four marts with identical rows. The script job ID is
  stable per packet, SQL and attempt, so a duplicate submission returns HTTP 409.
* **Least privilege:** the runner already holds exactly what this needs (jobs.create; input
  tables get/getData; marts get/getData/updateData). No new permission is requested.
* **Evidence:** dry runs, the script job, its child jobs, mart read-backs and input-table
  metadata before and after, verified here offline (``verify-mart-evidence``).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from financial_crime_ml.analytics.contracts import COLUMNS, GRAINS, bigquery_schemas
from financial_crime_ml.analytics.workflow import SQL_PATH as SQLITE_SQL_PATH
from financial_crime_ml.analytics.workflow import _build
from financial_crime_ml.deployment.ingestion_bundle import (
    SHA256_RE,
    BundleError,
    _inside_git_worktree,
    _safe_path,
    loaded_table_expectations,
    validate_window,
)
from financial_crime_ml.deployment.ingestion_evidence import (
    DECIMAL_RE,
    in_window,
    parse_epoch_ms,
    schema_problems,
    verify_table,
)
from financial_crime_ml.deployment.ingestion_packet import (
    DATASET_RE,
    PROJECT_RE,
    TABLES,
    canonical_json,
    verify_packet,
)
from financial_crime_ml.deployment.ingestion_readback import ReadbackError, convert_pages
from financial_crime_ml.evaluation.manifest import sha256_bytes
from financial_crime_ml.ingestion.load_data import REPO_ROOT

BUNDLE_FORMAT, BUNDLE_VERSION = "m8-mart-bundle", "1.0.0"
SCRIPT_SQL_PATH = REPO_ROOT / "infrastructure/sql/marts.v1.insert.proposed.sql"
CONTROLS_PATH = REPO_ROOT / "infrastructure/pilot_controls.json"
MARTS = tuple(GRAINS)
INPUTS = tuple(TABLES)
SCRIPT, RECORD, SUMS, PACKET_SUMS = "m8.sh", "bundle.json", "SHA256SUMS", "packet.sha256"
REQUESTS, EVIDENCE, EXPECTED = "requests", "evidence", "expected"
EVIDENCE_DIRS = ("tables", "dryrun", "jobs", "readback")
DRY_RUNS = (*MARTS, "script")
PAGE_SIZE = 1000
MIN_BILLED_PER_TABLE = 10 * 1024**2  # BigQuery on-demand minimum per referenced table
ILLUSTRATIVE_USD_PER_TIB = 10.0  # docs/gcp_deployment_readiness.md; not a verified quote
CHILD_TYPES = Counter({"BEGIN_TRANSACTION": 1, "DELETE": 4, "INSERT": 4,
                       "COMMIT_TRANSACTION": 1})
DATE_SOURCE = re.compile(r"DATE\(((?:t\.)?transaction_timestamp), 'UTC'\)")
TABLE_REF = re.compile(r"`PROJECT\.DATASET\.([a-z_]+)`")
INSERT_RE = re.compile(r"^INSERT INTO `PROJECT\.DATASET\.([a-z_]+)` \(([^)]*)\)\n(SELECT .+)$",
                       re.S)


class MartError(ValueError):
    """Any M8 derivation, rendering or evidence verification failure."""


# --------------------------------------------------------------------------- SQL
def script_statements(text: str) -> list[str]:
    """Statements of the M5 script, comments removed, in order."""
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    statements = [s.strip() for s in body.split(";") if s.strip()]
    return statements


def parse_script(text: str) -> dict[str, str]:
    """Require exactly BEGIN, 4 DELETE (all rows), 4 explicit-column INSERT ... SELECT in the
    contract order, COMMIT. Returns ``{mart: SELECT body}`` with placeholder identifiers."""
    statements = script_statements(text)
    deletes = [f"DELETE FROM `PROJECT.DATASET.{m}` WHERE TRUE" for m in MARTS]
    if (len(statements) != 10 or statements[0] != "BEGIN TRANSACTION"
            or statements[-1] != "COMMIT TRANSACTION" or statements[1:5] != deletes):
        raise MartError("M5 script structure differs from BEGIN; 4 DELETE; 4 INSERT; COMMIT")
    selects = {}
    for mart, statement in zip(MARTS, statements[5:9], strict=True):
        match = INSERT_RE.match(statement)
        if not match or match.group(1) != mart:
            raise MartError(f"{mart}: INSERT statement missing or out of order")
        if [c.strip() for c in match.group(2).split(",")] != list(COLUMNS[mart]):
            raise MartError(f"{mart}: INSERT column list differs from the contract")
        select = match.group(3)
        refs = TABLE_REF.findall(select)
        if not refs or not set(refs) <= set(INPUTS):
            raise MartError(f"{mart}: SELECT may read only the three input tables")
        if re.search(r"\b(CREATE|DROP|ALTER|TRUNCATE|MERGE|UPDATE|DELETE|INSERT|EXPORT|CALL|"
                     r"EXECUTE|FLOAT64|RAND|CURRENT_\w+|SESSION_USER)\b", select, re.I):
            raise MartError(f"{mart}: SELECT contains a forbidden keyword")
        selects[mart] = select
    return selects


def render(text: str, project: str, dataset: str) -> str:
    """Substitute only the approved identifiers; refuse anything left unresolved."""
    if not PROJECT_RE.fullmatch(project) or not DATASET_RE.fullmatch(dataset):
        raise MartError("project or dataset identifier is not approved-shaped")
    out = text.replace("`PROJECT.DATASET.", f"`{project}.{dataset}.")
    if re.search(r"\bPROJECT\b|\bDATASET\b", out):
        raise MartError("unresolved PROJECT/DATASET placeholder")
    return out


def rendered_script(project: str, dataset: str) -> str:
    text = SCRIPT_SQL_PATH.read_text()
    parse_script(text)
    return render(";\n".join(script_statements(text)) + ";\n", project, dataset)


def sqlite_translation(select: str) -> str:
    """BigQuery SELECT -> SQLite over the packet: bare table names, ``substr`` for the UTC
    date of a ``Z`` timestamp. Nothing else is rewritten, so every other token is tested."""
    return DATE_SOURCE.sub(r"substr(\1, 1, 10)", TABLE_REF.sub(r"\1", select))


# --------------------------------------------------------------------------- reference
def _packet_rows(packet_dir: Path, manifest: dict, table: str) -> list[dict]:
    content = (packet_dir / manifest["tables"][table]["file"]).read_bytes()
    return list(csv.DictReader(io.StringIO(content.decode("utf-8")), strict=True))


def mart_logical_bytes(rows: list[list[str]], schema: list[dict]) -> int:
    total = 0
    for row in rows:
        for value, field in zip(row, schema, strict=True):
            total += 2 + len(value.encode("utf-8")) if field["type"] == "STRING" else 8
    return total


def _csv(header: tuple[str, ...], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def expected_marts(packet_dir: Path, expected_manifest_sha256: str) -> dict:
    """Exact expected mart rows, derived only from the verified packet."""
    if not isinstance(expected_manifest_sha256, str) or not SHA256_RE.fullmatch(
            expected_manifest_sha256):
        raise MartError("an approved manifest SHA-256 (64 hex) is required")
    packet_dir = Path(packet_dir).resolve()
    verified = verify_packet(packet_dir, expected_manifest_sha256=expected_manifest_sha256)
    manifest = verified["manifest"]
    rows = {t: _packet_rows(packet_dir, manifest, t) for t in INPUTS}
    selects = parse_script(SCRIPT_SQL_PATH.read_text())
    schemas = {m: spec["fields"] for m, spec in bigquery_schemas()["tables"].items()}
    with sqlite3.connect(":memory:") as connection:
        reference = _build(connection, {"customers": rows["customers"],
                                        "accounts": rows["accounts"]},
                           rows["curated_transactions"], SQLITE_SQL_PATH.read_text())
        marts: dict[str, dict[str, Any]] = {}
        for mart in MARTS:
            want = [[str(r[c]) for c in COLUMNS[mart]] for r in reference[mart]]
            order = ",".join(str(COLUMNS[mart].index(g) + 1) for g in GRAINS[mart])
            got = [[str(v) for v in r] for r in connection.execute(
                f"SELECT * FROM ({sqlite_translation(selects[mart])}) ORDER BY {order}")]
            if got != want:
                raise MartError(f"{mart}: BigQuery SELECT differs from the M4 reference")
            marts[mart] = {"logical_bytes": mart_logical_bytes(want, schemas[mart]),
                           "row_count": len(want), "rows": want,
                           "csv_sha256": sha256_bytes(_csv(COLUMNS[mart], want))}
    currency = {c: {"amount_minor": v["amount_minor"], "row_count": v["row_count"]}
                for c, v in manifest["reconciliation"]["amount_minor_by_currency"].items()}
    for mart, spec in marts.items():
        if currency_totals(spec["rows"], COLUMNS[mart]) != currency:
            raise MartError(f"{mart}: reference does not reconcile with the packet")
    return {"currency_totals": currency, "manifest_sha256": verified["manifest_sha256"],
            "marts": marts, "packet_id": verified["packet_id"],
            "target": manifest["identity"]["target"],
            "entities": {"customers": sorted(r["customer_id"] for r in rows["customers"]),
                         "accounts": sorted((r["account_id"], r["customer_id"])
                                            for r in rows["accounts"])},
            "selects": selects}


# --------------------------------------------------------------------------- checks
def currency_totals(rows: list[list[str]], columns: tuple[str, ...]) -> dict:
    ci, ni, ai = (columns.index(k) for k in ("currency", "transaction_count",
                                             "total_amount_minor"))
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        entry = totals.setdefault(row[ci], {"amount_minor": 0, "row_count": 0})
        entry["amount_minor"] += int(row[ai])
        entry["row_count"] += int(row[ni])
    return totals


def financial_problems(mart: str, rows: list[list[str]], expected: dict) -> list[str]:
    """Independent accuracy checks on read-back rows (each also implied by exact equality,
    but reported separately so a failure names its cause)."""
    columns, problems = COLUMNS[mart], []
    keys = [tuple(r[columns.index(g)] for g in GRAINS[mart]) for r in rows]
    if len(keys) != len(set(keys)):
        problems.append(f"{mart}: duplicate grain")
    if currency_totals(rows, columns) != expected["currency_totals"]:
        problems.append(f"{mart}: per-currency count/amount differs from the packet")
    for row in rows:
        value = dict(zip(columns, row, strict=True))
        if int(value["transaction_count"]) < 1:
            problems.append(f"{mart}: non-positive transaction_count")
        if not value["first_event_time"] <= value["last_event_time"]:
            problems.append(f"{mart}: first_event_time after last_event_time")
        if {value["first_event_time"][:10], value["last_event_time"][:10]} != {
                value["event_date"]}:
            problems.append(f"{mart}: event time outside its UTC event_date")
        for k in ("active_customer_count", "active_account_count"):
            if k in value and not 1 <= int(value[k]) <= int(value["transaction_count"]):
                problems.append(f"{mart}: {k} outside 1..transaction_count")
    entities = expected["entities"]
    if mart == "customer_daily":
        if {r[columns.index("customer_id")] for r in rows} - set(entities["customers"]):
            problems.append("customer_daily: customer_id without a customer (anti-join)")
    if mart == "account_daily":
        pairs = {(r[columns.index("account_id")], r[columns.index("customer_id")])
                 for r in rows}
        if pairs - {tuple(p) for p in entities["accounts"]}:
            problems.append("account_daily: account/customer pair not in accounts (anti-join)")
    return sorted(set(problems))


# --------------------------------------------------------------------------- cost
def cost_envelope(expected: dict, cap: int) -> dict:
    """On-demand bytes billed. Every statement here reads far under 10 MiB per table, so the
    published minimum (10 MiB per referenced table) dominates. Dry runs and tabledata.list
    are not billed. Upper bound: every DML child billed at the per-job cap."""
    refs = {m: len(set(TABLE_REF.findall(s))) for m, s in expected["selects"].items()}
    statements = {f"delete_{m}": MIN_BILLED_PER_TABLE for m in MARTS}
    # Conservative: the INSERT destination is counted as a referenced table too.
    statements |= {f"insert_{m}": MIN_BILLED_PER_TABLE * (1 + refs[m]) for m in MARTS}
    expected_bytes = sum(statements.values())
    upper = 8 * cap

    def usd(n: int) -> float:
        return round(n / 1024**4 * ILLUSTRATIVE_USD_PER_TIB, 6)

    return {"basis": "illustrative US$10/TiB on-demand, no free tier, before tax; verify "
                     "London pricing (docs/gcp_deployment_readiness.md)",
            "statements_bytes_billed": statements,
            "conservative_bytes_billed": expected_bytes,
            "conservative_usd": usd(expected_bytes), "upper_bound_bytes_billed": upper,
            "upper_bound_usd": usd(upper),
            "storage_logical_bytes": sum(m["logical_bytes"] for m in expected["marts"].values())}


# --------------------------------------------------------------------------- bundle
def runner_email(target: dict) -> str:
    """Derived, never supplied: bootstrap names the runner ``${prefix}-${environment}-runner``
    and the dataset ``${prefix}_${environment}``."""
    return f"{target['dataset_id'].replace('_', '-')}-runner@{target['project_id']}" \
           ".iam.gserviceaccount.com"


def _controls() -> dict:
    controls = json.loads(CONTROLS_PATH.read_text())
    return {"max_bytes_billed": int(controls["max_query_bytes_billed"]),
            "max_jobs": int(controls["max_jobs_per_approved_execution"])}


def derive(packet_dir: Path, *, expected_manifest_sha256: str, window_start: str,
           window_expiry: str, attempt: int = 1,
           repo_dir: Path = REPO_ROOT) -> tuple[dict, dict[str, bytes]]:
    validate_window(window_start, window_expiry)
    if not isinstance(attempt, int) or not 1 <= attempt <= 9:
        raise MartError("attempt must be 1..9")
    expected = expected_marts(packet_dir, expected_manifest_sha256)
    target, controls = expected["target"], _controls()
    project, dataset, location = target["project_id"], target["dataset_id"], target["location"]
    script = rendered_script(project, dataset)
    script_sha = sha256_bytes(script.encode())
    job_id = f"m8_marts_{expected['packet_id'][4:]}_{script_sha[:16]}_a{attempt}"
    reference = {"projectId": project, "location": location}
    files: dict[str, bytes] = {"sql/marts.sql": script.encode()}
    for name in DRY_RUNS:
        query = script if name == "script" else render(expected["selects"][name], project,
                                                       dataset)
        files[f"{REQUESTS}/dryrun-{name}.json"] = canonical_json({
            "configuration": {"dryRun": True, "query": {
                "query": query, "useLegacySql": False, "useQueryCache": False}},
            "jobReference": reference})
    files[f"{REQUESTS}/script.job.json"] = canonical_json({
        "configuration": {"labels": {"milestone": "m8", "packet": expected["packet_id"]},
                          "query": {"maximumBytesBilled": str(controls["max_bytes_billed"]),
                                    "priority": "INTERACTIVE", "query": script,
                                    "useLegacySql": False, "useQueryCache": False}},
        "jobReference": {**reference, "jobId": job_id}})
    for mart, spec in expected["marts"].items():
        files[f"{EXPECTED}/{mart}.csv"] = _csv(COLUMNS[mart], spec["rows"])
    reservation = {"dry_runs": len(DRY_RUNS), "script_parent": 1,
                   "script_children": sum(CHILD_TYPES.values())}
    reservation["total"] = sum(reservation.values())
    if reservation["total"] > controls["max_jobs"]:
        raise MartError("job reservation exceeds the pilot job limit")
    record = {
        "bundle_format": BUNDLE_FORMAT, "bundle_version": BUNDLE_VERSION, "attempt": attempt,
        "cost": cost_envelope(expected, controls["max_bytes_billed"]),
        "currency_totals": expected["currency_totals"],
        "inputs": loaded_table_expectations(Path(packet_dir), expected_manifest_sha256)[
            "tables"],
        "job_reservation": reservation, "manifest_sha256": expected["manifest_sha256"],
        "marts": {m: {k: v for k, v in s.items() if k != "rows"}
                  for m, s in expected["marts"].items()},
        "max_bytes_billed": controls["max_bytes_billed"],
        "packet_dir": _safe_path(Path(packet_dir).resolve(), "packet"),
        "packet_id": expected["packet_id"], "page_size": PAGE_SIZE,
        "repo_dir": _safe_path(Path(repo_dir).resolve(), "repository"),
        "runner_email": runner_email(target), "script_job_id": job_id,
        "script_sha256": script_sha, "target": target,
        "window": {"expiry": window_expiry, "start": window_start},
    }
    return record, files


def _packet_sums(packet_dir: Path, record: dict) -> bytes:
    manifest = json.loads((Path(packet_dir) / "manifest.json").read_bytes())
    lines = [f"{record['manifest_sha256']}  manifest.json"]
    lines += [f"{manifest['tables'][t]['sha256']}  data/{t}.csv" for t in INPUTS]
    return ("\n".join(lines) + "\n").encode("ascii")


def bundle_files(packet_dir: Path, record: dict, files: dict[str, bytes]) -> dict[str, bytes]:
    out = {SCRIPT: render_shell(record).encode(), RECORD: canonical_json(record),
           PACKET_SUMS: _packet_sums(packet_dir, record), **files}
    out[SUMS] = "".join(f"{sha256_bytes(c)}  {n}\n" for n, c in sorted(out.items())).encode()
    return out


def render_bundle(packet_dir: Path, *, expected_manifest_sha256: str, window_start: str,
                  window_expiry: str, output_dir: Path, attempt: int = 1,
                  repo_dir: Path = REPO_ROOT) -> dict:
    output_dir = Path(output_dir).absolute()
    if output_dir.exists() or output_dir.is_symlink():
        raise MartError("bundle directory already exists; bundles are never overwritten")
    parent = output_dir.parent.resolve()
    if not parent.is_dir():
        raise MartError("bundle parent directory does not exist")
    output_dir = parent / output_dir.name
    if _inside_git_worktree(output_dir) or output_dir.is_relative_to(Path(repo_dir).resolve()):
        raise MartError("write the bundle and its evidence to custody, outside any git worktree")
    _safe_path(output_dir, "bundle")
    record, files = derive(packet_dir, expected_manifest_sha256=expected_manifest_sha256,
                           window_start=window_start, window_expiry=window_expiry,
                           attempt=attempt, repo_dir=repo_dir)
    everything = bundle_files(packet_dir, record, files)
    output_dir.mkdir(mode=0o700)
    os.chmod(output_dir, 0o700)
    for sub in sorted({str(Path(n).parent) for n in everything} - {"."}) + [
            EVIDENCE, *(f"{EVIDENCE}/{d}" for d in EVIDENCE_DIRS)]:
        (output_dir / sub).mkdir(mode=0o700)
    for name, content in everything.items():
        descriptor = os.open(output_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
    return {"bundle_sha256": sha256_bytes(everything[SUMS]), "cost": record["cost"],
            "job_reservation": record["job_reservation"],
            "marts": {m: s["row_count"] for m, s in record["marts"].items()},
            "packet_id": record["packet_id"], "runner_email": record["runner_email"],
            "script_job_id": record["script_job_id"], "script_sha256": record["script_sha256"],
            "window": record["window"]}


def load_bundle(bundle_dir: Path, packet_dir: Path, expected_manifest_sha256: str) -> dict:
    """Re-derive every bundle file and require byte identity. Returns the record."""
    bundle_dir = Path(bundle_dir)
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise MartError("bundle directory missing or a symbolic link")
    try:
        recorded = json.loads((bundle_dir / RECORD).read_bytes())
        record, files = derive(packet_dir, expected_manifest_sha256=expected_manifest_sha256,
                               window_start=recorded["window"]["start"],
                               window_expiry=recorded["window"]["expiry"],
                               attempt=recorded["attempt"], repo_dir=Path(recorded["repo_dir"]))
    except (KeyError, TypeError, json.JSONDecodeError, OSError) as error:
        raise MartError(f"bundle record malformed: {error}") from None
    expected = bundle_files(packet_dir, record, files)
    found = {p.relative_to(bundle_dir).as_posix(): p for p in bundle_dir.rglob("*")}
    for name, path in found.items():
        if path.is_symlink():
            raise MartError(f"symbolic link in bundle: {name}")
    files_found = {n for n, p in found.items() if p.is_file()
                   and not (n + "/").startswith(f"{EVIDENCE}/")}
    if files_found != set(expected):
        raise MartError(f"bundle files differ: missing {sorted(set(expected) - files_found)}, "
                        f"unexpected {sorted(files_found - set(expected))}")
    for name, content in expected.items():
        if found[name].read_bytes() != content:
            raise MartError(f"bundle file differs from re-derivation: {name}")
    record["bundle_sha256"] = sha256_bytes(expected[SUMS])
    return record


# --------------------------------------------------------------------------- evidence
def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError):
        raise MartError(f"evidence missing or not JSON: {path.name}") from None


def _job_ok(job: object, label: str) -> list[str]:
    if not isinstance(job, dict):
        return [f"{label}: not a JSON object"]
    status = job.get("status") or {}
    problems = []
    if status.get("state", job.get("state")) != "DONE":
        problems.append(f"{label}: not DONE")
    if status.get("errorResult") or status.get("errors") or job.get("errorResult"):
        problems.append(f"{label}: reported errors")
    return problems


def _decimal(value: object) -> int | None:
    return int(value) if isinstance(value, str) and DECIMAL_RE.fullmatch(value) else None


def check_preflight(ev: Path, record: dict) -> list[str]:
    target, problems = record["target"], []
    marts = bigquery_schemas()["tables"]
    for t in INPUTS:
        meta = _json(ev / "tables" / f"{t}.pre.json")
        exp = record["inputs"][t]
        problems += [p for p in verify_table(meta, {"table": t, "schema": _input_schema(t)},
                                             target, rows=exp["num_rows"])]
        if isinstance(meta, dict) and meta.get("numBytes") != str(exp["num_bytes"]):
            problems.append(f"input {t}: numBytes differs from the M7D-loaded packet")
    for m in MARTS:
        meta = _json(ev / "tables" / f"{m}.pre.json")
        rows = str((meta or {}).get("numRows")) if isinstance(meta, dict) else ""
        allowed = ("0", str(record["marts"][m]["row_count"]))  # first run, or idempotent replay
        if rows not in allowed:
            problems.append(f"mart {m}: numRows {rows} is neither 0 nor the expected count")
            continue
        problems += verify_table(meta, {"table": m, "schema": marts[m]["fields"]}, target,
                                 rows=int(rows))
    return problems


def _input_schema(table: str) -> list[dict]:
    return json.loads((REPO_ROOT / "infrastructure/schemas/v1/source_tables.json")
                      .read_text())[table]


def check_dryrun(ev: Path, record: dict, name: str) -> list[str]:
    job = _json(ev / "dryrun" / f"{name}.json")
    label = f"dry run {name}"
    problems = _job_ok(job, label)
    if not isinstance(job, dict):
        return problems
    if (job.get("configuration") or {}).get("dryRun") is not True:
        problems.append(f"{label}: not a dry run")
    query = (job.get("statistics") or {}).get("query") or {}
    processed = _decimal(query.get("totalBytesProcessed"))
    if processed is None or processed > record["max_bytes_billed"]:
        problems.append(f"{label}: totalBytesProcessed missing or above the per-job cap")
    if name == "script":
        if query.get("statementType") not in (None, "SCRIPT"):
            problems.append(f"{label}: statementType is not SCRIPT")
        return problems
    if query.get("statementType") != "SELECT":
        problems.append(f"{label}: statementType is not SELECT")
    refs = {r.get("tableId") for r in query.get("referencedTables") or []
            if isinstance(r, dict) and r.get("projectId") == record["target"]["project_id"]
            and r.get("datasetId") == record["target"]["dataset_id"]}
    want_refs = {"curated_transactions",
                 *({"customers"} if name == "customer_daily" else set()),
                 *({"accounts"} if name == "account_daily" else set())}
    if refs != want_refs or len(query.get("referencedTables") or []) != len(want_refs):
        problems.append(f"{label}: referenced tables differ from {sorted(want_refs)}")
    fields = (query.get("schema") or {}).get("fields")
    want = [{**f, "mode": "NULLABLE"} for f in bigquery_schemas()["tables"][name]["fields"]]
    got = [{**f, "mode": "NULLABLE"} if isinstance(f, dict) else f for f in fields or []]
    problems += schema_problems(got, want, label)
    return problems


def check_job(ev: Path, record: dict) -> tuple[list[str], dict]:
    job = _json(ev / "jobs" / "script.json")
    label, problems = "script job", _job_ok(job, "script job")
    if not isinstance(job, dict):
        return problems, {}
    request = json.loads((ev.parent / REQUESTS / "script.job.json").read_bytes())
    ref = job.get("jobReference") or {}
    if (ref.get("jobId") != record["script_job_id"]
            or ref.get("projectId") != record["target"]["project_id"]
            or str(ref.get("location", "")).lower() != record["target"]["location"]):
        problems.append(f"{label}: job reference differs from the approved job")
    if job.get("user_email") != record["runner_email"]:
        problems.append(f"{label}: not submitted by the runner")
    query = (job.get("configuration") or {}).get("query") or {}
    if query.get("query") != request["configuration"]["query"]["query"]:
        problems.append(f"{label}: SQL differs from the approved script")
    if str(query.get("maximumBytesBilled")) != str(record["max_bytes_billed"]):
        problems.append(f"{label}: maximumBytesBilled differs from the approved cap")
    stats = job.get("statistics") or {}
    if stats.get("numChildJobs") != str(sum(CHILD_TYPES.values())):
        problems.append(f"{label}: numChildJobs is not {sum(CHILD_TYPES.values())}")
    billed = _decimal(stats.get("totalBytesBilled", (stats.get("query") or {}).get(
        "totalBytesBilled")))
    if billed is None or billed > 8 * record["max_bytes_billed"]:
        problems.append(f"{label}: totalBytesBilled missing or above the ledger bound")
    window = validate_window(record["window"]["start"], record["window"]["expiry"])
    for key in ("creationTime", "startTime", "endTime"):
        if not in_window(parse_epoch_ms(stats.get(key)), window):
            problems.append(f"{label}: {key} outside the approved window")
    return problems, {"bytes_billed": billed, "bytes_processed": _decimal(
        stats.get("totalBytesProcessed"))}


def check_children(ev: Path, record: dict) -> list[str]:
    listing = _json(ev / "jobs" / "children.json")
    jobs = listing.get("jobs") if isinstance(listing, dict) else None
    if not isinstance(jobs, list):
        return ["children: not a jobs.list result"]
    problems = []
    if listing.get("nextPageToken"):
        problems.append("children: listing incomplete (nextPageToken)")
    types: Counter = Counter()
    inserted: Counter = Counter()
    deleted: Counter = Counter()
    for index, job in enumerate(jobs):
        label = f"child {index + 1}"
        problems += _job_ok(job, label)
        if not isinstance(job, dict):
            continue
        if job.get("user_email") != record["runner_email"]:
            problems.append(f"{label}: not run by the runner")
        query = (job.get("statistics") or {}).get("query") or {}
        types[query.get("statementType")] += 1
        billed = _decimal(query.get("totalBytesBilled", "0"))
        if billed is None or billed > record["max_bytes_billed"]:
            problems.append(f"{label}: bytes billed above the per-job cap")
        dml = query.get("dmlStats") or {}
        if query.get("statementType") == "INSERT":
            inserted[dml.get("insertedRowCount")] += 1
        if query.get("statementType") == "DELETE":
            deleted[dml.get("deletedRowCount")] += 1
    if types != CHILD_TYPES:
        problems.append(f"children: statement types {dict(types)} != {dict(CHILD_TYPES)}")
    want_inserted = Counter(str(m["row_count"]) for m in record["marts"].values())
    if inserted and inserted != want_inserted:
        problems.append("children: inserted row counts differ from the expected marts")
    pre = Counter(str((_json(ev / "tables" / f"{m}.pre.json") or {}).get("numRows"))
                  for m in MARTS)
    if deleted and deleted != pre:
        problems.append("children: deleted row counts differ from the preflight mart rows")
    return problems


def check_readback(ev: Path, record: dict, expected: dict, mart: str) -> tuple[list[str], dict]:
    folder = ev / "readback" / mart
    schema = bigquery_schemas()["tables"][mart]["fields"]
    want = expected["marts"][mart]["rows"]
    problems = []
    before, after = _json(folder / "table.before.json"), _json(folder / "table.after.json")
    for meta in (before, after):
        problems += verify_table(meta, {"table": mart, "schema": schema}, record["target"],
                                 rows=len(want))
    if isinstance(before, dict) and isinstance(after, dict) and any(
            before.get(k) != after.get(k) for k in ("numRows", "numBytes", "lastModifiedTime")):
        problems.append(f"{mart}: table changed during read-back")
    if isinstance(after, dict) and after.get("numBytes") != str(
            expected["marts"][mart]["logical_bytes"]):
        problems.append(f"{mart}: numBytes differs from the logical size model")
    pages = []
    for n in range(1, math.ceil(len(want) / PAGE_SIZE) + 2):
        page = folder / f"page-{n:04d}.json"
        if not page.exists():
            break
        pages.append(((folder / f"page-{n:04d}.token").read_text(), _json(page)))
    try:
        rows = convert_pages(pages, schema, len(want))
    except ReadbackError as error:
        return problems + [f"{mart}: {error}"], {}
    order = [COLUMNS[mart].index(g) for g in GRAINS[mart]]
    rows.sort(key=lambda r: [r[i] for i in order])
    if rows != want:
        problems.append(f"{mart}: rows differ from the expected mart")
    problems += financial_problems(mart, rows, expected)
    return problems, {"row_count": len(rows), "csv_sha256": sha256_bytes(_csv(COLUMNS[mart],
                                                                               rows)),
                      "currency_totals": currency_totals(rows, COLUMNS[mart])}


def check_post_inputs(ev: Path, record: dict) -> list[str]:
    problems = []
    for t in INPUTS:
        pre, post = _json(ev / "tables" / f"{t}.pre.json"), _json(ev / "tables" / f"{t}.post.json")
        if not isinstance(pre, dict) or not isinstance(post, dict) or any(
                pre.get(k) != post.get(k) for k in ("numRows", "numBytes", "lastModifiedTime",
                                                    "schema")):
            problems.append(f"input {t}: changed during M8 (inputs must be read-only)")
    return problems


STAGES = ("bundle", "preflight", "dryrun", "job", "children", "readback", "post-inputs", "all")


def run_stage(bundle_dir: Path, packet_dir: Path, manifest_sha256: str, stage: str,
              name: str | None = None) -> dict:
    record = load_bundle(bundle_dir, packet_dir, manifest_sha256)
    ev = Path(bundle_dir) / EVIDENCE
    report: dict = {"bundle_sha256": record["bundle_sha256"], "packet_id": record["packet_id"],
                    "stage": stage}
    problems: list[str] = []
    if stage in ("preflight", "all"):
        problems += check_preflight(ev, record)
    if stage == "dryrun" or stage == "all":
        for n in ([name] if stage == "dryrun" else DRY_RUNS):
            if n not in DRY_RUNS:
                raise MartError(f"unknown dry run {n}")
            problems += check_dryrun(ev, record, n)
    if stage in ("job", "all"):
        found, report["job"] = check_job(ev, record)
        problems += found
    if stage in ("children", "all"):
        problems += check_children(ev, record)
    if stage in ("readback", "all"):
        expected = expected_marts(packet_dir, manifest_sha256)
        report["marts"] = {}
        for m in ([name] if stage == "readback" else MARTS):
            if m not in MARTS:
                raise MartError(f"unknown mart {m}")
            found, report["marts"][m] = check_readback(ev, record, expected, m)
            problems += found
    if stage in ("post-inputs", "all"):
        problems += check_post_inputs(ev, record)
    if stage == "all":
        billed = (report.get("job") or {}).get("bytes_billed") or 0
        report["ledger"] = {"jobs": record["job_reservation"]["total"], "bytes_billed": billed,
                            "usd_at_illustrative_rate": round(
                                billed / 1024**4 * ILLUSTRATIVE_USD_PER_TIB, 6)}
    report.update(problems=problems, ok=not problems)
    return report


# --------------------------------------------------------------------------- shell
def render_shell(record: dict) -> str:
    target = record["target"]
    return SHELL_TEMPLATE.format(
        packet_id=record["packet_id"], manifest=record["manifest_sha256"],
        project=target["project_id"], dataset=target["dataset_id"],
        location=target["location"], runner=record["runner_email"],
        packet_dir=record["packet_dir"], repo_dir=record["repo_dir"],
        start=int(validate_window(**record["window"])["start"].timestamp()),
        expiry=int(validate_window(**record["window"])["expiry"].timestamp()),
        start_text=record["window"]["start"], expiry_text=record["window"]["expiry"],
        job_id=record["script_job_id"], page_size=record["page_size"],
        max_pages_case="\n".join(
            f"    {m}) echo {math.ceil(s['row_count'] / record['page_size']) + 1} ;;"
            for m, s in record["marts"].items()) + "\n    *) die \"unknown mart\" ;;",
        marts=" ".join(MARTS), inputs=" ".join(INPUTS), dryruns=" ".join(DRY_RUNS))


SHELL_TEMPLATE = r"""#!/usr/bin/env bash
# M8 runner bundle for packet {packet_id}: build the four marts. Generated offline. Run ONE
# step at a time, by hand, in Terminal.app, only inside the approved window and only after the
# approvals in docs/upgrade/m8_runbook.md. The assistant never runs this file.
#
#   export M8_APPROVED_BUNDLE_SHA256=<approved bundle SHA-256>
#   bash m8.sh verify-bundle
#   bash m8.sh preflight
#   bash m8.sh dryrun <name>       ({dryruns})
#   bash m8.sh run                 (the one billed script job)
#   bash m8.sh job                 (repeat until DONE)
#   bash m8.sh children
#   bash m8.sh readback <mart>     ({marts})
#   bash m8.sh post-inputs
#   bash m8.sh verify-all
#
# No step creates or deletes tables, changes IAM, raises a limit or retries automatically.
set -euo pipefail
set -o noclobber
umask 077
export LC_ALL=C
unset CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
  GOOGLE_OAUTH_ACCESS_TOKEN GOOGLE_APPLICATION_CREDENTIALS

BUNDLE_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd -P)"
readonly BUNDLE_DIR
readonly MANIFEST_SHA256='{manifest}'
readonly PROJECT='{project}'
readonly DATASET='{dataset}'
readonly LOCATION='{location}'
readonly RUNNER='{runner}'
readonly PACKET_DIR='{packet_dir}'
readonly REPO_DIR='{repo_dir}'
readonly WINDOW_START_EPOCH={start}   # {start_text}
readonly WINDOW_EXPIRY_EPOCH={expiry}  # {expiry_text}
readonly SAFETY_MARGIN=900
readonly JOB_ID='{job_id}'
readonly PAGE_SIZE={page_size}
readonly MARTS='{marts}'
readonly INPUTS='{inputs}'
readonly DRYRUNS='{dryruns}'
readonly EVIDENCE="$BUNDLE_DIR/evidence"
readonly BQ="https://bigquery.googleapis.com/bigquery/v2/projects/$PROJECT"
TOKEN=""

die() {{ printf 'STOP: %s\n' "$*" >&2; exit 1; }}
fresh() {{
  [[ ! -e "$1" ]] || die "evidence already exists, never overwritten: ${{1#$BUNDLE_DIR/}}"
}}
member() {{ [[ " $2 " == *" $1 "* ]] || die "unknown name '$1' (one of: $2)"; }}

approved() {{
  [[ "${{M8_APPROVED_BUNDLE_SHA256:-}}" =~ ^[0-9a-f]{{64}}$ ]] \
    || die "export M8_APPROVED_BUNDLE_SHA256=<approved bundle SHA-256> first"
  (cd "$BUNDLE_DIR" && shasum -a 256 -c --status SHA256SUMS) \
    || die "bundle files differ from SHA256SUMS"
  local got; got="$(shasum -a 256 < "$BUNDLE_DIR/SHA256SUMS")"; got="${{got%% *}}"
  [[ "$got" == "$M8_APPROVED_BUNDLE_SHA256" ]] || die "bundle SHA-256 is not the approved value"
  (cd "$PACKET_DIR" && shasum -a 256 -c --status "$BUNDLE_DIR/packet.sha256") \
    || die "packet bytes differ"
}}

check_window() {{
  local now; now="$(date -u +%s)"
  (( now >= WINDOW_START_EPOCH )) || die "before the approved window"
  (( now < WINDOW_EXPIRY_EPOCH - SAFETY_MARGIN )) || die "too close to or after the approved expiry"
}}

offline() {{
  (cd "$REPO_DIR" && PYTHONPATH=src python3 -m financial_crime_ml.cli verify-mart-evidence \
    --bundle-dir "$BUNDLE_DIR" --packet-dir "$PACKET_DIR" \
    --expected-manifest-sha256 "$MANIFEST_SHA256" "$@")
}}

mint_runner_token() {{
  TOKEN="$(gcloud auth print-access-token --impersonate-service-account="$RUNNER")" \
    || die "runner token could not be minted (no approved runner window)"
  [[ "$TOKEN" =~ ^ya29\.[A-Za-z0-9._-]+$ ]] || die "unexpected token shape"
}}

api() {{  # api METHOD URL OUTFILE [curl options...] -> HTTP status; token never in argv
  local method="$1" url="$2" out="$3"; shift 3
  curl -sS --proto '=https' --tlsv1.2 --max-time 300 -X "$method" \
    -H @<(printf 'Authorization: Bearer %s\n' "$TOKEN") \
    -o "$out" -w '%{{http_code}}' "$@" "$url"
}}

json_field() {{ python3 -I -c 'import json,sys
d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    d=d.get(k, "") if isinstance(d, dict) else ""
print(d)' "$1" "$2"; }}

tables_get() {{  # tables_get TABLE OUTFILE
  fresh "$2"; local code
  code="$(api GET "$BQ/datasets/$DATASET/tables/$1" "$2")" || die "tables.get transport error"
  [[ "$code" == 200 ]] || die "tables.get $1 returned HTTP $code"
}}

max_pages() {{
  case "$1" in
{max_pages_case}
  esac
}}

step_verify_bundle() {{
  approved; offline --stage bundle >/dev/null || die "bundle failed offline re-derivation"
  echo "Bundle verified."
}}

step_preflight() {{
  approved; check_window; mint_runner_token
  local t; for t in $INPUTS $MARTS; do tables_get "$t" "$EVIDENCE/tables/$t.pre.json"; done
  offline --stage preflight
}}

step_dryrun() {{
  member "$1" "$DRYRUNS"; approved; check_window
  offline --stage preflight >/dev/null || die "preflight not verified"
  local out="$EVIDENCE/dryrun/$1.json"; fresh "$out"; mint_runner_token
  local code; code="$(api POST "$BQ/jobs" "$out" -H 'Content-Type: application/json' \
    --data-binary @"$BUNDLE_DIR/requests/dryrun-$1.json")" || die "dry-run transport error"
  [[ "$code" == 200 ]] || die "dry run $1 returned HTTP $code"
  offline --stage dryrun --name "$1"
}}

step_run() {{
  approved; check_window
  offline --stage preflight >/dev/null || die "preflight not verified"
  local d; for d in $DRYRUNS; do
    offline --stage dryrun --name "$d" >/dev/null || die "dry run $d not verified"
  done
  local out="$EVIDENCE/jobs/script.insert.json"; fresh "$out"; mint_runner_token
  local code; code="$(api POST "$BQ/jobs" "$out" -H 'Content-Type: application/json' \
    --data-binary @"$BUNDLE_DIR/requests/script.job.json")" \
    || die "jobs.insert transport error: run 'job'; never resubmit"
  case "$code" in
    200) ;;
    409) die "job $JOB_ID already exists: run 'job'; never resubmit" ;;
    *) die "jobs.insert returned HTTP $code (see ${{out#$BUNDLE_DIR/}})" ;;
  esac
  printf 'Submitted %s. Next: bash m8.sh job\n' "$JOB_ID"
}}

step_job() {{
  approved; check_window
  local out="$EVIDENCE/jobs/script.json"; fresh "$out"; mint_runner_token
  local poll; poll="$(mktemp "$EVIDENCE/jobs/.poll.XXXXXX")"
  local code; code="$(api GET "$BQ/jobs/$JOB_ID?location=$LOCATION" "$poll")" \
    || {{ rm -f "$poll"; die "jobs.get transport error"; }}
  [[ "$code" == 200 ]] || {{ rm -f "$poll"; die "jobs.get returned HTTP $code"; }}
  local state; state="$(json_field "$poll" status.state)"
  if [[ "$state" != DONE ]]; then
    rm -f "$poll"; printf 'Job is %s; run this step again shortly.\n' "$state"; exit 3
  fi
  mv -n "$poll" "$out"; [[ ! -e "$poll" ]] || die "could not record the job result"
  offline --stage job
}}

step_children() {{
  approved; check_window
  offline --stage job >/dev/null || die "script job not verified"
  local out="$EVIDENCE/jobs/children.json"; fresh "$out"; mint_runner_token
  local code; code="$(api GET "$BQ/jobs" "$out" -G --data-urlencode "parentJobId=$JOB_ID" \
    --data-urlencode "allUsers=false" --data-urlencode "projection=full" \
    --data-urlencode "maxResults=50")" || die "jobs.list transport error"
  [[ "$code" == 200 ]] || die "jobs.list returned HTTP $code (record it; never widen roles)"
  offline --stage children
}}

step_readback() {{
  member "$1" "$MARTS"; approved; check_window
  offline --stage job >/dev/null || die "script job not verified"
  local dir="$EVIDENCE/readback/$1"; fresh "$dir"; mkdir "$dir"; mint_runner_token
  tables_get "$1" "$dir/table.before.json"
  local token="" n=1 page code limit; limit="$(max_pages "$1")"
  while :; do
    (( n <= limit )) || die "page limit $limit exceeded"
    page="$(printf '%s/page-%04d' "$dir" "$n")"
    printf '%s' "$token" > "$page.token"
    if [[ -z "$token" ]]; then
      code="$(api GET "$BQ/datasets/$DATASET/tables/$1/data" "$page.json" -G \
        --data-urlencode "maxResults=$PAGE_SIZE" \
        --data-urlencode "formatOptions.useInt64Timestamp=true")" \
        || die "tabledata.list transport error"
    else
      code="$(api GET "$BQ/datasets/$DATASET/tables/$1/data" "$page.json" -G \
        --data-urlencode "maxResults=$PAGE_SIZE" \
        --data-urlencode "formatOptions.useInt64Timestamp=true" \
        --data-urlencode "pageToken=$token")" \
        || die "tabledata.list transport error"
    fi
    [[ "$code" == 200 ]] || die "tabledata.list $1 page $n returned HTTP $code"
    token="$(json_field "$page.json" pageToken)"
    [[ -n "$token" ]] || break
    n=$((n + 1))
  done
  tables_get "$1" "$dir/table.after.json"
  offline --stage readback --name "$1"
}}

step_post_inputs() {{
  approved; check_window; mint_runner_token
  local t; for t in $INPUTS; do tables_get "$t" "$EVIDENCE/tables/$t.post.json"; done
  offline --stage post-inputs
}}

step_verify_all() {{
  approved
  local report="$EVIDENCE/report.json"; fresh "$report"
  offline --stage all > "$report" || die "offline verification failed; see ${{report#$BUNDLE_DIR/}}"
  echo "All evidence verified; report at ${{report#$BUNDLE_DIR/}}"
}}

case "${{1:-}}" in
  verify-bundle) step_verify_bundle ;;
  preflight) step_preflight ;;
  dryrun) step_dryrun "${{2:-}}" ;;
  run) step_run ;;
  job) step_job ;;
  children) step_children ;;
  readback) step_readback "${{2:-}}" ;;
  post-inputs) step_post_inputs ;;
  verify-all) step_verify_all ;;
  *) die "usage: bash m8.sh <step> [name]; see the header of this file" ;;
esac
"""


# --------------------------------------------------------------------------- CLI
def render_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml render-mart-bundle")
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-expiry", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        result = render_bundle(**vars(args))
    except (MartError, BundleError, OSError) as error:
        parser.exit(1, f"M8 bundle rejected: {error}\n")
    print(json.dumps(result, indent=1, sort_keys=True))


def expected_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml mart-expectations")
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        expected = expected_marts(args.packet_dir, args.expected_manifest_sha256)
    except (MartError, BundleError, OSError) as error:
        parser.exit(1, f"M8 expectations rejected: {error}\n")
    controls = _controls()
    print(json.dumps({"packet_id": expected["packet_id"],
                      "currency_totals": expected["currency_totals"],
                      "marts": {m: {k: v for k, v in s.items() if k != "rows"}
                                for m, s in expected["marts"].items()},
                      "cost": cost_envelope(expected, controls["max_bytes_billed"])},
                     indent=1, sort_keys=True))


def verify_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml verify-mart-evidence")
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--name")
    args = parser.parse_args(argv)
    try:
        report = run_stage(args.bundle_dir, args.packet_dir, args.expected_manifest_sha256,
                           args.stage, args.name)
    except (MartError, BundleError, OSError) as error:
        print(json.dumps({"ok": False, "problems": [str(error)]}))
        raise SystemExit(1) from None
    print(json.dumps(report, indent=1, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)
