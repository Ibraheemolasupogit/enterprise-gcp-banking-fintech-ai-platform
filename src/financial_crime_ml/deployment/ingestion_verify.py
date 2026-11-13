"""M7C2 offline evidence verification stages for an M7D bundle (gaps G4 and G5).

Called by the operator bundle after each step, and by the operator or reviewer afterwards.
Reads only the verified packet, the re-derived bundle and captured evidence files; never
contacts a cloud API; prints a redacted JSON report (never the bucket name).

Exit codes: 0 verified, 1 problems, 2 cannot verify (malformed or missing input),
3 a load job is not finished yet.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from financial_crime_ml.deployment.ingestion_bundle import EVIDENCE, BundleError, load_bundle
from financial_crime_ml.deployment.ingestion_evidence import (
    EvidenceError,
    redact,
    verify_inventory,
    verify_load_job,
    verify_object,
    verify_post_object,
    verify_table,
    verify_upload_headers,
)
from financial_crime_ml.deployment.ingestion_packet import TABLES, PacketError, verify_packet
from financial_crime_ml.deployment.ingestion_readback import (
    ReadbackError,
    acquisition_stable,
    convert_pages,
    reconcile_table,
)

STAGES = ("bundle", "preload", "objects", "job", "post-objects", "readback", "inventory", "all")
PAGE_RE = re.compile(r"^page-(\d{4})\.(json|token)$")


class _Context:
    def __init__(self, bundle_dir: Path, packet_dir: Path, manifest_sha256: str):
        self.record = load_bundle(bundle_dir, packet_dir, manifest_sha256)
        self.manifest = verify_packet(packet_dir,
                                      expected_manifest_sha256=manifest_sha256)["manifest"]
        self.packet_dir = Path(packet_dir).resolve()
        self.evidence = Path(bundle_dir) / EVIDENCE
        self.bucket = self.record["bucket"]
        self.window = self.record["window_parsed"]
        self.target = self.record["target"]

    def expected(self, table: str) -> dict:
        spec = self.record["tables"][table]
        load = self.manifest["load_jobs"][table]["configuration"]["load"]
        return {**spec, "table": table, "schema": load["schema"]["fields"]}

    def request(self, table: str) -> dict:
        return json.loads(self.record_bytes(self.record["tables"][table]["request_file"]))

    def record_bytes(self, name: str) -> bytes:
        return (self.evidence.parent / name).read_bytes()

    def read_json(self, relative: str) -> object:
        path = self.evidence / relative
        if path.is_symlink() or not path.is_file():
            raise EvidenceError(f"evidence missing: {relative}")
        try:
            return json.loads(path.read_bytes())
        except json.JSONDecodeError as error:
            raise EvidenceError(f"evidence is not JSON: {relative} ({error.msg})") from None


def _preload(ctx: _Context, table: str) -> dict:
    meta = ctx.read_json(f"tables/{table}.preload.json")
    return {"problems": verify_table(meta, ctx.expected(table), ctx.target, rows=0)}


def _objects(ctx: _Context, table: str) -> dict:
    expected = ctx.expected(table)
    meta = ctx.read_json(f"objects/{table}.json")
    problems = verify_object(meta, expected, ctx.bucket, ctx.window)
    headers = ctx.evidence / f"upload/{table}.headers"
    if headers.is_symlink() or not headers.is_file():
        problems.append(f"upload {table}: response headers missing")
    else:
        problems += verify_upload_headers(headers.read_text("latin-1"), expected,
                                          meta.get("generation") if isinstance(meta, dict)
                                          else None)
    return {"problems": problems,
            "generation": meta.get("generation") if isinstance(meta, dict) else None}


def _job(ctx: _Context, table: str) -> dict:
    expected = ctx.expected(table)
    secrets = (ctx.bucket,)
    inserted = ctx.read_json(f"jobs/{table}.insert.json")
    problems = []
    reference = {"projectId": ctx.target["project_id"], "jobId": expected["job_id"],
                 "location": ctx.target["location"]}
    if not isinstance(inserted, dict) or inserted.get("jobReference") != reference or \
            inserted.get("user_email") != ctx.record["loader_email"]:
        problems.append(f"job {table}: insert response is not the approved job by the loader")
    result = verify_load_job(ctx.read_json(f"jobs/{table}.json"), expected,
                             ctx.request(table), ctx.target, ctx.record["loader_email"],
                             ctx.window, secrets)
    if result["state"] == "pending":
        return {"state": "pending", "problems": problems}
    return {"state": result["state"], "problems": problems + result["problems"],
            "failure": result.get("failure"), "output_bytes": result.get("output_bytes")}


def _post_objects(ctx: _Context, table: str) -> dict:
    before = ctx.read_json(f"objects/{table}.json")
    after = ctx.read_json(f"objects-post/{table}.json")
    problems = verify_object(after, ctx.expected(table), ctx.bucket, ctx.window)
    return {"problems": problems + verify_post_object(before, after, table)}


def _readback(ctx: _Context, table: str) -> dict:
    expected = ctx.expected(table)
    directory = ctx.evidence / "readback" / table
    if directory.is_symlink() or not directory.is_dir():
        raise EvidenceError(f"evidence missing: readback/{table}")
    names = sorted(path.name for path in directory.iterdir())
    pages = sorted({int(m.group(1)) for m in map(PAGE_RE.fullmatch, names) if m})
    allowed = {"table.before.json", "table.after.json",
               *(f"page-{n:04d}.{ext}" for n in pages for ext in ("json", "token"))}
    if set(names) != allowed or pages != list(range(1, len(pages) + 1)):
        raise EvidenceError(f"readback/{table}: unexpected, missing or non-contiguous files")
    if len(pages) > expected["max_pages"]:
        raise EvidenceError(f"readback/{table}: more pages than the bundle allows")
    before = ctx.read_json(f"readback/{table}/table.before.json")
    after = ctx.read_json(f"readback/{table}/table.after.json")
    rows = expected["row_count"]
    problems = verify_table(before, expected, ctx.target, rows=rows)
    problems += verify_table(after, expected, ctx.target, rows=rows)
    if isinstance(before, dict) and isinstance(after, dict):
        problems += [f"readback {table}: {p}" for p in acquisition_stable(before, after)]
    captured = [((directory / f"page-{n:04d}.token").read_text("ascii"),
                 ctx.read_json(f"readback/{table}/page-{n:04d}.json")) for n in pages]
    content = (ctx.packet_dir / "data" / f"{table}.csv").read_bytes()
    try:
        converted = convert_pages(captured, expected["schema"], rows)
        result = reconcile_table(table, converted, content, ctx.manifest)
    except ReadbackError as error:
        return {"problems": [*problems, f"readback {table}: {error}"]}
    num_bytes = after.get("numBytes") if isinstance(after, dict) else None
    result["num_bytes"] = num_bytes
    result["num_bytes_matches_logical_model"] = num_bytes == str(expected["logical_bytes"])
    result["pages"] = len(pages)
    return {"problems": problems, **result}


def _inventory(ctx: _Context) -> dict:
    listing = ctx.read_json("inventory.json")
    generations = {}
    for table in TABLES:
        meta = ctx.read_json(f"objects/{table}.json")
        generations[table] = meta.get("generation") if isinstance(meta, dict) else None
    expected = {table: ctx.expected(table) for table in TABLES}
    return {"problems": verify_inventory(listing, expected, ctx.bucket, generations)}


def _layout(ctx: _Context) -> list[str]:
    allowed = {"inventory.json", "report.json"}
    for table in TABLES:
        allowed |= {f"upload/{table}.headers", f"upload/{table}.body", f"objects/{table}.json",
                    f"objects-post/{table}.json", f"jobs/{table}.insert.json",
                    f"jobs/{table}.json", f"tables/{table}.preload.json"}
    problems = []
    for path in ctx.evidence.rglob("*"):
        relative = path.relative_to(ctx.evidence).as_posix()
        if path.is_symlink():
            problems.append(f"evidence: symbolic link {relative}")
        elif path.is_file() and relative not in allowed and not re.fullmatch(
                r"readback/[a-z_]+/[^/]+", relative):
            problems.append(f"evidence: unexpected file {relative}")
    return problems


def run_stage(bundle_dir: Path, packet_dir: Path, expected_manifest_sha256: str, stage: str,
              table: str | None = None) -> dict:
    if stage not in STAGES:
        raise EvidenceError(f"unknown stage {stage}")
    if table is not None and table not in TABLES:
        raise EvidenceError(f"unknown table {table}")
    ctx = _Context(bundle_dir, packet_dir, expected_manifest_sha256)
    report = {"stage": stage, "packet_id": ctx.record["packet_id"],
              "manifest_sha256": ctx.record["manifest_sha256"],
              "bundle_sha256": ctx.record["bundle_sha256"], "tables": {}, "problems": []}
    tables = [table] if table else list(TABLES)
    per_table = {"preload": _preload, "objects": _objects, "job": _job,
                 "post-objects": _post_objects, "readback": _readback}
    stages = [stage] if stage in per_table else []
    if stage == "all":
        stages = list(per_table)
    pending = False
    for name in stages:
        for item in tables:
            result = per_table[name](ctx, item)
            report["problems"] += result.pop("problems")
            pending |= result.get("state") == "pending"
            report["tables"].setdefault(item, {})[name] = result
    if stage == "inventory":
        report["problems"] += _inventory(ctx)["problems"]
    if stage == "all":
        report["problems"] += _layout(ctx)
        if (ctx.evidence / "inventory.json").exists():
            report["problems"] += _inventory(ctx)["problems"]
            report["inventory"] = "verified"
        else:
            report["inventory"] = "not captured (prefix listing needs separate approval)"
        report["ledger"] = {
            "load_jobs": len(TABLES), "query_jobs": 0, "query_bytes_billed": 0,
            "upload_bytes": sum(ctx.record["tables"][t]["size_bytes"] for t in TABLES)}
        report["checksum_scope"] = (
            "Cloud Storage objects matched packet MD5, CRC32C and size (transfer checks, not "
            "SHA-256). SHA-256 equivalence comes from the byte-identical rebuild of each "
            "loaded table from the BigQuery read-back.")
    report["problems"] = [redact(p, (ctx.bucket,)) for p in report["problems"]]
    report["pending"] = pending
    report["ok"] = not report["problems"] and not pending
    return report


def verify_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml verify-ingestion-evidence")
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--table", choices=tuple(TABLES))
    args = parser.parse_args(argv)
    bucket = None
    try:
        report = run_stage(args.bundle_dir, args.packet_dir, args.expected_manifest_sha256,
                           args.stage, args.table)
    except (EvidenceError, BundleError, PacketError, OSError, KeyError, TypeError,
            ValueError) as error:
        try:
            bucket = json.loads((args.bundle_dir / "bundle.json").read_bytes()).get("bucket")
        except (OSError, ValueError, AttributeError):
            bucket = None
        print(json.dumps({"ok": False, "stage": args.stage,
                          "problems": [redact(f"cannot verify: {error}", (bucket or "",))]},
                         indent=2))
        sys.exit(2)
    print(json.dumps(report, indent=2, sort_keys=True))
    sys.exit(0 if report["ok"] else 3 if report["pending"] and not report["problems"] else 1)
