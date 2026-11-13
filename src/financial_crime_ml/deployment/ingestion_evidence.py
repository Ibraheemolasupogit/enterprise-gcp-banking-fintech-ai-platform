"""M7C2 offline verifiers for operator-captured Cloud Storage and BigQuery JSON (gap G4).

Pure functions over documents the operator captured in Terminal during M7D. Nothing here
contacts a cloud API, reads credentials or writes files. Every verifier returns a list of
problems; an empty list is the only success. Unknown or unexpected attributes are problems,
so a response shape the code has not been reviewed against fails closed.

Checksum semantics. Cloud Storage reports base64 MD5 and CRC32C, never SHA-256. A matching
MD5/CRC32C/size shows that the stored object has the packet's bytes with high probability,
but it is **not** a SHA-256 proof. SHA-256 equivalence of the loaded data is established
separately, by rebuilding each table byte-for-byte from the BigQuery read-back
(``ingestion_readback``) and hashing it.
"""

from __future__ import annotations

import base64
import csv
import io
import re
from datetime import UTC, datetime, timedelta

DECIMAL_RE = re.compile(r"^(0|[1-9][0-9]{0,18})$")
POSITIVE_RE = re.compile(r"^[1-9][0-9]{0,18}$")
CLOCK_SKEW = timedelta(minutes=5)


class EvidenceError(ValueError):
    """A captured document cannot be verified at all (malformed, missing, unsafe)."""


# --------------------------------------------------------------------------- checksums
def _crc32c_table() -> tuple[int, ...]:
    table = []
    for index in range(256):
        crc = index
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_CRC32C_TABLE = _crc32c_table()


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli), as Cloud Storage computes it. Standard library only."""
    crc = 0xFFFFFFFF
    table = _CRC32C_TABLE
    for byte in data:
        crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def crc32c_base64(data: bytes) -> str:
    """The ``crc32c`` value Cloud Storage reports: base64 of the big-endian 4 bytes."""
    return base64.b64encode(crc32c(data).to_bytes(4, "big")).decode("ascii")


def logical_bytes(content: bytes, schema: list[dict]) -> int:
    """BigQuery logical size of a REQUIRED-only CSV: STRING 2 + UTF-8 bytes, INT64 and
    TIMESTAMP 8 bytes each. A model used for cross-checks; never a substitute for rows."""
    rows = list(csv.reader(io.StringIO(content.decode("utf-8"), newline=""), strict=True))
    if not rows or rows[0] != [field["name"] for field in schema]:
        raise EvidenceError("logical size: header differs from the schema")
    total = 0
    for row in rows[1:]:
        if len(row) != len(schema):
            raise EvidenceError("logical size: field count differs from the schema")
        for value, field in zip(row, schema, strict=True):
            if field["type"] == "STRING":
                total += 2 + len(value.encode("utf-8"))
            elif field["type"] in ("INT64", "TIMESTAMP"):
                total += 8
            else:
                raise EvidenceError(f"logical size: unsupported type {field['type']}")
    return total


# --------------------------------------------------------------------------- helpers
def redact(text: object, secrets: tuple[str, ...]) -> str:
    """Remove the bucket name (and any other custody value) from text before reporting."""
    result = str(text)
    for secret in secrets:
        if secret:
            result = result.replace(secret, "<redacted>")
    return result


def parse_rfc3339(value: object) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})", value):
        return None
    text = value.replace("Z", "+00:00")
    match = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", text)
    fraction = (match.group(2) or "")[:7]  # fromisoformat accepts at most microseconds
    try:
        return datetime.fromisoformat(match.group(1) + fraction + match.group(3)).astimezone(UTC)
    except ValueError:
        return None


def parse_epoch_ms(value: object) -> datetime | None:
    """BigQuery int64 milliseconds, serialised as a decimal string."""
    if not isinstance(value, str) or not POSITIVE_RE.fullmatch(value):
        return None
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(value))


def in_window(moment: datetime | None, window: dict) -> bool:
    return moment is not None and window["start"] - CLOCK_SKEW <= moment <= (
        window["expiry"] + CLOCK_SKEW)


def _unknown(document: dict, allowed: frozenset, label: str) -> list[str]:
    extra = sorted(set(document) - allowed)
    return [f"{label}: unreviewed attribute(s) {extra}"] if extra else []


# --------------------------------------------------------------------------- Cloud Storage
OBJECT_KEYS = frozenset({
    "acl", "bucket", "cacheControl", "componentCount", "contentDisposition", "contentEncoding",
    "contentLanguage", "contentType", "contexts", "crc32c", "customTime", "customerEncryption",
    "etag", "eventBasedHold", "generation", "hardDeleteTime", "id", "kind", "kmsKeyName",
    "md5Hash", "mediaLink", "metadata", "metageneration", "name", "owner", "restoreToken",
    "retention", "retentionExpirationTime", "selfLink", "size", "softDeleteTime",
    "storageClass", "temporaryHold", "timeCreated", "timeDeleted", "timeFinalized",
    "timeStorageClassUpdated", "updated",
})
# Present only if something other than the bundle wrote or changed the object: composite
# objects have no MD5, encodings change the bytes BigQuery reads, holds/retention/deletion
# change lifecycle, custom metadata or headers mean a different writer.
OBJECT_FORBIDDEN = ("acl", "cacheControl", "componentCount", "contentDisposition",
                    "contentEncoding", "contentLanguage", "contexts", "customTime",
                    "customerEncryption", "hardDeleteTime", "kmsKeyName", "metadata", "owner",
                    "restoreToken", "retention", "retentionExpirationTime", "softDeleteTime",
                    "timeDeleted")


def verify_object(meta: object, expected: dict, bucket: str, window: dict) -> list[str]:
    """JSON API ``objects.get`` result for one packet object."""
    label = f"object {expected['table']}"
    if not isinstance(meta, dict):
        return [f"{label}: metadata is not a JSON object"]
    problems = _unknown(meta, OBJECT_KEYS, label)
    if meta.get("kind") != "storage#object":
        problems.append(f"{label}: kind is not storage#object")
    if meta.get("bucket") != bucket:
        problems.append(f"{label}: bucket is not the approved bucket")
    if meta.get("name") != expected["object"]:
        problems.append(f"{label}: name is not the packet object name")
    generation = meta.get("generation")
    if not isinstance(generation, str) or not POSITIVE_RE.fullmatch(generation):
        problems.append(f"{label}: generation is not a positive decimal string")
    elif meta.get("id") != f"{bucket}/{expected['object']}/{generation}":
        problems.append(f"{label}: id does not match bucket/name/generation")
    if meta.get("metageneration") != "1":
        problems.append(f"{label}: metageneration is not 1 (object metadata was changed)")
    if meta.get("size") != str(expected["size_bytes"]):
        problems.append(f"{label}: size differs from the packet")
    if meta.get("md5Hash") != expected["md5_base64"]:
        problems.append(f"{label}: md5Hash differs from the packet MD5")
    if meta.get("crc32c") != expected["crc32c_base64"]:
        problems.append(f"{label}: crc32c differs from the packet CRC32C")
    if meta.get("contentType") != "text/csv":
        problems.append(f"{label}: contentType is not text/csv")
    for key in OBJECT_FORBIDDEN:
        if key in meta:
            problems.append(f"{label}: unexpected attribute {key}")
    for hold in ("temporaryHold", "eventBasedHold"):
        if meta.get(hold) not in (None, False):
            problems.append(f"{label}: {hold} is set")
    created = parse_rfc3339(meta.get("timeCreated"))
    if not in_window(created, window):
        problems.append(f"{label}: timeCreated is outside the approved window")
    updated = parse_rfc3339(meta.get("updated"))
    if updated is None or created is None or updated < created:
        problems.append(f"{label}: updated is not a valid time at or after timeCreated")
    return problems


def parse_response_headers(text: str) -> tuple[int, dict[str, list[str]]]:
    """Last HTTP response block of a ``curl -D`` capture (skips ``100 Continue``)."""
    blocks, current = [], None
    for line in text.replace("\r\n", "\n").split("\n"):
        status = re.fullmatch(r"HTTP/[0-9.]+ ([0-9]{3})(?: .*)?", line)
        if status:
            current = (int(status.group(1)), {})
            blocks.append(current)
        elif current is not None and ":" in line:
            name, value = line.split(":", 1)
            current[1].setdefault(name.strip().lower(), []).append(value.strip())
    final = [block for block in blocks if block[0] != 100]
    if not final:
        raise EvidenceError("upload response has no HTTP status line")
    return final[-1]


def verify_upload_headers(text: str, expected: dict, generation: object) -> list[str]:
    """XML API create-only PUT response: 200, same generation, server-computed hashes."""
    label = f"upload {expected['table']}"
    try:
        status, headers = parse_response_headers(text)
    except EvidenceError as error:
        return [f"{label}: {error}"]
    problems = []
    if status != 200:
        problems.append(f"{label}: HTTP status {status}, not 200")
    if headers.get("x-goog-generation") != [generation]:
        problems.append(f"{label}: x-goog-generation differs from the described generation")
    hashes = {part.strip() for value in headers.get("x-goog-hash", [])
              for part in value.split(",")}
    if f"md5={expected['md5_base64']}" not in hashes:
        problems.append(f"{label}: server md5 differs from the packet MD5")
    if f"crc32c={expected['crc32c_base64']}" not in hashes:
        problems.append(f"{label}: server crc32c differs from the packet CRC32C")
    if headers.get("x-goog-stored-content-length") != [str(expected["size_bytes"])]:
        problems.append(f"{label}: stored length differs from the packet size")
    if headers.get("x-goog-stored-content-encoding", ["identity"]) != ["identity"]:
        problems.append(f"{label}: stored content encoding is not identity")
    return problems


def verify_post_object(before: object, after: object, table: str) -> list[str]:
    """The object a load read must be the same generation and bytes afterwards."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return [f"object {table}: post-load metadata missing"]
    keys = ("bucket", "name", "generation", "metageneration", "size", "md5Hash", "crc32c")
    changed = [key for key in keys if before.get(key) != after.get(key)]
    return [f"object {table}: changed after upload: {changed}"] if changed else []


def verify_inventory(listing: object, expected: dict[str, dict], bucket: str,
                     generations: dict[str, str]) -> list[str]:
    """Operator ``objects.list`` (prefix, all versions): exactly the three packet objects."""
    if not isinstance(listing, dict) or listing.get("kind") != "storage#objects":
        return ["inventory: not a storage#objects listing"]
    problems = _unknown(listing, frozenset({"kind", "items", "prefixes", "nextPageToken"}),
                        "inventory")
    if listing.get("nextPageToken"):
        problems.append("inventory: listing is incomplete (nextPageToken present)")
    if listing.get("prefixes"):
        problems.append("inventory: unexpected sub-prefixes")
    items = listing.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return [*problems, "inventory: items missing"]
    want = {(spec["object"], generations.get(table)) for table, spec in expected.items()}
    got = {(item.get("name"), item.get("generation")) for item in items}
    if len(items) != len(got) or got != want:
        problems.append(f"inventory: {len(items)} object version(s) under the packet prefix; "
                        "expected exactly the three verified generations")
    if any(item.get("bucket") != bucket for item in items):
        problems.append("inventory: object outside the approved bucket")
    return problems


# --------------------------------------------------------------------------- BigQuery
TABLE_KEYS = frozenset({
    "kind", "etag", "id", "selfLink", "tableReference", "schema", "numBytes",
    "numLongTermBytes", "numRows", "creationTime", "lastModifiedTime", "type", "location",
    "labels", "numTotalLogicalBytes", "numActiveLogicalBytes", "numLongTermLogicalBytes",
    "numCurrentPhysicalBytes", "numTotalPhysicalBytes", "numActivePhysicalBytes",
    "numLongTermPhysicalBytes", "numTimeTravelPhysicalBytes", "numPhysicalBytes",
    "description", "friendlyName",
})
TABLE_FORBIDDEN = ("expirationTime", "view", "materializedView", "externalDataConfiguration",
                   "streamingBuffer", "timePartitioning", "rangePartitioning", "clustering",
                   "encryptionConfiguration", "snapshotDefinition", "cloneDefinition",
                   "requirePartitionFilter", "tableConstraints", "defaultCollation",
                   "maxStaleness", "biglakeConfiguration", "tableReplicationInfo")
# tables.get and job configurations may report the legacy SQL name for INT64.
TYPE_ALIASES = {"INT64": {"INT64", "INTEGER"}, "STRING": {"STRING"},
                "TIMESTAMP": {"TIMESTAMP"}}


def schema_problems(fields: object, schema: list[dict], label: str) -> list[str]:
    if not isinstance(fields, list) or len(fields) != len(schema):
        return [f"{label}: schema field count differs from the packet"]
    problems = []
    for got, want in zip(fields, schema, strict=True):
        if not isinstance(got, dict) or set(got) - {"name", "type", "mode"}:
            problems.append(f"{label}: schema field has unreviewed attributes")
            continue
        if (got.get("name") != want["name"] or got.get("mode") != want["mode"]
                or got.get("type") not in TYPE_ALIASES[want["type"]]):
            problems.append(f"{label}: schema field {want['name']} differs from the packet")
    return problems


def verify_table(meta: object, expected: dict, target: dict, *, rows: int) -> list[str]:
    """``tables.get`` result: same table, exact schema, plain TABLE, exact row count."""
    label = f"table {expected['table']}"
    if not isinstance(meta, dict):
        return [f"{label}: metadata is not a JSON object"]
    problems = _unknown(meta, TABLE_KEYS | frozenset(TABLE_FORBIDDEN), label)
    reference = {"projectId": target["project_id"], "datasetId": target["dataset_id"],
                 "tableId": expected["table"]}
    if meta.get("kind") != "bigquery#table" or meta.get("tableReference") != reference:
        problems.append(f"{label}: not the approved table")
    if meta.get("id") != f"{target['project_id']}:{target['dataset_id']}.{expected['table']}":
        problems.append(f"{label}: id differs from the approved table")
    if meta.get("type") != "TABLE":
        problems.append(f"{label}: type is not TABLE")
    if str(meta.get("location", "")).lower() != target["location"]:
        problems.append(f"{label}: location differs from {target['location']}")
    problems += schema_problems((meta.get("schema") or {}).get("fields"), expected["schema"],
                                label)
    if meta.get("numRows") != str(rows):
        problems.append(f"{label}: numRows is not {rows}")
    if not DECIMAL_RE.fullmatch(str(meta.get("numBytes"))):
        problems.append(f"{label}: numBytes is not a decimal string")
    elif rows == 0 and meta.get("numBytes") != "0":
        problems.append(f"{label}: numBytes is not 0 for an empty table")
    if meta.get("numLongTermBytes", "0") != "0":
        problems.append(f"{label}: numLongTermBytes is not 0")
    for key in TABLE_FORBIDDEN:
        if key in meta:
            problems.append(f"{label}: unexpected table setting {key}")
    return problems


JOB_KEYS = frozenset({"kind", "etag", "id", "selfLink", "user_email", "configuration",
                      "jobReference", "statistics", "status", "principal_subject",
                      "jobCreationReason"})
# Load settings whose packet value equals the documented API default may be omitted in the
# returned configuration; every other setting must be echoed exactly.
LOAD_DEFAULTS = {"allowJaggedRows": False, "allowQuotedNewlines": False, "autodetect": False,
                 "encoding": "UTF-8", "fieldDelimiter": ",", "ignoreUnknownValues": False,
                 "maxBadRecords": 0, "quote": '"', "schemaUpdateOptions": [],
                 "sourceFormat": "CSV"}
LOAD_STATISTICS_KEYS = frozenset({"inputFiles", "inputFileBytes", "outputRows", "outputBytes",
                                  "badRecords", "timeline"})


def _same(got: object, want: object) -> bool:
    """Exact JSON equality that does not confuse booleans with integers."""
    if isinstance(want, bool) or isinstance(got, bool):
        return type(got) is type(want) and got == want
    if isinstance(want, int):
        return type(got) is int and got == want
    if isinstance(want, list):
        return isinstance(got, list) and len(got) == len(want) and all(
            _same(g, w) for g, w in zip(got, want, strict=True))
    if isinstance(want, dict):
        return isinstance(got, dict) and set(got) == set(want) and all(
            _same(got[k], want[k]) for k in want)
    return got == want


def load_config_problems(got: object, request: dict, schema: list[dict],
                         label: str) -> list[str]:
    if not isinstance(got, dict):
        return [f"{label}: load configuration missing"]
    problems = []
    for key in sorted(set(got) - set(request)):
        problems.append(f"{label}: load configuration has unrequested setting {key}")
    for key, want in request.items():
        if key == "schema":
            problems += schema_problems((got.get("schema") or {}).get("fields"), schema, label)
            if set(got.get("schema") or {}) != {"fields"}:
                problems.append(f"{label}: load schema has unreviewed attributes")
        elif key not in got:
            if not (key in LOAD_DEFAULTS and _same(want, LOAD_DEFAULTS[key])):
                problems.append(f"{label}: load setting {key} missing from the job")
        elif not _same(got[key], want):
            problems.append(f"{label}: load setting {key} differs from the request")
    return problems


def verify_load_job(job: object, expected: dict, request: dict, target: dict,
                    loader_email: str, window: dict, secrets: tuple[str, ...]) -> dict:
    """``jobs.get`` result for one load job. Returns ``{"state", "problems", ...}``.

    ``state`` is ``pending`` (not DONE yet), ``failed`` (BigQuery reported an error: the
    load is atomic, so the table must still be empty) or ``done``. Only ``done`` with no
    problems is success.
    """
    label = f"job {expected['table']}"
    if not isinstance(job, dict):
        return {"state": "invalid", "problems": [f"{label}: not a JSON object"]}
    status = job.get("status") if isinstance(job.get("status"), dict) else {}
    if status.get("state") != "DONE":
        return {"state": "pending", "problems": [f"{label}: state {status.get('state')!r}"]}
    problems = _unknown(job, JOB_KEYS, label)
    reference = {"projectId": target["project_id"], "jobId": expected["job_id"],
                 "location": target["location"]}
    if job.get("kind") != "bigquery#job" or job.get("jobReference") != reference:
        problems.append(f"{label}: not the approved job ID, project and location")
    if job.get("id") != f"{target['project_id']}:{target['location']}.{expected['job_id']}":
        problems.append(f"{label}: id differs from the approved job")
    if job.get("user_email") != loader_email:
        problems.append(f"{label}: not submitted by the loader service account")
    if "principal_subject" in job and job["principal_subject"] != f"serviceAccount:{loader_email}":
        problems.append(f"{label}: principal_subject is not the loader")
    configuration = job.get("configuration") if isinstance(job.get("configuration"),
                                                           dict) else {}
    if set(configuration) - {"load", "jobType"} or configuration.get("jobType") != "LOAD":
        problems.append(f"{label}: configuration is not a plain LOAD job")
    problems += load_config_problems(configuration.get("load"),
                                     request["configuration"]["load"], expected["schema"],
                                     label)
    failure = None
    if status.get("errorResult") or status.get("errors"):
        error = status.get("errorResult") or (status.get("errors") or [{}])[0]
        failure = {"reason": redact(error.get("reason"), secrets),
                   "message": redact(error.get("message"), secrets)[:500]}
        problems.append(f"{label}: BigQuery reported an error ({failure['reason']})")
    statistics = job.get("statistics") if isinstance(job.get("statistics"), dict) else {}
    times = [parse_epoch_ms(statistics.get(k)) for k in ("creationTime", "startTime",
                                                         "endTime")]
    if not all(in_window(moment, window) for moment in times) or not (
            times[0] <= times[1] <= times[2]):
        problems.append(f"{label}: job times missing, unordered or outside the window")
    if "query" in statistics or statistics.get("totalBytesBilled", "0") != "0":
        problems.append(f"{label}: statistics show query work or billed bytes")
    load = statistics.get("load") if isinstance(statistics.get("load"), dict) else None
    if failure is None:
        if load is None:
            problems.append(f"{label}: load statistics missing")
        else:
            problems += _unknown(load, LOAD_STATISTICS_KEYS, f"{label} statistics")
            for key, want in (("inputFiles", "1"), ("inputFileBytes",
                                                   str(expected["size_bytes"])),
                              ("outputRows", str(expected["row_count"]))):
                if load.get(key) != want:
                    problems.append(f"{label}: statistics {key} is not {want}")
            if load.get("badRecords", "0") != "0":
                problems.append(f"{label}: statistics report bad records")
    return {"state": "failed" if failure else "done", "problems": problems,
            "failure": failure,
            "output_bytes": load.get("outputBytes") if load else None}
