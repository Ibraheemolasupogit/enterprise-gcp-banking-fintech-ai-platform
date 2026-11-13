"""M7C2 operator command bundle for one verified M7B ingestion packet (gap G3).

Renders, offline, a bash script plus exact request bodies that an operator runs **by hand,
one step at a time, in Terminal.app** during an approved M7D window. The assistant never
runs it and tests never execute it. Rendering performs no cloud call.

Everything the commands send is derived from the verified packet: object names, MD5/CRC32C,
sizes, immutable job IDs, schemas and load settings. The only execution-time inputs are the
custody bucket name and the approved window, which are validated here and bound into the
bundle. Because the bundle then contains the bucket name, it must live outside any git
worktree (custody), is written create-exclusive with private permissions, and the renderer
prints only its SHA-256, never the bucket.

Fail-closed design of the generated script:
  * every step re-verifies the bundle against an operator-exported approved SHA-256 and
    refuses to run outside the approved window (with a safety margin before expiry);
  * uploads use the XML API with ``x-goog-if-generation-match: 0`` (create-only) and a
    ``Content-MD5`` header, so Cloud Storage itself rejects any byte difference;
  * load jobs are submitted once with the packet's immutable job ID; a 409 (ID exists) or
    any transport error stops the step, and a retry with a new ID needs a new packet;
  * no step deletes, overwrites, lists as the loader or changes IAM; evidence files are
    never overwritten;
  * each step runs the offline verifier on what it captured before the next step can use it.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from financial_crime_ml.deployment.ingestion_evidence import crc32c_base64, logical_bytes
from financial_crime_ml.deployment.ingestion_packet import (
    LOAD_SETTINGS,
    TABLES,
    canonical_json,
    verify_packet,
)
from financial_crime_ml.evaluation.manifest import sha256_bytes
from financial_crime_ml.ingestion.load_data import REPO_ROOT

BUNDLE_FORMAT = "m7c2-operator-bundle"
BUNDLE_VERSION = "1.0.0"
SCRIPT, RECORD, SUMS, PACKET_SUMS = "m7d.sh", "bundle.json", "SHA256SUMS", "packet.sha256"
REQUESTS, EVIDENCE = "requests", "evidence"
EVIDENCE_DIRS = ("upload", "objects", "objects-post", "jobs", "tables", "readback")
PAGE_SIZE = 1000
SAFETY_MARGIN_SECONDS = 900  # no step starts in the last 15 minutes of the window
MAX_WINDOW = timedelta(hours=8)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# Mirrors the loader_bucket_name validation in the bootstrap root and the plan checker.
BUCKET_RE = re.compile(r"^[a-z][a-z0-9-]{6,61}[a-z0-9]$")
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9/._+@-]+$")  # single-quoted in bash; no quotes/$/`


class BundleError(ValueError):
    """Any bundle rendering or re-verification failure."""


def _utc(value: str, name: str) -> datetime:
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        raise BundleError(f"{name} must be YYYY-MM-DDTHH:MM:SSZ")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_window(start: str, expiry: str) -> dict:
    begin, end = _utc(start, "window start"), _utc(expiry, "window expiry")
    if end <= begin + timedelta(seconds=SAFETY_MARGIN_SECONDS):
        raise BundleError("window expiry must be after the start plus the safety margin")
    if end - begin > MAX_WINDOW:
        raise BundleError("window may not exceed 8 hours")
    return {"start": begin, "expiry": end}


def validate_bucket(bucket: str) -> str:
    if (not isinstance(bucket, str) or not BUCKET_RE.fullmatch(bucket)
            or bucket.startswith("goog") or "google" in bucket or "--" in bucket):
        raise BundleError("bucket name is not a valid approved pilot bucket name")
    return bucket


def loader_email(target: dict) -> str:
    """Derived, never supplied: the bootstrap root names the loader
    ``${prefix}-${environment}-loader`` and the dataset ``${prefix}_${environment}``."""
    prefix = target["dataset_id"].replace("_", "-")
    return f"{prefix}-loader@{target['project_id']}.iam.gserviceaccount.com"


def _inside_git_worktree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in (path, *path.parents))


def _safe_path(path: Path, name: str) -> str:
    text = str(path)
    if not SAFE_PATH_RE.fullmatch(text):
        raise BundleError(f"{name} path has characters that are unsafe in the bundle")
    return text


# --------------------------------------------------------------------------- derivation
def derive(packet_dir: Path, *, expected_manifest_sha256: str, bucket: str, window_start: str,
           window_expiry: str, repo_dir: Path = REPO_ROOT) -> tuple[dict, dict[str, bytes]]:
    """Bundle record and request bodies, as a pure function of the verified packet and the
    approved inputs. Both the renderer and the verifier call it."""
    if not isinstance(expected_manifest_sha256, str) or not SHA256_RE.fullmatch(
            expected_manifest_sha256):
        raise BundleError("an approved manifest SHA-256 (64 hex) is required")
    validate_bucket(bucket)
    validate_window(window_start, window_expiry)
    packet_dir = Path(packet_dir).resolve()
    verified = verify_packet(packet_dir, expected_manifest_sha256=expected_manifest_sha256)
    manifest = verified["manifest"]
    target = manifest["identity"]["target"]
    tables, requests = {}, {}
    for table in TABLES:
        entry, spec = manifest["tables"][table], manifest["load_jobs"][table]
        content = (packet_dir / entry["file"]).read_bytes()
        load = {key: value for key, value in spec["configuration"]["load"].items()
                if key not in ("sourceObject", "sourceUriTemplate")}
        if set(load) != {*LOAD_SETTINGS, "destinationTable", "schema"}:
            raise BundleError(f"{table}: packet load configuration has unexpected keys")
        obj = manifest["objects"]["names"][table]
        load["sourceUris"] = [f"gs://{bucket}/{obj}"]
        body = {"configuration": {"load": load},
                "jobReference": {"jobId": spec["job_id"], "location": spec["location"],
                                 "projectId": target["project_id"]}}
        request = canonical_json(body)
        requests[f"{REQUESTS}/{table}.job.json"] = request
        tables[table] = {
            "crc32c_base64": crc32c_base64(content),
            "encoded_object": quote(obj, safe=""),
            "job_id": spec["job_id"],
            "logical_bytes": logical_bytes(content, load["schema"]["fields"]),
            "max_pages": math.ceil(entry["row_count"] / PAGE_SIZE) + 1,
            "md5_base64": entry["md5_base64"],
            "object": obj,
            "request_file": f"{REQUESTS}/{table}.job.json",
            "request_sha256": sha256_bytes(request),
            "row_count": entry["row_count"],
            "sha256": entry["sha256"],
            "size_bytes": entry["size_bytes"],
        }
    record = {
        "bucket": bucket,
        "bundle_format": BUNDLE_FORMAT,
        "bundle_version": BUNDLE_VERSION,
        "load_order": list(TABLES),
        "loader_email": loader_email(target),
        "manifest_sha256": verified["manifest_sha256"],
        "object_prefix": f"m7/ingestion/{verified['packet_id']}/",
        "packet_dir": _safe_path(packet_dir, "packet"),
        "packet_id": verified["packet_id"],
        "page_size": PAGE_SIZE,
        "repo_dir": _safe_path(Path(repo_dir).resolve(), "repository"),
        "safety_margin_seconds": SAFETY_MARGIN_SECONDS,
        "tables": tables,
        "target": target,
        "window": {"expiry": window_expiry, "start": window_start},
    }
    return record, requests


def loaded_table_expectations(packet_dir: Path, expected_manifest_sha256: str) -> dict:
    """Exact post-load table metadata for the G6 ``pilot-loaded`` plan check: row count and
    BigQuery logical bytes of each input table, derived only from the verified packet."""
    if not isinstance(expected_manifest_sha256, str) or not SHA256_RE.fullmatch(
            expected_manifest_sha256):
        raise BundleError("an approved manifest SHA-256 (64 hex) is required")
    packet_dir = Path(packet_dir).resolve()
    verified = verify_packet(packet_dir, expected_manifest_sha256=expected_manifest_sha256)
    manifest = verified["manifest"]
    tables = {}
    for table in TABLES:
        entry = manifest["tables"][table]
        schema = manifest["load_jobs"][table]["configuration"]["load"]["schema"]["fields"]
        content = (packet_dir / entry["file"]).read_bytes()
        tables[table] = {"num_bytes": logical_bytes(content, schema),
                         "num_rows": entry["row_count"]}
    return {"packet_id": verified["packet_id"], "tables": tables}


def _packet_sums(record: dict) -> bytes:
    lines = [f"{record['manifest_sha256']}  manifest.json"]
    lines += [f"{record['tables'][t]['sha256']}  data/{t}.csv" for t in TABLES]
    return ("\n".join(lines) + "\n").encode("ascii")


def _sums(files: dict[str, bytes]) -> bytes:
    return "".join(f"{sha256_bytes(content)}  {name}\n"
                   for name, content in sorted(files.items())).encode("ascii")


def bundle_files(record: dict, requests: dict[str, bytes]) -> dict[str, bytes]:
    files = {SCRIPT: render_script(record).encode("utf-8"), RECORD: canonical_json(record),
             PACKET_SUMS: _packet_sums(record), **requests}
    files[SUMS] = _sums(files)
    return files


# --------------------------------------------------------------------------- rendering
def render_bundle(packet_dir: Path, *, expected_manifest_sha256: str, bucket: str,
                  window_start: str, window_expiry: str, output_dir: Path,
                  repo_dir: Path = REPO_ROOT) -> dict:
    """Write a new bundle directory (create-exclusive, 0700/0600). Returns a summary that
    never contains the bucket name."""
    output_dir = Path(output_dir).absolute()
    if output_dir.exists() or output_dir.is_symlink():
        raise BundleError("bundle directory already exists; bundles are never overwritten")
    parent = output_dir.parent.resolve()
    if not parent.is_dir():
        raise BundleError("bundle parent directory does not exist")
    output_dir = parent / output_dir.name
    if _inside_git_worktree(output_dir) or output_dir.is_relative_to(Path(repo_dir).resolve()):
        raise BundleError("bundle contains the bucket name: write it to custody, outside any "
                          "git worktree")
    if output_dir.is_relative_to(Path(packet_dir).resolve()):
        raise BundleError("bundle cannot be written inside the packet")
    _safe_path(output_dir, "bundle")
    record, requests = derive(packet_dir, expected_manifest_sha256=expected_manifest_sha256,
                              bucket=bucket, window_start=window_start,
                              window_expiry=window_expiry, repo_dir=repo_dir)
    files = bundle_files(record, requests)
    output_dir.mkdir(mode=0o700)
    os.chmod(output_dir, 0o700)
    for sub in (REQUESTS, EVIDENCE, *(f"{EVIDENCE}/{d}" for d in EVIDENCE_DIRS)):
        (output_dir / sub).mkdir(mode=0o700)
    for name, content in files.items():
        descriptor = os.open(output_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
    return summary(record, files)


def summary(record: dict, files: dict[str, bytes]) -> dict:
    return {
        "bundle_sha256": sha256_bytes(files[SUMS]),
        "loader_email": record["loader_email"],
        "manifest_sha256": record["manifest_sha256"],
        "packet_id": record["packet_id"],
        "tables": {t: {k: record["tables"][t][k] for k in
                       ("job_id", "object", "row_count", "size_bytes", "md5_base64",
                        "crc32c_base64", "request_sha256")} for t in TABLES},
        "window": record["window"],
    }


def load_bundle(bundle_dir: Path, packet_dir: Path, expected_manifest_sha256: str) -> dict:
    """Independently re-derive the whole bundle from the packet and the recorded inputs and
    require every bundle file to be byte-identical. Returns the record."""
    bundle_dir = Path(bundle_dir)
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise BundleError("bundle directory missing or a symbolic link")
    try:
        recorded = json.loads((bundle_dir / RECORD).read_bytes())
        record, requests = derive(
            packet_dir, expected_manifest_sha256=expected_manifest_sha256,
            bucket=recorded["bucket"], window_start=recorded["window"]["start"],
            window_expiry=recorded["window"]["expiry"], repo_dir=Path(recorded["repo_dir"]))
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise BundleError(f"bundle record malformed: {error}") from None
    if Path(recorded.get("packet_dir", "")) != Path(packet_dir).resolve():
        raise BundleError("bundle was rendered for a different packet directory")
    expected = bundle_files(record, requests)
    found = {}
    for path in bundle_dir.rglob("*"):
        relative = path.relative_to(bundle_dir).as_posix()
        if path.is_symlink():
            raise BundleError(f"symbolic link in bundle: {relative}")
        if relative == EVIDENCE or relative.startswith(f"{EVIDENCE}/"):
            continue
        if path.is_file():
            found[relative] = path.read_bytes()
        elif relative != REQUESTS:
            raise BundleError(f"unexpected directory in bundle: {relative}")
    if set(found) != set(expected):
        raise BundleError(f"bundle files differ: missing {sorted(set(expected) - set(found))}, "
                          f"unexpected {sorted(set(found) - set(expected))}")
    for name, content in expected.items():
        if found[name] != content:
            raise BundleError(f"bundle file differs from re-derivation: {name}")
    record["bundle_sha256"] = sha256_bytes(expected[SUMS])
    record["window_parsed"] = validate_window(record["window"]["start"],
                                              record["window"]["expiry"])
    return record


# --------------------------------------------------------------------------- the script
def _epoch(value: str) -> int:
    return int(_utc(value, "window").timestamp())


def _table_case(record: dict) -> str:
    arms = []
    for table, spec in record["tables"].items():
        arms.append(
            f"    {table})\n"
            f"      OBJECT='{spec['object']}'; OBJECT_ENC='{spec['encoded_object']}'\n"
            f"      JOB_ID='{spec['job_id']}'; MD5='{spec['md5_base64']}'\n"
            f"      SIZE='{spec['size_bytes']}'; MAX_PAGES='{spec['max_pages']}' ;;")
    return "\n".join(arms)


def render_script(record: dict) -> str:
    target = record["target"]
    tables = " ".join(record["tables"])
    return SCRIPT_TEMPLATE.format(
        packet_id=record["packet_id"], manifest=record["manifest_sha256"],
        project=target["project_id"], dataset=target["dataset_id"],
        location=target["location"], loader=record["loader_email"], bucket=record["bucket"],
        prefix=record["object_prefix"], packet_dir=record["packet_dir"],
        repo_dir=record["repo_dir"], start=_epoch(record["window"]["start"]),
        expiry=_epoch(record["window"]["expiry"]), start_text=record["window"]["start"],
        expiry_text=record["window"]["expiry"], margin=record["safety_margin_seconds"],
        page_size=record["page_size"], tables=tables, table_case=_table_case(record))


SCRIPT_TEMPLATE = r"""#!/usr/bin/env bash
# M7C2 operator bundle for packet {packet_id}. Generated offline; CONTAINS THE CUSTODY BUCKET
# NAME: keep in custody, never commit, paste or print. Run ONE step at a time, by hand, in
# Terminal.app, only inside the approved M7D window and only after the approvals in
# docs/upgrade/m7d_runbook.md. The assistant never runs this file.
#
#   export M7_APPROVED_BUNDLE_SHA256=<approved bundle SHA-256>
#   bash m7d.sh verify-bundle
#   bash m7d.sh preflight
#   bash m7d.sh upload <table>       (customers, accounts, curated_transactions, in order)
#   bash m7d.sh load <table>
#   bash m7d.sh job <table>          (repeat until DONE)
#   bash m7d.sh post-objects
#   bash m7d.sh readback <table>
#   bash m7d.sh inventory            (operator identity; only if approved)
#   bash m7d.sh verify-all
#   bash m7d.sh describe <table>     (recovery only: after an upload transport error)
#
# No step deletes, overwrites, lists as the loader, changes IAM or retries automatically.
set -euo pipefail
set -o noclobber
umask 077
export LC_ALL=C
unset CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT CLOUDSDK_AUTH_ACCESS_TOKEN_FILE \
  GOOGLE_OAUTH_ACCESS_TOKEN GOOGLE_APPLICATION_CREDENTIALS

BUNDLE_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd -P)"
readonly BUNDLE_DIR
readonly PACKET_ID='{packet_id}'
readonly MANIFEST_SHA256='{manifest}'
readonly PROJECT='{project}'
readonly DATASET='{dataset}'
readonly LOCATION='{location}'
readonly LOADER='{loader}'
readonly BUCKET='{bucket}'
readonly OBJECT_PREFIX='{prefix}'
readonly PACKET_DIR='{packet_dir}'
readonly REPO_DIR='{repo_dir}'
readonly WINDOW_START_EPOCH={start}   # {start_text}
readonly WINDOW_EXPIRY_EPOCH={expiry}  # {expiry_text}
readonly SAFETY_MARGIN={margin}
readonly PAGE_SIZE={page_size}
readonly TABLES='{tables}'
readonly EVIDENCE="$BUNDLE_DIR/evidence"
readonly BQ="https://bigquery.googleapis.com/bigquery/v2/projects/$PROJECT"
readonly GCS_JSON="https://storage.googleapis.com/storage/v1/b/$BUCKET/o"
TOKEN=""

die() {{ printf 'STOP: %s\n' "$*" >&2; exit 1; }}
fresh() {{
  [[ ! -e "$1" ]] || die "evidence already exists, never overwritten: ${{1#$BUNDLE_DIR/}}"
}}

table_values() {{
  case "${{1:-}}" in
{table_case}
    *) die "unknown table '${{1:-}}' (one of: $TABLES)" ;;
  esac
}}

approved() {{
  [[ "${{M7_APPROVED_BUNDLE_SHA256:-}}" =~ ^[0-9a-f]{{64}}$ ]] \
    || die "export M7_APPROVED_BUNDLE_SHA256=<approved bundle SHA-256> first"
  (cd "$BUNDLE_DIR" && shasum -a 256 -c --status SHA256SUMS) \
    || die "bundle files differ from SHA256SUMS"
  local got; got="$(shasum -a 256 < "$BUNDLE_DIR/SHA256SUMS")"; got="${{got%% *}}"
  [[ "$got" == "$M7_APPROVED_BUNDLE_SHA256" ]] || die "bundle SHA-256 is not the approved value"
}}

check_window() {{
  local now; now="$(date -u +%s)"
  (( now >= WINDOW_START_EPOCH )) || die "before the approved window"
  (( now < WINDOW_EXPIRY_EPOCH - SAFETY_MARGIN )) || die "too close to or after the approved expiry"
}}

offline() {{
  (cd "$REPO_DIR" && PYTHONPATH=src python3 -m financial_crime_ml.cli verify-ingestion-evidence \
    --bundle-dir "$BUNDLE_DIR" --packet-dir "$PACKET_DIR" \
    --expected-manifest-sha256 "$MANIFEST_SHA256" "$@")
}}

check_packet() {{
  (cd "$PACKET_DIR" && shasum -a 256 -c --status "$BUNDLE_DIR/packet.sha256") \
    || die "packet bytes differ"
  offline --stage bundle >/dev/null || die "bundle or packet failed offline re-derivation"
}}

mint_loader_token() {{
  TOKEN="$(gcloud auth print-access-token --impersonate-service-account="$LOADER")" \
    || die "loader token could not be minted (window closed or access not granted)"
  [[ "$TOKEN" =~ ^ya29\.[A-Za-z0-9._-]+$ ]] || die "unexpected token shape"
}}

# api METHOD URL OUTFILE [curl options...]  -> prints the HTTP status. The token reaches curl
# through a builtin printf in a process substitution, never through argv.
api() {{
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

describe_object() {{  # describe_object TABLE DIR
  table_values "$1"; local out="$EVIDENCE/$2/$1.json"; fresh "$out"
  local code; code="$(api GET "$GCS_JSON/$OBJECT_ENC" "$out")" || die "describe transport error"
  [[ "$code" == 200 ]] || die "describe $1 returned HTTP $code"
}}

tables_get() {{  # tables_get TABLE OUTFILE
  fresh "$2"
  local code
  code="$(api GET "$BQ/datasets/$DATASET/tables/$1" "$2")" || die "tables.get transport error"
  [[ "$code" == 200 ]] || die "tables.get $1 returned HTTP $code"
}}

step_verify_bundle() {{
  approved; check_packet
  printf 'Bundle verified for packet %s (manifest %s).\n' "$PACKET_ID" "$MANIFEST_SHA256"
}}

step_preflight() {{
  approved; check_window
  local tool; for tool in gcloud curl shasum openssl python3; do
    command -v "$tool" >/dev/null 2>&1 || die "missing tool: $tool"
  done
  check_packet; mint_loader_token
  local t; for t in $TABLES; do tables_get "$t" "$EVIDENCE/tables/$t.preload.json"; done
  offline --stage preload
  echo "Preflight passed: loader impersonation works; three empty tables with the packet schema."
}}

step_upload() {{
  table_values "$1"; approved; check_window; check_packet
  offline --stage preload --table "$1" >/dev/null
  local file="$PACKET_DIR/data/$1.csv" md5 size
  md5="$(openssl dgst -md5 -binary < "$file" | openssl base64 -A)"
  [[ "$md5" == "$MD5" ]] || die "local MD5 differs from the packet"
  size="$(wc -c < "$file" | tr -d ' ')"; [[ "$size" == "$SIZE" ]] || die "local size differs"
  local headers="$EVIDENCE/upload/$1.headers" body="$EVIDENCE/upload/$1.body"
  fresh "$headers"; fresh "$body"; fresh "$EVIDENCE/objects/$1.json"
  mint_loader_token
  local code
  code="$(api PUT "https://storage.googleapis.com/$BUCKET/$OBJECT" "$body" -D "$headers" \
    -H 'Content-Type: text/csv' -H "Content-MD5: $MD5" -H 'x-goog-if-generation-match: 0' \
    --data-binary @"$file")" \
    || die "upload transport error: run 'describe $1', never re-upload blindly"
  case "$code" in
    200) ;;
    412) die "object already exists (create-only precondition): run 'describe $1' and review" ;;
    *) die "upload $1 returned HTTP $code" ;;
  esac
  describe_object "$1" objects
  offline --stage objects --table "$1"
}}

step_describe() {{
  table_values "$1"; approved; check_window; mint_loader_token
  describe_object "$1" objects
  offline --stage objects --table "$1"
}}

step_load() {{
  table_values "$1"; approved; check_window; check_packet
  offline --stage objects --table "$1" >/dev/null || die "object evidence for $1 not verified"
  offline --stage preload --table "$1" >/dev/null || die "table $1 not verified empty"
  local out="$EVIDENCE/jobs/$1.insert.json"; fresh "$out"
  mint_loader_token
  local code
  code="$(api POST "$BQ/jobs" "$out" -H 'Content-Type: application/json' \
    --data-binary @"$BUNDLE_DIR/requests/$1.job.json")" \
    || die "jobs.insert transport error: run 'job $1'; never resubmit"
  case "$code" in
    200) ;;
    409) die "job ID $JOB_ID already exists: run 'job $1'; never resubmit" ;;
    *) die "jobs.insert $1 returned HTTP $code (see ${{out#$BUNDLE_DIR/}})" ;;
  esac
  printf 'Submitted %s. Next: bash m7d.sh job %s\n' "$JOB_ID" "$1"
}}

step_job() {{
  table_values "$1"; approved; check_window
  local out="$EVIDENCE/jobs/$1.json"; fresh "$out"
  mint_loader_token
  local poll; poll="$(mktemp "$EVIDENCE/jobs/.poll.XXXXXX")"
  local code; code="$(api GET "$BQ/jobs/$JOB_ID?location=$LOCATION" "$poll")" \
    || {{ rm -f "$poll"; die "jobs.get transport error"; }}
  [[ "$code" == 200 ]] || {{ rm -f "$poll"; die "jobs.get $1 returned HTTP $code"; }}
  local state; state="$(json_field "$poll" status.state)"
  if [[ "$state" != DONE ]]; then
    rm -f "$poll"; printf 'Job %s is %s; run this step again shortly.\n' "$JOB_ID" "$state"; exit 3
  fi
  mv -n "$poll" "$out"; [[ ! -e "$poll" ]] || die "could not record the job result"
  offline --stage job --table "$1"
}}

step_post_objects() {{
  approved; check_window; mint_loader_token
  local t; for t in $TABLES; do describe_object "$t" objects-post; done
  offline --stage post-objects
}}

step_readback() {{
  table_values "$1"; approved; check_window
  offline --stage job --table "$1" >/dev/null || die "load job for $1 not verified"
  local dir="$EVIDENCE/readback/$1"; fresh "$dir"; mkdir "$dir"
  mint_loader_token
  tables_get "$1" "$dir/table.before.json"
  local token="" n=1 page code
  while :; do
    (( n <= MAX_PAGES )) || die "page limit $MAX_PAGES exceeded"
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
  offline --stage readback --table "$1"
}}

step_inventory() {{
  approved; check_window
  local out="$EVIDENCE/inventory.json"; fresh "$out"
  # The operator's own read-only identity: the loader deliberately cannot list objects.
  TOKEN="$(gcloud auth print-access-token)" || die "operator token unavailable"
  local code; code="$(api GET "$GCS_JSON" "$out" -G --data-urlencode "prefix=$OBJECT_PREFIX" \
    --data-urlencode "versions=true" --data-urlencode "maxResults=100")" \
    || die "list transport error"
  [[ "$code" == 200 ]] || die "objects.list returned HTTP $code"
  offline --stage inventory
}}

step_verify_all() {{
  approved
  local report="$EVIDENCE/report.json"; fresh "$report"
  offline --stage all > "$report" || die "offline verification failed; see ${{report#$BUNDLE_DIR/}}"
  echo "All evidence verified; redacted report at ${{report#$BUNDLE_DIR/}}"
}}

case "${{1:-}}" in
  verify-bundle) step_verify_bundle ;;
  preflight) step_preflight ;;
  upload) step_upload "${{2:-}}" ;;
  describe) step_describe "${{2:-}}" ;;
  load) step_load "${{2:-}}" ;;
  job) step_job "${{2:-}}" ;;
  post-objects) step_post_objects ;;
  readback) step_readback "${{2:-}}" ;;
  inventory) step_inventory ;;
  verify-all) step_verify_all ;;
  *) die "usage: bash m7d.sh <step> [table]; see the header of this file" ;;
esac
"""


# --------------------------------------------------------------------------- CLI
def render_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="financial-crime-ml render-ingestion-bundle")
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--bucket", required=True, help="custody bucket name (never printed)")
    parser.add_argument("--window-start", required=True, help="UTC YYYY-MM-DDTHH:MM:SSZ")
    parser.add_argument("--window-expiry", required=True,
                        help="UTC; the same value as loader_access_expiry")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="new custody directory outside any git worktree")
    args = parser.parse_args(argv)
    try:
        result = render_bundle(args.packet_dir,
                               expected_manifest_sha256=args.expected_manifest_sha256,
                               bucket=args.bucket, window_start=args.window_start,
                               window_expiry=args.window_expiry, output_dir=args.output_dir)
    except (ValueError, OSError, KeyError) as error:
        message = str(error).replace(args.bucket, "<redacted>") if args.bucket else str(error)
        parser.exit(1, f"Bundle rejected: {message}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
