"""M9D runner bundle: score the 4,000-row M7B pilot packet in BigQuery with spec v2.

Offline and pure except for writing a new bundle directory; nothing here calls a cloud API.
Reuses the M8 pattern (``marts_bundle``): a deterministic record and requests re-derived and
byte-compared at every step, one operator script, and a staged offline verifier.

* Model: the M9C run's ``model_spec.json`` (spec v2), verified against that run's manifest,
  its approval record and its own SHA-256; the GoogleSQL is regenerated from the spec and
  must equal the M9C artifacts byte for byte.
* Expected output: ``score_python(spec, packet curated_transactions)``: exact INT64
  ``score_ppm``, alerts and reason codes for every packet row. No labels exist in the packet.
* Jobs (≤ 20): 1 preflight query, 2 dry runs, 1 script parent, 4 script children = 8.
  Every billable job carries ``maximumBytesBilled = 104857600``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from financial_crime_ml.deployment import model_spec as ms
from financial_crime_ml.deployment import scoring_sql as sq
from financial_crime_ml.deployment.ingestion_bundle import (
    SHA256_RE,
    BundleError,
    _inside_git_worktree,
    _safe_path,
    loaded_table_expectations,
    validate_window,
)
from financial_crime_ml.deployment.ingestion_evidence import (
    in_window,
    parse_epoch_ms,
    verify_table,
)
from financial_crime_ml.deployment.ingestion_packet import canonical_json, verify_packet
from financial_crime_ml.deployment.ingestion_readback import ReadbackError, convert_pages
from financial_crime_ml.deployment.marts_bundle import (
    _controls,
    _decimal,
    _job_ok,
    _json,
    expected_marts,
    render,
    runner_email,
)
from financial_crime_ml.evaluation.behavioural import MODEL_FEATURES
from financial_crime_ml.evaluation.manifest import sha256_bytes
from financial_crime_ml.ingestion.load_data import REPO_ROOT
from financial_crime_ml.monitoring import reference_profile as rp

BUNDLE_FORMAT, BUNDLE_VERSION = "m9d-scoring-bundle", "1.0.0"
SCRIPT, RECORD, SUMS, PACKET_SUMS = "m9d.sh", "bundle.json", "SHA256SUMS", "packet.sha256"
REQUESTS, EVIDENCE, EXPECTED = "requests", "evidence", "expected"
EVIDENCE_DIRS = ("tables", "preflight", "dryrun", "jobs", "readback")
INPUTS = ("customers", "accounts", "curated_transactions")
MARTS = ("transaction_daily", "customer_daily", "account_daily", "operations_daily")
SCORE_TABLE = sq.TARGET_TABLE
DRY_RUNS = ("select", "refresh")
CHILD_TYPES = Counter({"BEGIN_TRANSACTION": 1, "DELETE": 1, "INSERT": 1,
                       "COMMIT_TRANSACTION": 1})
PAGE_SIZE = 1000
NULL_SENTINEL = "\u0000NULL"
SCHEMA_PATH = REPO_ROOT / "sql/scoring/v1/transaction_risk_scores.schema.json"


class ScoringError(ValueError):
    """Any M9D derivation, rendering or evidence verification failure."""


# --------------------------------------------------------------------------- M9C input
def load_m9c(run_dir: Path) -> dict[str, Any]:
    """A completed M9C spec-v2 run: every artifact hash, the spec hash and regenerated SQL."""
    run_dir = Path(run_dir).resolve()
    manifest = json.loads((run_dir / "manifest.json").read_bytes())
    if manifest.get("milestone") != "M9C" or manifest.get("status") != "completed" or \
            manifest.get("spec_version") != ms.SPEC_V2:
        raise ScoringError("require a completed M9C run with spec v2")
    contents = {}
    for name, digest in manifest["artifacts_sha256"].items():
        content = (run_dir / name).read_bytes()
        if sha256_bytes(content) != digest:
            raise ScoringError(f"M9C artifact hash mismatch: {name}")
        contents[name] = content
    spec = json.loads(contents["model_spec.json"])
    if ms.canonical(spec) != contents["model_spec.json"] or \
            ms.spec_sha256(spec) != manifest["model_spec_sha256"]:
        raise ScoringError("model specification hash mismatch")
    approval = spec["provenance"].get("approval") or {}
    if approval.get("record_sha256") != sha256_bytes(contents["approval_record.json"]) or \
            approval.get("production_approval") != "pending":
        raise ScoringError("approval record does not match the specification")
    for name, text in (("scoring_select.googlesql.sql", sq.render_select(spec, sq.GOOGLESQL)),
                       ("scoring_refresh.googlesql.sql", sq.render_insert(spec)),
                       ("preflight.googlesql.sql", sq.render_preflight(spec))):
        if contents[name] != text.encode():
            raise ScoringError(f"{name} is not the SQL generated from the specification")
    if json.loads(contents["transaction_risk_scores.schema.json"]) != \
            json.loads(SCHEMA_PATH.read_text()):
        raise ScoringError("score table schema differs from the repository contract")
    return {"run_dir": run_dir, "manifest": manifest, "spec": spec, "contents": contents,
            "manifest_sha256": sha256_bytes((run_dir / "manifest.json").read_bytes())}


def expected_scores(spec: dict[str, Any], packet_dir: Path, manifest: dict) -> pd.DataFrame:
    """Python reference scores for exactly the packet rows (all must be valid)."""
    content = (Path(packet_dir) / manifest["tables"]["curated_transactions"]["file"]).read_bytes()
    scored = ms.score_python(spec, pd.read_csv(io.BytesIO(content), dtype=str))
    if (scored["input_valid"] != 1).any():
        raise ScoringError("the pilot packet contains rows the specification rejects")
    rows = scored.loc[:, ["transaction_id", "score_ppm", "alert",
                          *(f"reason_code_{i}" for i in range(1, ms.REASON_SLOTS + 1)),
                          *MODEL_FEATURES]].copy()
    return rows.sort_values("transaction_id", kind="stable").reset_index(drop=True)


def expected_rows(spec: dict[str, Any], scores: pd.DataFrame) -> list[list[str | None]]:
    """Score-table rows exactly as tabledata.list returns them (strings, NULLs as None)."""
    sha = ms.spec_sha256(spec)
    out: list[list[str | None]] = []
    for r in scores.to_dict("records"):
        codes = [r[f"reason_code_{i}"] for i in range(1, ms.REASON_SLOTS + 1)]
        out.append([str(r["transaction_id"]), sha, "1", str(int(r["score_ppm"])),
                    str(int(r["alert"])), *[c if isinstance(c, str) else None for c in codes]])
    return out


# --------------------------------------------------------------------------- derivation
def _request_id(*parts: str) -> str:
    return str(uuid.UUID(hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]))


def derive(packet_dir: Path, m9c_run: Path, *, expected_manifest_sha256: str,
           window_start: str, window_expiry: str, attempt: int = 1,
           repo_dir: Path = REPO_ROOT) -> tuple[dict, dict[str, bytes]]:
    validate_window(window_start, window_expiry)
    if not isinstance(attempt, int) or not 1 <= attempt <= 9:
        raise ScoringError("attempt must be 1..9")
    if not isinstance(expected_manifest_sha256, str) or not SHA256_RE.fullmatch(
            expected_manifest_sha256):
        raise ScoringError("an approved manifest SHA-256 (64 hex) is required")
    packet_dir = Path(packet_dir).resolve()
    verified = verify_packet(packet_dir, expected_manifest_sha256=expected_manifest_sha256)
    m9c = load_m9c(Path(m9c_run))
    spec, sha = m9c["spec"], m9c["manifest"]["model_spec_sha256"]
    target = verified["manifest"]["identity"]["target"]
    project, dataset, location = target["project_id"], target["dataset_id"], target["location"]
    controls = _controls()
    cap = str(controls["max_bytes_billed"])
    select = render(sq.render_select(spec, sq.GOOGLESQL), project, dataset)
    refresh = render(sq.render_insert(spec), project, dataset)
    preflight = render(sq.render_preflight(spec), project, dataset)
    job_id = f"m9d_scores_{verified['packet_id'][4:]}_{sha[:16]}_a{attempt}"
    reference = {"projectId": project, "location": location}
    files: dict[str, bytes] = {"sql/select.sql": select.encode(),
                               "sql/refresh.sql": refresh.encode(),
                               "sql/preflight.sql": preflight.encode(),
                               "model_spec.json": m9c["contents"]["model_spec.json"],
                               "monitoring_reference.json":
                               m9c["contents"]["monitoring_reference.json"]}
    files[f"{REQUESTS}/preflight.query.json"] = canonical_json({
        "query": preflight, "useLegacySql": False, "useQueryCache": False,
        "maximumBytesBilled": cap, "location": location, "timeoutMs": 60000,
        "requestId": _request_id(job_id, "preflight"),
        "labels": {"milestone": "m9d", "packet": verified["packet_id"]}})
    for name, query in (("select", select), ("refresh", refresh)):
        files[f"{REQUESTS}/dryrun-{name}.json"] = canonical_json({
            "configuration": {"dryRun": True, "query": {
                "query": query, "useLegacySql": False, "useQueryCache": False}},
            "jobReference": reference})
    files[f"{REQUESTS}/refresh.job.json"] = canonical_json({
        "configuration": {"labels": {"milestone": "m9d", "packet": verified["packet_id"]},
                          "query": {"maximumBytesBilled": cap, "priority": "INTERACTIVE",
                                    "query": refresh, "useLegacySql": False,
                                    "useQueryCache": False}},
        "jobReference": {**reference, "jobId": job_id}})
    scores = expected_scores(spec, packet_dir, verified["manifest"])
    buffer = io.StringIO()
    scores.loc[:, list(sq.OUTPUT_COLUMNS[:1]) + ["score_ppm", "alert",
                                                 *(f"reason_code_{i}" for i in (1, 2, 3))]
               ].to_csv(buffer, index=False, lineterminator="\n")
    files[f"{EXPECTED}/scores.csv"] = buffer.getvalue().encode()
    inputs = loaded_table_expectations(packet_dir, expected_manifest_sha256)["tables"]
    marts = {m: {"num_rows": s["row_count"], "num_bytes": s["logical_bytes"]} for m, s in
             expected_marts(packet_dir, expected_manifest_sha256)["marts"].items()}
    reservation = {"preflight_query": 1, "dry_runs": len(DRY_RUNS), "script_parent": 1,
                   "script_children": sum(CHILD_TYPES.values())}
    reservation["total"] = sum(reservation.values())
    if reservation["total"] > controls["max_jobs"]:
        raise ScoringError("job reservation exceeds the pilot job limit")
    record = {
        "bundle_format": BUNDLE_FORMAT, "bundle_version": BUNDLE_VERSION, "attempt": attempt,
        "packet_id": verified["packet_id"], "manifest_sha256": verified["manifest_sha256"],
        "packet_dir": _safe_path(packet_dir, "packet"),
        "repo_dir": _safe_path(Path(repo_dir).resolve(), "repository"),
        "m9c_run_dir": _safe_path(m9c["run_dir"], "M9C run"),
        "m9c_manifest_sha256": m9c["manifest_sha256"], "model_spec_sha256": sha,
        "spec_version": spec["spec_version"], "threshold_ppm": spec["decision"]["threshold_ppm"],
        "target": target, "runner_email": runner_email(target), "script_job_id": job_id,
        "max_bytes_billed": controls["max_bytes_billed"], "job_reservation": reservation,
        "expected": {"rows": int(len(scores)), "alerts": int(scores["alert"].sum()),
                     "scores_sha256": sha256_bytes(files[f"{EXPECTED}/scores.csv"])},
        "inputs": inputs, "marts": marts, "page_size": PAGE_SIZE,
        "window": {"expiry": window_expiry, "start": window_start},
        "cost": {"basis": "illustrative US$10/TiB on-demand; M8 billing evidence",
                 "expected_bytes_billed": 30 * 1024**2,
                 "upper_bound_bytes_billed": 3 * controls["max_bytes_billed"],
                 "note": "preflight query (10 MiB minimum) + INSERT (source and destination, "
                         "10 MiB each); DELETE on an empty table bills 0; dry runs and "
                         "tabledata.list are free"}}
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


def render_bundle(packet_dir: Path, m9c_run: Path, *, expected_manifest_sha256: str,
                  window_start: str, window_expiry: str, output_dir: Path, attempt: int = 1,
                  repo_dir: Path = REPO_ROOT) -> dict:
    output_dir = Path(output_dir).absolute()
    if output_dir.exists() or output_dir.is_symlink():
        raise ScoringError("bundle directory already exists; bundles are never overwritten")
    parent = output_dir.parent.resolve()
    if not parent.is_dir():
        raise ScoringError("bundle parent directory does not exist")
    output_dir = parent / output_dir.name
    if _inside_git_worktree(output_dir) or output_dir.is_relative_to(Path(repo_dir).resolve()):
        raise ScoringError("write the bundle and its evidence to custody, outside any git "
                           "worktree")
    _safe_path(output_dir, "bundle")
    record, files = derive(packet_dir, m9c_run, expected_manifest_sha256=expected_manifest_sha256,
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
    return {"bundle_sha256": sha256_bytes(everything[SUMS]),
            "model_spec_sha256": record["model_spec_sha256"],
            "script_job_id": record["script_job_id"], "expected": record["expected"],
            "job_reservation": record["job_reservation"], "cost": record["cost"],
            "runner_email": record["runner_email"], "window": record["window"]}


def load_bundle(bundle_dir: Path, packet_dir: Path, expected_manifest_sha256: str) -> dict:
    """Re-derive every bundle file from the packet, the M9C run and the recorded inputs."""
    bundle_dir = Path(bundle_dir)
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise ScoringError("bundle directory missing or a symbolic link")
    try:
        recorded = json.loads((bundle_dir / RECORD).read_bytes())
        record, files = derive(packet_dir, Path(recorded["m9c_run_dir"]),
                               expected_manifest_sha256=expected_manifest_sha256,
                               window_start=recorded["window"]["start"],
                               window_expiry=recorded["window"]["expiry"],
                               attempt=recorded["attempt"],
                               repo_dir=Path(recorded["repo_dir"]))
    except (KeyError, TypeError, json.JSONDecodeError, OSError) as error:
        raise ScoringError(f"bundle record malformed: {error}") from None
    expected = bundle_files(packet_dir, record, files)
    found = {p.relative_to(bundle_dir).as_posix(): p for p in bundle_dir.rglob("*")}
    if any(p.is_symlink() for p in found.values()):
        raise ScoringError("symbolic link in bundle")
    names = {n for n, p in found.items() if p.is_file() and not n.startswith(f"{EVIDENCE}/")}
    if names != set(expected):
        raise ScoringError(f"bundle files differ: missing {sorted(set(expected) - names)}, "
                           f"unexpected {sorted(names - set(expected))}")
    for name, content in expected.items():
        if found[name].read_bytes() != content:
            raise ScoringError(f"bundle file differs from re-derivation: {name}")
    record["bundle_sha256"] = sha256_bytes(expected[SUMS])
    return record


# --------------------------------------------------------------------------- evidence
def score_schema() -> list[dict]:
    return json.loads(SCHEMA_PATH.read_text())["fields"]


def _source_schema(table: str) -> list[dict]:
    return json.loads((REPO_ROOT / "infrastructure/schemas/v1/source_tables.json")
                      .read_text())[table]


def _mart_schema(table: str) -> list[dict]:
    return json.loads((REPO_ROOT / "sql/analytics/v1/bigquery.schemas.json").read_text())[
        "tables"][table]["fields"]


def check_tables(ev: Path, record: dict, phase: str) -> tuple[list[str], int | None]:
    """Inputs exactly the packet, marts exactly M8, score table present with the contract."""
    problems: list[str] = []
    target = record["target"]
    for t in INPUTS:
        meta = _json(ev / "tables" / f"{t}.{phase}.json")
        spec = record["inputs"][t]
        problems += verify_table(meta, {"table": t, "schema": _source_schema(t)}, target,
                                 rows=spec["num_rows"])
        if isinstance(meta, dict) and meta.get("numBytes") != str(spec["num_bytes"]):
            problems.append(f"input {t}: numBytes differs from the packet")
    for t in MARTS:
        meta = _json(ev / "tables" / f"{t}.{phase}.json")
        spec = record["marts"][t]
        problems += verify_table(meta, {"table": t, "schema": _mart_schema(t)}, target,
                                 rows=spec["num_rows"])
        if isinstance(meta, dict) and meta.get("numBytes") != str(spec["num_bytes"]):
            problems.append(f"mart {t}: numBytes differs from the M8-verified value")
    score_rows = None
    if phase == "pre":
        meta = _json(ev / "tables" / f"{SCORE_TABLE}.pre.json")
        rows = str(meta.get("numRows")) if isinstance(meta, dict) else ""
        if rows not in ("0", str(record["expected"]["rows"])):  # first run or exact replay
            problems.append(f"score table: numRows {rows} is neither 0 nor the expected count")
        else:
            score_rows = int(rows)
            problems += verify_table(meta, {"table": SCORE_TABLE, "schema": score_schema()},
                                     target, rows=score_rows)
    return problems, score_rows


def check_preflight(ev: Path, record: dict) -> list[str]:
    problems, _ = check_tables(ev, record, "pre")
    response = _json(ev / "preflight" / "query.json")
    if not isinstance(response, dict) or response.get("jobComplete") is not True:
        return problems + ["preflight query did not complete"]
    rows = response.get("rows") or []
    names = [f.get("name") for f in (response.get("schema") or {}).get("fields") or []]
    want = ["row_count", "duplicate_ids", "invalid_rows", "sub_second_timestamps"]
    if names != want or len(rows) != 1:
        return problems + ["preflight result shape differs"]
    values = [c.get("v") for c in rows[0].get("f", [])]
    expected = [str(record["expected"]["rows"]), "0", "0", "0"]
    if values != expected:
        problems.append(f"preflight counts {dict(zip(want, values, strict=False))} != "
                        f"{dict(zip(want, expected, strict=True))}")
    processed = _decimal(response.get("totalBytesProcessed"))
    if processed is None or processed > record["max_bytes_billed"]:
        problems.append("preflight bytes processed missing or above the per-job cap")
    return problems


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
    want_type = "SELECT" if name == "select" else "SCRIPT"
    if query.get("statementType", want_type) != want_type:
        problems.append(f"{label}: statementType is not {want_type}")
    if name == "select":
        refs = {(r.get("datasetId"), r.get("tableId")) for r in query.get("referencedTables")
                or [] if isinstance(r, dict)}
        if refs != {(record["target"]["dataset_id"], "curated_transactions")}:
            problems.append(f"{label}: may read only curated_transactions")
    return problems


def check_job(ev: Path, record: dict) -> tuple[list[str], dict]:
    job = _json(ev / "jobs" / "refresh.json")
    label, problems = "scoring job", _job_ok(job, "scoring job")
    if not isinstance(job, dict):
        return problems, {}
    request = json.loads((ev.parent / REQUESTS / "refresh.job.json").read_bytes())
    ref = job.get("jobReference") or {}
    if ref.get("jobId") != record["script_job_id"] or \
            ref.get("projectId") != record["target"]["project_id"]:
        problems.append(f"{label}: job reference differs from the approved job")
    if job.get("user_email") != record["runner_email"]:
        problems.append(f"{label}: not submitted by the runner")
    query = (job.get("configuration") or {}).get("query") or {}
    if query.get("query") != request["configuration"]["query"]["query"]:
        problems.append(f"{label}: SQL differs from the approved refresh script")
    if str(query.get("maximumBytesBilled")) != str(record["max_bytes_billed"]):
        problems.append(f"{label}: maximumBytesBilled differs from the approved cap")
    stats = job.get("statistics") or {}
    if stats.get("numChildJobs") != str(sum(CHILD_TYPES.values())):
        problems.append(f"{label}: numChildJobs is not {sum(CHILD_TYPES.values())}")
    billed = _decimal(stats.get("totalBytesBilled", (stats.get("query") or {}).get(
        "totalBytesBilled")))
    if billed is None or billed > 2 * record["max_bytes_billed"]:
        problems.append(f"{label}: totalBytesBilled missing or above the ledger bound")
    window = validate_window(record["window"]["start"], record["window"]["expiry"])
    for key in ("creationTime", "startTime", "endTime"):
        if not in_window(parse_epoch_ms(stats.get(key)), window):
            problems.append(f"{label}: {key} outside the approved window")
    return problems, {"bytes_billed": billed}


def check_children(ev: Path, record: dict) -> list[str]:
    listing = _json(ev / "jobs" / "children.json")
    jobs = listing.get("jobs") if isinstance(listing, dict) else None
    if not isinstance(jobs, list):
        return ["children: not a jobs.list result"]
    problems = ["children: listing incomplete"] if listing.get("nextPageToken") else []
    types: Counter = Counter()
    for index, job in enumerate(jobs):
        label = f"child {index + 1}"
        problems += _job_ok(job, label)
        if not isinstance(job, dict):
            continue
        if job.get("user_email") != record["runner_email"]:
            problems.append(f"{label}: not run by the runner")
        query = (job.get("statistics") or {}).get("query") or {}
        kind = query.get("statementType")
        types[kind] += 1
        billed = _decimal(query.get("totalBytesBilled", "0"))
        if billed is None or billed > record["max_bytes_billed"]:
            problems.append(f"{label}: bytes billed above the per-job cap")
        dml = query.get("dmlStats") or {}
        if kind == "INSERT" and dml.get("insertedRowCount") != str(record["expected"]["rows"]):
            problems.append("children: inserted row count differs from the packet")
        if kind == "DELETE":
            pre = _json(ev / "tables" / f"{SCORE_TABLE}.pre.json")
            if dml.get("deletedRowCount") != str((pre or {}).get("numRows")):
                problems.append("children: deleted row count differs from the preflight")
    if types != CHILD_TYPES:
        problems.append(f"children: statement types {dict(types)} != {dict(CHILD_TYPES)}")
    return problems


def _readback_rows(folder: Path, expected_rows: int) -> list[list[str | None]]:
    fields = score_schema()
    pages = []
    for n in range(1, math.ceil(expected_rows / PAGE_SIZE) + 2):
        page_path = folder / f"page-{n:04d}.json"
        if not page_path.exists():
            break
        page = _json(page_path)
        for row in (page.get("rows") or []) if isinstance(page, dict) else []:
            for cell, field in zip(row.get("f", []), fields, strict=False):
                if isinstance(cell, dict) and cell.get("v") is None and \
                        field["mode"] == "NULLABLE" and field["type"] == "STRING":
                    cell["v"] = NULL_SENTINEL  # reason codes only; INT64 NULLs fail closed
        pages.append(((folder / f"page-{n:04d}.token").read_text(), page))
    rows = convert_pages(pages, fields, expected_rows)
    return [[None if v == NULL_SENTINEL else v for v in row] for row in rows]


def check_readback(ev: Path, record: dict, packet_dir: Path) -> tuple[list[str], dict]:
    folder = ev / "readback" / SCORE_TABLE
    n = record["expected"]["rows"]
    problems: list[str] = []
    before, after = _json(folder / "table.before.json"), _json(folder / "table.after.json")
    if any(not isinstance(m, dict) or m.get("numRows") != str(n) for m in (before, after)):
        problems.append("score table: numRows differs from the packet")
    if isinstance(before, dict) and isinstance(after, dict) and any(
            before.get(k) != after.get(k) for k in ("numRows", "numBytes", "lastModifiedTime")):
        problems.append("score table changed during read-back")
    try:
        rows = sorted(_readback_rows(folder, n), key=lambda r: str(r[0]))
    except ReadbackError as error:
        return problems + [f"score table: {error}"], {}
    spec = json.loads((ev.parent / "model_spec.json").read_bytes())
    manifest = json.loads((Path(packet_dir) / "manifest.json").read_bytes())
    scores = expected_scores(spec, packet_dir, manifest)
    want = expected_rows(spec, scores)
    columns = list(sq.OUTPUT_COLUMNS)
    mismatches = {c: sum(1 for g, w in zip(rows, want, strict=False) if g[i] != w[i])
                  for i, c in enumerate(columns)}
    if len(rows) != len(want) or [r[0] for r in rows] != [w[0] for w in want]:
        problems.append("score table transaction ids differ from the packet")
    problems += [f"score parity: {c} differs on {k} rows" for c, k in mismatches.items() if k]
    cloud = pd.DataFrame(rows, columns=columns)
    cloud_ppm = cloud["score_ppm"].astype("int64").to_numpy()
    reference = json.loads((ev.parent / "monitoring_reference.json").read_bytes())
    score_psi = rp.psi(reference["score_ppm"]["proportions"], rp.score_proportions(cloud_ppm))
    features = rp.compare(reference, scores.assign(score_ppm=scores["score_ppm"].astype(int)))
    return problems, {
        "rows": len(rows), "alerts": int(cloud["alert"].astype(int).sum()),
        "parity_mismatches": mismatches,
        "monitoring": {
            "score_ppm_psi_cloud": score_psi, "score_ppm_band": rp.band(score_psi),
            "alert_rate_cloud": float(cloud["alert"].astype(int).mean()),
            "alert_rate_reference": reference["alert_rate"],
            "feature_psi_offline_packet": {k: v for k, v in features.items()
                                           if k not in ("score_ppm", "alert_rate")},
            "note": "cloud stores scores only; feature PSI uses the Python features of the same "
                    "packet, equal to the cloud features by the M9C parity contract"}}


def check_post(ev: Path, record: dict) -> list[str]:
    problems, _ = check_tables(ev, record, "post")
    for t in (*INPUTS, *MARTS):
        pre, post = _json(ev / "tables" / f"{t}.pre.json"), _json(ev / "tables" / f"{t}.post.json")
        if not isinstance(pre, dict) or not isinstance(post, dict) or any(
                pre.get(k) != post.get(k) for k in ("numRows", "numBytes", "lastModifiedTime",
                                                    "schema")):
            problems.append(f"{t}: changed during M9D (inputs and marts are read-only)")
    return problems


STAGES = ("bundle", "preflight", "dryrun", "job", "children", "readback", "post", "all")


def run_stage(bundle_dir: Path, packet_dir: Path, manifest_sha256: str, stage: str,
              name: str | None = None) -> dict:
    record = load_bundle(bundle_dir, packet_dir, manifest_sha256)
    ev = Path(bundle_dir) / EVIDENCE
    report: dict = {"bundle_sha256": record["bundle_sha256"],
                    "model_spec_sha256": record["model_spec_sha256"], "stage": stage}
    problems: list[str] = []
    if stage in ("preflight", "all"):
        problems += check_preflight(ev, record)
    if stage in ("dryrun", "all"):
        for n in ([name] if stage == "dryrun" else DRY_RUNS):
            if n not in DRY_RUNS:
                raise ScoringError(f"unknown dry run {n}")
            problems += check_dryrun(ev, record, n)
    if stage in ("job", "all"):
        found, report["job"] = check_job(ev, record)
        problems += found
    if stage in ("children", "all"):
        problems += check_children(ev, record)
    if stage in ("readback", "all"):
        found, report["readback"] = check_readback(ev, record, packet_dir)
        problems += found
    if stage in ("post", "all"):
        problems += check_post(ev, record)
    if stage == "all":
        billed = (report.get("job") or {}).get("bytes_billed") or 0
        report["ledger"] = {"jobs": record["job_reservation"]["total"],
                            "script_bytes_billed": billed,
                            "usd_at_illustrative_rate": round(billed / 1024**4 * 10, 6)}
    report.update(problems=problems, ok=not problems)
    return report


# --------------------------------------------------------------------------- shell
def render_shell(record: dict) -> str:
    target = record["target"]
    window = validate_window(**record["window"])
    return SHELL_TEMPLATE.format(
        manifest=record["manifest_sha256"], project=target["project_id"],
        dataset=target["dataset_id"], location=target["location"],
        runner=record["runner_email"], packet_dir=record["packet_dir"],
        repo_dir=record["repo_dir"], start=int(window["start"].timestamp()),
        expiry=int(window["expiry"].timestamp()), start_text=record["window"]["start"],
        expiry_text=record["window"]["expiry"], job_id=record["script_job_id"],
        spec=record["model_spec_sha256"], page_size=record["page_size"],
        max_pages=math.ceil(record["expected"]["rows"] / record["page_size"]) + 1,
        tables=" ".join((*INPUTS, *MARTS)), score=SCORE_TABLE)


SHELL_TEMPLATE = r"""#!/usr/bin/env bash
# M9D runner bundle: score the M7B pilot packet with model spec {spec}.
# Generated offline. Run ONE step at a time, by hand, in Terminal.app, inside the approved
# runner window, after the approvals in docs/upgrade/m9d_runbook.md. Never in Claude Code.
#
#   export M9D_APPROVED_BUNDLE_SHA256=<approved bundle SHA-256>
#   bash m9d.sh verify-bundle
#   bash m9d.sh preflight          (tables.get x8 + one read-only preflight query)
#   bash m9d.sh dryrun select      then: bash m9d.sh dryrun refresh
#   bash m9d.sh run                (the one billed scoring script)
#   bash m9d.sh job                (repeat until DONE)
#   bash m9d.sh children
#   bash m9d.sh readback
#   bash m9d.sh post
#   bash m9d.sh verify-all
#
# No step creates or drops tables, changes IAM, raises a limit or retries automatically.
set -euo pipefail
set -o noclobber
umask 077
export LC_ALL=C
unset CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
  GOOGLE_OAUTH_ACCESS_TOKEN GOOGLE_APPLICATION_CREDENTIALS

BUNDLE_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd -P)"
readonly BUNDLE_DIR
readonly MANIFEST_SHA256='{manifest}'
readonly MODEL_SPEC_SHA256='{spec}'
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
readonly MAX_PAGES={max_pages}
readonly TABLES='{tables}'
readonly SCORE='{score}'
readonly EVIDENCE="$BUNDLE_DIR/evidence"
readonly BQ="https://bigquery.googleapis.com/bigquery/v2/projects/$PROJECT"
TOKEN=""

die() {{ printf 'STOP: %s\n' "$*" >&2; exit 1; }}
fresh() {{
  [[ ! -e "$1" ]] || die "evidence already exists, never overwritten: ${{1#$BUNDLE_DIR/}}"
}}

approved() {{
  [[ "${{M9D_APPROVED_BUNDLE_SHA256:-}}" =~ ^[0-9a-f]{{64}}$ ]] \
    || die "export M9D_APPROVED_BUNDLE_SHA256=<approved bundle SHA-256> first"
  (cd "$BUNDLE_DIR" && shasum -a 256 -c --status SHA256SUMS) \
    || die "bundle files differ from SHA256SUMS"
  local got; got="$(shasum -a 256 < "$BUNDLE_DIR/SHA256SUMS")"; got="${{got%% *}}"
  [[ "$got" == "$M9D_APPROVED_BUNDLE_SHA256" ]] || die "bundle SHA-256 is not the approved value"
  got="$(shasum -a 256 < "$BUNDLE_DIR/model_spec.json")"; got="${{got%% *}}"
  [[ "$got" == "$MODEL_SPEC_SHA256" ]] || die "model specification hash differs"
  (cd "$PACKET_DIR" && shasum -a 256 -c --status "$BUNDLE_DIR/packet.sha256") \
    || die "packet bytes differ"
}}

check_window() {{
  local now; now="$(date -u +%s)"
  (( now >= WINDOW_START_EPOCH )) || die "before the approved window"
  (( now < WINDOW_EXPIRY_EPOCH - SAFETY_MARGIN )) || die "too close to or after the approved expiry"
}}

offline() {{
  (cd "$REPO_DIR" && PYTHONPATH=src python3 -m financial_crime_ml.cli verify-scoring-evidence \
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

step_verify_bundle() {{
  approved; offline --stage bundle >/dev/null || die "bundle failed offline re-derivation"
  echo "Bundle verified."
}}

step_preflight() {{
  approved; check_window; mint_runner_token
  local t; for t in $TABLES $SCORE; do tables_get "$t" "$EVIDENCE/tables/$t.pre.json"; done
  local out="$EVIDENCE/preflight/query.json"; fresh "$out"
  local code; code="$(api POST "$BQ/queries" "$out" -H 'Content-Type: application/json' \
    --data-binary @"$BUNDLE_DIR/requests/preflight.query.json")" || die "preflight transport error"
  [[ "$code" == 200 ]] || die "preflight query returned HTTP $code"
  offline --stage preflight
}}

step_dryrun() {{
  [[ "${{1:-}}" == select || "${{1:-}}" == refresh ]] || die "dryrun select|refresh"
  approved; check_window
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
  local d; for d in select refresh; do
    offline --stage dryrun --name "$d" >/dev/null || die "dry run $d not verified"
  done
  local out="$EVIDENCE/jobs/refresh.insert.json"; fresh "$out"; mint_runner_token
  local code; code="$(api POST "$BQ/jobs" "$out" -H 'Content-Type: application/json' \
    --data-binary @"$BUNDLE_DIR/requests/refresh.job.json")" \
    || die "jobs.insert transport error: run 'job'; never resubmit"
  case "$code" in
    200) ;;
    409) die "job $JOB_ID already exists: run 'job'; never resubmit" ;;
    *) die "jobs.insert returned HTTP $code (see ${{out#$BUNDLE_DIR/}})" ;;
  esac
  printf 'Submitted %s. Next: bash m9d.sh job\n' "$JOB_ID"
}}

step_job() {{
  approved; check_window
  local out="$EVIDENCE/jobs/refresh.json"; fresh "$out"; mint_runner_token
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
  offline --stage job >/dev/null || die "scoring job not verified"
  local out="$EVIDENCE/jobs/children.json"; fresh "$out"; mint_runner_token
  local code; code="$(api GET "$BQ/jobs" "$out" -G --data-urlencode "parentJobId=$JOB_ID" \
    --data-urlencode "allUsers=false" --data-urlencode "projection=full" \
    --data-urlencode "maxResults=50")" || die "jobs.list transport error"
  [[ "$code" == 200 ]] || die "jobs.list returned HTTP $code (record it; never widen roles)"
  offline --stage children
}}

step_readback() {{
  approved; check_window
  offline --stage job >/dev/null || die "scoring job not verified"
  local dir="$EVIDENCE/readback/$SCORE"; fresh "$dir"; mkdir "$dir"; mint_runner_token
  tables_get "$SCORE" "$dir/table.before.json"
  local token="" n=1 page code
  while :; do
    (( n <= MAX_PAGES )) || die "page limit $MAX_PAGES exceeded"
    page="$(printf '%s/page-%04d' "$dir" "$n")"
    printf '%s' "$token" > "$page.token"
    if [[ -z "$token" ]]; then
      code="$(api GET "$BQ/datasets/$DATASET/tables/$SCORE/data" "$page.json" -G \
        --data-urlencode "maxResults=$PAGE_SIZE")" || die "tabledata.list transport error"
    else
      code="$(api GET "$BQ/datasets/$DATASET/tables/$SCORE/data" "$page.json" -G \
        --data-urlencode "maxResults=$PAGE_SIZE" --data-urlencode "pageToken=$token")" \
        || die "tabledata.list transport error"
    fi
    [[ "$code" == 200 ]] || die "tabledata.list page $n returned HTTP $code"
    token="$(json_field "$page.json" pageToken)"
    [[ -n "$token" ]] || break
    n=$((n + 1))
  done
  tables_get "$SCORE" "$dir/table.after.json"
  offline --stage readback
}}

step_post() {{
  approved; check_window; mint_runner_token
  local t; for t in $TABLES; do tables_get "$t" "$EVIDENCE/tables/$t.post.json"; done
  offline --stage post
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
  readback) step_readback ;;
  post) step_post ;;
  verify-all) step_verify_all ;;
  *) die "usage: bash m9d.sh <step> [name]; see the header of this file" ;;
esac
"""


# --------------------------------------------------------------------------- CLI
def render_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml render-scoring-bundle")
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--m9c-run", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-expiry", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        result = render_bundle(**vars(args))
    except (ScoringError, BundleError, ms.SpecError, OSError) as error:
        parser.exit(1, f"M9D bundle rejected: {error}\n")
    print(json.dumps(result, indent=1, sort_keys=True))


def verify_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml verify-scoring-evidence")
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--name")
    args = parser.parse_args(argv)
    try:
        report = run_stage(args.bundle_dir, args.packet_dir, args.expected_manifest_sha256,
                           args.stage, args.name)
    except (ScoringError, BundleError, ms.SpecError, OSError) as error:
        print(json.dumps({"ok": False, "problems": [str(error)]}))
        raise SystemExit(1) from None
    print(json.dumps(report, indent=1, sort_keys=True, default=str))
    if not report["ok"]:
        raise SystemExit(1)
