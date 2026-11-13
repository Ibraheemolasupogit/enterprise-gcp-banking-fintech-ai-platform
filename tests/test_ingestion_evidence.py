"""M7C2 G4: independent offline verifiers for captured Cloud Storage and BigQuery JSON."""

from __future__ import annotations

import base64
import copy
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from financial_crime_ml.deployment.ingestion_evidence import (
    crc32c,
    crc32c_base64,
    logical_bytes,
    parse_response_headers,
    verify_inventory,
    verify_load_job,
    verify_object,
    verify_post_object,
    verify_table,
    verify_upload_headers,
)
from financial_crime_ml.deployment.ingestion_packet import LOAD_SETTINGS

BUCKET = "fixture-pilot-bucket-7q2"
LOADER = "bankpilot-dev-loader@ebank-pilot-dev-261031-k7m4.iam.gserviceaccount.com"
TARGET = {"project_id": "ebank-pilot-dev-261031-k7m4", "dataset_id": "bankpilot_dev",
          "location": "europe-west2"}
CONTENT = b"customer_id,name,score,onboarded_at\nC1,Ann,7,2025-01-01T00:00:00Z\n"
SCHEMA = [{"name": "customer_id", "type": "STRING", "mode": "REQUIRED"},
          {"name": "name", "type": "STRING", "mode": "REQUIRED"},
          {"name": "score", "type": "INT64", "mode": "REQUIRED"},
          {"name": "onboarded_at", "type": "TIMESTAMP", "mode": "REQUIRED"}]
OBJECT = "m7/ingestion/m7p-0123456789abcdef01234567/customers.csv"
JOB_ID = "m7_load_0123456789abcdef01234567_customers"
WINDOW = {"start": datetime(2026, 11, 20, 9, tzinfo=UTC),
          "expiry": datetime(2026, 11, 20, 13, tzinfo=UTC)}
MD5 = base64.b64encode(hashlib.md5(CONTENT).digest()).decode()
EXPECTED = {"table": "customers", "object": OBJECT, "size_bytes": len(CONTENT),
            "md5_base64": MD5, "crc32c_base64": crc32c_base64(CONTENT), "row_count": 1,
            "job_id": JOB_ID, "schema": SCHEMA,
            "sha256": hashlib.sha256(CONTENT).hexdigest()}
GEN = "1763629200000000"
IN_WINDOW = "2026-11-20T10:00:00.123Z"
MS = str(int(datetime(2026, 11, 20, 10, tzinfo=UTC).timestamp() * 1000))


def good_object() -> dict:
    return {"kind": "storage#object", "id": f"{BUCKET}/{OBJECT}/{GEN}", "selfLink": "s",
            "mediaLink": "m", "name": OBJECT, "bucket": BUCKET, "generation": GEN,
            "metageneration": "1", "contentType": "text/csv", "storageClass": "STANDARD",
            "size": str(len(CONTENT)), "md5Hash": MD5, "crc32c": crc32c_base64(CONTENT),
            "etag": "CAE=", "timeCreated": IN_WINDOW, "updated": IN_WINDOW,
            "timeStorageClassUpdated": IN_WINDOW, "timeFinalized": IN_WINDOW}


def good_headers() -> str:
    return ("HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\n"
            f"X-Goog-Generation: {GEN}\r\nx-goog-metageneration: 1\r\n"
            f"x-goog-hash: crc32c={crc32c_base64(CONTENT)}\r\nx-goog-hash: md5={MD5}\r\n"
            f"x-goog-stored-content-length: {len(CONTENT)}\r\n"
            "x-goog-stored-content-encoding: identity\r\n\r\n")


def request_load() -> dict:
    return {**LOAD_SETTINGS,
            "destinationTable": {"datasetId": "bankpilot_dev",
                                 "projectId": TARGET["project_id"], "tableId": "customers"},
            "schema": {"fields": SCHEMA},
            "sourceUris": [f"gs://{BUCKET}/{OBJECT}"]}


def request() -> dict:
    return {"configuration": {"load": request_load()},
            "jobReference": {"jobId": JOB_ID, "location": "europe-west2",
                             "projectId": TARGET["project_id"]}}


def good_job() -> dict:
    return {"kind": "bigquery#job", "etag": "e",
            "id": f"{TARGET['project_id']}:europe-west2.{JOB_ID}", "selfLink": "s",
            "user_email": LOADER, "principal_subject": f"serviceAccount:{LOADER}",
            "configuration": {"load": request_load(), "jobType": "LOAD"},
            "jobReference": request()["jobReference"],
            "statistics": {"creationTime": MS, "startTime": MS, "endTime": str(int(MS) + 5),
                           "load": {"inputFiles": "1", "inputFileBytes": str(len(CONTENT)),
                                    "outputRows": "1", "outputBytes": "40",
                                    "badRecords": "0"}},
            "status": {"state": "DONE"}}


def good_table(rows: int = 1) -> dict:
    return {"kind": "bigquery#table", "etag": "t",
            "id": f"{TARGET['project_id']}:bankpilot_dev.customers", "selfLink": "s",
            "tableReference": {"projectId": TARGET["project_id"], "datasetId": "bankpilot_dev",
                               "tableId": "customers"},
            "schema": {"fields": [{**f, "type": "INTEGER" if f["type"] == "INT64"
                                   else f["type"]} for f in SCHEMA]},
            "numBytes": "40" if rows else "0", "numLongTermBytes": "0", "numRows": str(rows),
            "creationTime": MS, "lastModifiedTime": MS, "type": "TABLE",
            "location": "europe-west2", "labels": {"environment": "dev"}}


def job_check(job, **overrides):
    return verify_load_job(job, EXPECTED, request(), TARGET, LOADER, WINDOW, (BUCKET,))


# ------------------------------------------------------------------ checksums and sizes
def test_crc32c_matches_rfc3720_vectors():
    assert crc32c(b"123456789") == 0xE3069283
    assert crc32c(bytes(32)) == 0x8A9136AA
    assert crc32c(b"\xff" * 32) == 0x62A8AB43
    assert crc32c(bytes(range(32))) == 0x46DD794E
    assert crc32c_base64(b"") == "AAAAAA=="


def test_logical_bytes_model():
    # STRING 2 + len ("C1" 4, "Ann" 5), INT64 8, TIMESTAMP 8
    assert logical_bytes(CONTENT, SCHEMA) == 4 + 5 + 8 + 8
    assert logical_bytes("id\nä\n".encode(), [SCHEMA[0] | {"name": "id"}]) == 2 + 2
    with pytest.raises(ValueError):
        logical_bytes(b"other\nx\n", SCHEMA[:1])


# ------------------------------------------------------------------ objects
def test_good_object_and_headers_verify():
    assert verify_object(good_object(), EXPECTED, BUCKET, WINDOW) == []
    assert verify_upload_headers(good_headers(), EXPECTED, GEN) == []


OBJECT_MUTATIONS = {
    "kind": ("kind", "storage#bucket"),
    "other bucket": ("bucket", "another-bucket-xyz"),
    "other name": ("name", OBJECT.replace("customers", "accounts")),
    "other packet prefix": ("name", OBJECT.replace("0123", "9999")),
    "generation zero": ("generation", "0"),
    "generation number": ("generation", int(GEN)),
    "id mismatch": ("id", f"{BUCKET}/{OBJECT}/1"),
    "metadata rewritten": ("metageneration", "2"),
    "size": ("size", str(len(CONTENT) + 1)),
    "size int": ("size", len(CONTENT)),
    "md5": ("md5Hash", base64.b64encode(b"\0" * 16).decode()),
    "md5 hex instead of base64": ("md5Hash", hashlib.md5(CONTENT).hexdigest()),
    "sha256 offered as md5": ("md5Hash", hashlib.sha256(CONTENT).hexdigest()),
    "crc32c": ("crc32c", "AAAAAA=="),
    "content type": ("contentType", "application/octet-stream"),
    "gzip encoding": ("contentEncoding", "gzip"),
    "composite": ("componentCount", 2),
    "csek": ("customerEncryption", {"encryptionAlgorithm": "AES256"}),
    "kms": ("kmsKeyName", "projects/x/keys/y"),
    "custom metadata sha claim": ("metadata", {"sha256": EXPECTED["sha256"]}),
    "temporary hold": ("temporaryHold", True),
    "event hold": ("eventBasedHold", True),
    "retention": ("retention", {"mode": "Locked"}),
    "soft deleted": ("softDeleteTime", IN_WINDOW),
    "deleted": ("timeDeleted", IN_WINDOW),
    "created before window": ("timeCreated", "2026-11-20T08:00:00Z"),
    "created after window": ("timeCreated", "2026-11-20T14:00:00Z"),
    "created unparsable": ("timeCreated", "yesterday"),
    "unknown attribute": ("futureField", "x"),
    "acl": ("acl", []),
    "cache control": ("cacheControl", "no-store"),
}


@pytest.mark.parametrize("name", sorted(OBJECT_MUTATIONS))
def test_object_mutations_are_rejected(name):
    key, value = OBJECT_MUTATIONS[name]
    meta = good_object()
    meta[key] = value
    assert verify_object(meta, EXPECTED, BUCKET, WINDOW), name


@pytest.mark.parametrize("key", ["kind", "bucket", "name", "generation", "md5Hash", "crc32c",
                                 "size", "contentType", "metageneration", "timeCreated"])
def test_missing_object_attributes_are_rejected(key):
    meta = good_object()
    del meta[key]
    assert verify_object(meta, EXPECTED, BUCKET, WINDOW)


def test_object_verifier_rejects_non_objects():
    for value in (None, [], "x", 1):
        assert verify_object(value, EXPECTED, BUCKET, WINDOW)


HEADER_MUTATIONS = {
    "not 200": lambda h: h.replace("200 OK", "201 Created"),
    "generation": lambda h: h.replace(f"Generation: {GEN}", "Generation: 1"),
    "md5": lambda h: h.replace(f"md5={MD5}", "md5=AAAAAAAAAAAAAAAAAAAAAA=="),
    "crc": lambda h: h.replace("crc32c=", "crc32c=X"),
    "length": lambda h: h.replace(f"length: {len(CONTENT)}", "length: 1"),
    "gzip stored": lambda h: h.replace("encoding: identity", "encoding: gzip"),
    "no status": lambda h: "x-goog-generation: 1\r\n",
    "only continue": lambda h: "HTTP/1.1 100 Continue\r\n\r\n",
    "412": lambda h: "HTTP/1.1 412 Precondition Failed\r\n\r\n",
    "two generations": lambda h: h.replace("x-goog-metageneration: 1",
                                           "x-goog-generation: 2\r\nx-goog-metageneration: 1"),
}


@pytest.mark.parametrize("name", sorted(HEADER_MUTATIONS))
def test_upload_header_mutations_are_rejected(name):
    assert verify_upload_headers(HEADER_MUTATIONS[name](good_headers()), EXPECTED, GEN), name


def test_header_parser_uses_the_final_response():
    status, headers = parse_response_headers(good_headers())
    assert status == 200 and headers["x-goog-generation"] == [GEN]
    assert len(headers["x-goog-hash"]) == 2


def test_post_load_object_must_be_unchanged():
    before = good_object()
    assert verify_post_object(before, copy.deepcopy(before), "customers") == []
    for key, value in (("generation", "2"), ("md5Hash", "x"), ("metageneration", "2"),
                       ("size", "1"), ("crc32c", "y")):
        after = good_object()
        after[key] = value
        assert verify_post_object(before, after, "customers")
    assert verify_post_object(None, before, "customers")


def test_inventory_must_be_exactly_the_three_verified_generations():
    expected = {t: {**EXPECTED, "object": OBJECT.replace("customers", t)}
                for t in ("customers", "accounts", "curated_transactions")}
    gens = {t: str(i + 1) for i, t in enumerate(expected)}
    items = [{"name": expected[t]["object"], "generation": gens[t], "bucket": BUCKET}
             for t in expected]
    good = {"kind": "storage#objects", "items": items}
    assert verify_inventory(good, expected, BUCKET, gens) == []
    extra = copy.deepcopy(good)
    extra["items"].append({"name": OBJECT.replace("customers", "x"), "generation": "9",
                           "bucket": BUCKET})
    noncurrent = copy.deepcopy(good)
    noncurrent["items"].append({**items[0], "generation": "0"})
    duplicate = copy.deepcopy(good)
    duplicate["items"].append(items[0])
    for bad in (extra, noncurrent, duplicate,
                {**good, "nextPageToken": "t"}, {**good, "prefixes": ["a/"]},
                {**good, "kind": "x"}, {"kind": "storage#objects"},
                {**good, "items": items[:2]}, {**good, "other": 1},
                {**good, "items": [{**i, "bucket": "other"} for i in items]}):
        assert verify_inventory(bad, expected, BUCKET, gens)
    assert verify_inventory(good, expected, BUCKET, {**gens, "customers": "8"})


# ------------------------------------------------------------------ tables
def test_good_tables_verify():
    assert verify_table(good_table(0), EXPECTED, TARGET, rows=0) == []
    assert verify_table(good_table(1), EXPECTED, TARGET, rows=1) == []


TABLE_MUTATIONS = {
    "rows": ("numRows", "2"), "rows int": ("numRows", 1), "bytes text": ("numBytes", "x"),
    "long term": ("numLongTermBytes", "5"), "type view": ("type", "VIEW"),
    "location": ("location", "US"), "kind": ("kind", "bigquery#dataset"),
    "id": ("id", "other:bankpilot_dev.customers"),
    "reference": ("tableReference", {"projectId": TARGET["project_id"],
                                     "datasetId": "bankpilot_dev", "tableId": "accounts"}),
    "expiration": ("expirationTime", "1"), "partitioning": ("timePartitioning", {}),
    "clustering": ("clustering", {"fields": ["x"]}), "streaming": ("streamingBuffer", {}),
    "external": ("externalDataConfiguration", {}), "unknown": ("newSetting", 1),
    "cmek": ("encryptionConfiguration", {"kmsKeyName": "k"}),
}


@pytest.mark.parametrize("name", sorted(TABLE_MUTATIONS))
def test_table_mutations_are_rejected(name):
    key, value = TABLE_MUTATIONS[name]
    meta = good_table(1)
    meta[key] = value
    assert verify_table(meta, EXPECTED, TARGET, rows=1), name


@pytest.mark.parametrize("change", ["rename", "type", "mode", "drop", "add", "reorder",
                                    "description", "string-as-int"])
def test_table_schema_changes_are_rejected(change):
    meta = good_table(1)
    fields = meta["schema"]["fields"]
    if change == "rename":
        fields[0]["name"] = "id"
    elif change == "type":
        fields[2]["type"] = "FLOAT"
    elif change == "mode":
        fields[0]["mode"] = "NULLABLE"
    elif change == "drop":
        fields.pop()
    elif change == "add":
        fields.append({"name": "x", "type": "STRING", "mode": "NULLABLE"})
    elif change == "reorder":
        fields.reverse()
    elif change == "description":
        fields[0]["description"] = "x"
    else:
        fields[0]["type"] = "INTEGER"
    assert verify_table(meta, EXPECTED, TARGET, rows=1)


def test_empty_table_must_have_zero_bytes():
    meta = good_table(0)
    meta["numBytes"] = "8"
    assert verify_table(meta, EXPECTED, TARGET, rows=0)


# ------------------------------------------------------------------ load jobs
def test_good_job_verifies_including_omitted_defaults():
    assert job_check(good_job())["problems"] == []
    trimmed = good_job()
    for key in ("allowJaggedRows", "allowQuotedNewlines", "ignoreUnknownValues",
                "schemaUpdateOptions", "maxBadRecords", "autodetect", "quote", "encoding",
                "fieldDelimiter", "sourceFormat"):
        del trimmed["configuration"]["load"][key]
    result = job_check(trimmed)
    assert result["state"] == "done" and result["problems"] == []


def test_pending_job_is_neither_success_nor_failure():
    for state in ("PENDING", "RUNNING", None):
        job = good_job()
        job["status"] = {"state": state} if state else {}
        assert job_check(job)["state"] == "pending"


@pytest.mark.parametrize("key", ["createDisposition", "writeDisposition", "skipLeadingRows",
                                 "destinationTable", "schema", "sourceUris"])
def test_non_default_settings_must_be_echoed(key):
    job = good_job()
    del job["configuration"]["load"][key]
    assert job_check(job)["problems"]


LOAD_MUTATIONS = {
    "write truncate": ("writeDisposition", "WRITE_TRUNCATE"),
    "write append": ("writeDisposition", "WRITE_APPEND"),
    "create if needed": ("createDisposition", "CREATE_IF_NEEDED"),
    "bad records": ("maxBadRecords", 10), "bad records text": ("maxBadRecords", "0"),
    "bad records bool": ("maxBadRecords", False),
    "autodetect": ("autodetect", True), "jagged": ("allowJaggedRows", True),
    "jagged int": ("allowJaggedRows", 0), "newlines": ("allowQuotedNewlines", True),
    "ignore unknown": ("ignoreUnknownValues", True), "skip rows": ("skipLeadingRows", 0),
    "skip text": ("skipLeadingRows", "1"), "skip bool": ("skipLeadingRows", True),
    "schema update": ("schemaUpdateOptions", ["ALLOW_FIELD_ADDITION"]),
    "encoding": ("encoding", "ISO-8859-1"), "delimiter": ("fieldDelimiter", ";"),
    "quote": ("quote", ""), "format": ("sourceFormat", "NEWLINE_DELIMITED_JSON"),
    "two uris": ("sourceUris", [f"gs://{BUCKET}/{OBJECT}", f"gs://{BUCKET}/x.csv"]),
    "wildcard": ("sourceUris", [f"gs://{BUCKET}/m7/ingestion/*"]),
    "other bucket": ("sourceUris", [f"gs://other-bucket-xx/{OBJECT}"]),
    "destination": ("destinationTable", {"datasetId": "bankpilot_dev",
                                         "projectId": TARGET["project_id"],
                                         "tableId": "accounts"}),
    "unrequested setting": ("nullMarker", "NULL"),
    "time partitioning": ("timePartitioning", {"type": "DAY"}),
    "schema extra key": ("schema", {"fields": SCHEMA, "x": 1}),
    "schema reordered": ("schema", {"fields": list(reversed(SCHEMA))}),
}


@pytest.mark.parametrize("name", sorted(LOAD_MUTATIONS))
def test_load_configuration_mutations_are_rejected(name):
    key, value = LOAD_MUTATIONS[name]
    job = good_job()
    job["configuration"]["load"][key] = value
    assert job_check(job)["problems"], name


JOB_MUTATIONS = {
    "other user": lambda j: j.update(user_email="someone@example.com"),
    "runner": lambda j: j.update(user_email=LOADER.replace("loader", "runner")),
    "principal": lambda j: j.update(principal_subject="user:someone@example.com"),
    "job id": lambda j: j["jobReference"].update(jobId="other"),
    "location": lambda j: j["jobReference"].update(location="US"),
    "project": lambda j: j["jobReference"].update(projectId="other-project-1"),
    "id": lambda j: j.update(id="x"),
    "kind": lambda j: j.update(kind="bigquery#query"),
    "query job": lambda j: j.update(configuration={"query": {}, "jobType": "QUERY"}),
    "extra config": lambda j: j["configuration"].update(labels={"a": "b"}),
    "dry run": lambda j: j["configuration"].update(dryRun=True),
    "job type": lambda j: j["configuration"].update(jobType="QUERY"),
    "error result": lambda j: j["status"].update(errorResult={"reason": "invalid",
                                                              "message": "x"}),
    "errors only": lambda j: j["status"].update(errors=[{"reason": "invalid"}]),
    "output rows": lambda j: j["statistics"]["load"].update(outputRows="2"),
    "output rows int": lambda j: j["statistics"]["load"].update(outputRows=1),
    "input files": lambda j: j["statistics"]["load"].update(inputFiles="2"),
    "input bytes": lambda j: j["statistics"]["load"].update(inputFileBytes="1"),
    "bad records": lambda j: j["statistics"]["load"].update(badRecords="1"),
    "load stats missing": lambda j: j["statistics"].pop("load"),
    "load stats extra": lambda j: j["statistics"]["load"].update(foo="1"),
    "query stats": lambda j: j["statistics"].update(query={}),
    "billed": lambda j: j["statistics"].update(totalBytesBilled="10485760"),
    "before window": lambda j: j["statistics"].update(creationTime="1"),
    "after window": lambda j: j["statistics"].update(
        endTime=str(int((WINDOW["expiry"] + timedelta(hours=1)).timestamp() * 1000))),
    "unordered": lambda j: j["statistics"].update(startTime=str(int(MS) + 10)),
    "time missing": lambda j: j["statistics"].pop("endTime"),
    "unknown top": lambda j: j.update(newField=1),
}


@pytest.mark.parametrize("name", sorted(JOB_MUTATIONS))
def test_job_mutations_are_rejected(name):
    job = good_job()
    JOB_MUTATIONS[name](job)
    assert job_check(job)["problems"], name


def test_failed_job_is_classified_and_redacted():
    job = good_job()
    job["status"]["errorResult"] = {"reason": "invalid",
                                    "message": f"Error reading gs://{BUCKET}/{OBJECT}"}
    del job["statistics"]["load"]
    result = job_check(job)
    assert result["state"] == "failed" and result["failure"]["reason"] == "invalid"
    assert BUCKET not in str(result)


def test_job_verifier_rejects_non_objects():
    assert job_check(None)["state"] == "invalid"
    assert job_check([])["problems"]
