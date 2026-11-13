"""Shared offline fixtures for the M7C2 tests: a small M7B packet and a fake cloud.

The fake cloud replaces ``gcloud`` and ``curl`` on a PATH that contains no real cloud tool,
so the generated operator bundle can be exercised end to end without any network access.
It emulates only the documented behaviour the bundle relies on (create-only XML upload with
Content-MD5, objects.get/list, tables.get, jobs.insert/get, tabledata.list).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from financial_crime_ml.data_generation.v2.contracts import SyntheticConfig
from financial_crime_ml.data_generation.v2.generator import generate_v2
from financial_crime_ml.deployment.ingestion_packet import build_packet
from financial_crime_ml.ingestion.foundation.pipeline import ingest

TARGET = {"project_id": "ebank-pilot-dev-261031-k7m4", "dataset_id": "bankpilot_dev"}
BUCKET = "fixture-pilot-bucket-7q2"
LOADER = "bankpilot-dev-loader@ebank-pilot-dev-261031-k7m4.iam.gserviceaccount.com"
REPO = Path(__file__).resolve().parents[1]
REAL_TOOLS = ("shasum", "openssl", "date", "wc", "tr", "dirname", "mktemp", "mv", "rm",
              "mkdir", "cut", "cat", "env", "perl")
CLOUD_TOOLS = ("gcloud", "curl", "bq", "gsutil", "terraform", "wget")


def build_fixture_packet(base: Path, transactions: int = 100) -> dict:
    frames = generate_v2(SyntheticConfig(customers=4, accounts=6, beneficiaries=12, devices=8,
                                         transactions=transactions))
    inputs = base / "input"
    inputs.mkdir(parents=True)
    for name, frame in frames.items():
        frame.to_csv(inputs / f"{name}.csv", index=False, lineterminator="\n")
    run = ingest(inputs, source_id="m7c2-fixture", run_id="m3", runs_root=base / "runs",
                 source_time="2026-01-01T00:00:00Z", ingestion_time="2026-01-02T00:00:00Z")
    assert run["manifest"]["status"] == "completed"
    return build_packet(run["run_dir"], output_root=base / "packets", **TARGET)


def utc(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def open_window(hours_before: float = 1, hours_after: float = 4) -> tuple[str, str]:
    now = datetime.now(UTC).replace(microsecond=0)
    return utc(now - timedelta(hours=hours_before)), utc(now + timedelta(hours=hours_after))


FAKE_GCLOUD = r'''#!__PYTHON__
import json, os, sys
state = os.environ["FAKE_CLOUD_DIR"]
with open(os.path.join(state, "calls.jsonl"), "a") as log:
    log.write(json.dumps({"tool": "gcloud", "argv": sys.argv[1:]}) + "\n")
knobs = json.load(open(os.path.join(state, "knobs.json")))
args = sys.argv[1:]
if args[:2] != ["auth", "print-access-token"]:
    sys.exit("fake gcloud: unsupported command")
imp = [a for a in args if a.startswith("--impersonate-service-account=")]
if imp:
    if knobs.get("deny_impersonation"):
        sys.exit("ERROR: (gcloud.auth.print-access-token) permission denied")
    print("ya29.loader-" + "L" * 40)
else:
    print("ya29.operator-" + "O" * 40)
'''

FAKE_CURL = r'''#!__PYTHON__
import base64, csv, hashlib, io, json, os, sys, time, urllib.parse
STATE = os.environ["FAKE_CLOUD_DIR"]
sys.path.insert(0, os.environ["FAKE_CLOUD_SRC"])
from financial_crime_ml.deployment.ingestion_evidence import crc32c_base64, logical_bytes

args = sys.argv[1:]
opts = {"H": [], "data": [], "G": False}
url = None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-sS", "--tlsv1.2"):
        i += 1
    elif a == "-G":
        opts["G"] = True; i += 1
    elif a in ("--proto", "--max-time", "-X", "-o", "-w", "-D", "--data-binary"):
        opts[a] = args[i + 1]; i += 2
    elif a == "-H":
        opts["H"].append(args[i + 1]); i += 2
    elif a == "--data-urlencode":
        opts["data"].append(args[i + 1]); i += 2
    elif a.startswith("-"):
        sys.exit(f"fake curl: unexpected option {a}")
    else:
        url = a; i += 1
headers, token = {}, None
for h in opts["H"]:
    if h.startswith("@"):
        text = open(h[1:]).read()
        assert text.startswith("Authorization: Bearer "), "auth header file"
        token = text.split(" ", 2)[2].strip()
    else:
        k, v = h.split(":", 1); headers[k.strip().lower()] = v.strip()
with open(os.path.join(STATE, "calls.jsonl"), "a") as log:
    log.write(json.dumps({"tool": "curl", "argv": args, "method": opts.get("-X"),
                          "url": url, "token_kind": (token or "").split("-")[0]}) + "\n")
assert opts.get("--proto") == "=https" and opts.get("-w") == "%{http_code}"
state = json.load(open(os.path.join(STATE, "state.json")))
knobs = json.load(open(os.path.join(STATE, "knobs.json")))
query = dict(d.split("=", 1) for d in opts["data"])
parsed = urllib.parse.urlsplit(url)
if parsed.query:
    query.update(urllib.parse.parse_qsl(parsed.query))
path = parsed.path
now_ms = int(time.time() * 1000)
def iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + ".%03dZ" % (ms % 1000)
def out(code, body=None, raw=None):
    with open(opts["-o"], "wb") as f:
        f.write((raw if raw is not None else json.dumps(body)).encode())
    json.dump(state, open(os.path.join(STATE, "state.json"), "w"))
    sys.stdout.write(str(code)); sys.exit(0)
if knobs.get("transport_error") and knobs["transport_error"] in url:
    sys.exit(7)
B = state["bucket"]
def obj_meta(name):
    o = state["objects"][name]
    data = base64.b64decode(o["data"])
    md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
    return {"kind": "storage#object", "id": f"{B}/{name}/{o['generation']}",
            "selfLink": f"https://www.googleapis.com/storage/v1/b/{B}/o/x",
            "mediaLink": "https://storage.googleapis.com/download/x", "name": name,
            "bucket": B, "generation": o["generation"], "metageneration": "1",
            "contentType": "text/csv", "storageClass": "STANDARD", "size": str(len(data)),
            "md5Hash": md5, "crc32c": crc32c_base64(data), "etag": "CAE=",
            "timeCreated": iso(o["ms"]), "updated": iso(o["ms"]),
            "timeStorageClassUpdated": iso(o["ms"]), "timeFinalized": iso(o["ms"])}
if parsed.netloc == "storage.googleapis.com" and opts.get("-X") == "PUT":
    name = path[len(f"/{B}/"):]
    assert path.startswith(f"/{B}/")
    data = open(opts["--data-binary"][1:], "rb").read()
    if headers.get("x-goog-if-generation-match") != "0":
        out(400, raw="precondition header required")
    if base64.b64encode(hashlib.md5(data).digest()).decode() != headers.get("content-md5"):
        out(400, raw="BadDigest")
    if name in state["objects"]:
        open(opts["-D"], "w").write("HTTP/1.1 412 Precondition Failed\r\n\r\n")
        out(412, raw="PreconditionFailed")
    if knobs.get("corrupt_upload"):
        data = data[:-2] + b"X\n"
    gen = str(now_ms * 1000)
    state["objects"][name] = {"data": base64.b64encode(data).decode(), "generation": gen,
                              "ms": now_ms}
    md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
    open(opts["-D"], "w").write(
        "HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\n"
        f"x-goog-generation: {gen}\r\nx-goog-metageneration: 1\r\n"
        f"x-goog-hash: crc32c={crc32c_base64(data)}\r\nx-goog-hash: md5={md5}\r\n"
        f"x-goog-stored-content-length: {len(data)}\r\n"
        "x-goog-stored-content-encoding: identity\r\n\r\n")
    out(200, raw="")
if parsed.netloc == "storage.googleapis.com" and path.startswith(f"/storage/v1/b/{B}/o"):
    rest = path[len(f"/storage/v1/b/{B}/o"):]
    if rest == "":
        assert token and token.startswith("ya29.operator"), "list must use the operator"
        names = sorted(n for n in state["objects"] if n.startswith(query["prefix"]))
        items = [obj_meta(n) for n in names] + state.get("extra_versions", [])
        out(200, {"kind": "storage#objects", "items": items})
    name = urllib.parse.unquote(rest[1:])
    if name not in state["objects"]:
        out(404, {"error": {"code": 404}})
    meta = obj_meta(name)
    if knobs.get("metageneration_bump"):
        meta["metageneration"] = "2"
    out(200, meta)
assert parsed.netloc == "bigquery.googleapis.com", url
P, D, L = state["project"], state["dataset"], state["location"]
base = f"/bigquery/v2/projects/{P}"
def table_meta(t):
    tb = state["tables"][t]
    schema = tb["schema"]
    rows = tb["rows"]
    nbytes = 0
    if rows:
        buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n")
        w.writerow([f["name"] for f in schema]); w.writerows(rows)
        nbytes = logical_bytes(buf.getvalue().encode(), schema)
    return {"kind": "bigquery#table", "etag": tb["etag"], "id": f"{P}:{D}.{t}",
            "selfLink": "https://bigquery.googleapis.com/x",
            "tableReference": {"projectId": P, "datasetId": D, "tableId": t},
            "schema": {"fields": [{"name": f["name"], "mode": f["mode"],
                                   "type": "INTEGER" if f["type"] == "INT64" else f["type"]}
                                  for f in schema]},
            "numBytes": str(nbytes), "numLongTermBytes": "0", "numRows": str(len(rows)),
            "creationTime": "1762000000000", "lastModifiedTime": str(tb["modified"]),
            "type": "TABLE", "location": L, "labels": {"environment": "dev"}}
if path.startswith(f"{base}/datasets/{D}/tables/"):
    rest = path[len(f"{base}/datasets/{D}/tables/"):]
    if rest.endswith("/data"):
        t = rest[:-5]
        rows = list(reversed(state["tables"][t]["rows"]))
        if knobs.get("mutate_readback") == t:
            rows[0] = list(rows[0]); rows[0][1] = rows[0][1] + "x"
        size = int(query["maxResults"])
        start = int(query.get("pageToken", "tok-0").split("-")[1])
        assert query.get("formatOptions.useInt64Timestamp") == "true"
        page = {"kind": "bigquery#tableDataList", "etag": "p",
                "totalRows": str(len(rows))}
        cells = []
        schema = state["tables"][t]["schema"]
        for row in rows[start:start + size]:
            values = []
            for value, f in zip(row, schema):
                if f["type"] == "TIMESTAMP":
                    s = time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
                    import calendar
                    value = str(calendar.timegm(s) * 1_000_000)
                values.append({"v": value})
            cells.append({"f": values})
        if cells:
            page["rows"] = cells
        if start + size < len(rows):
            page["pageToken"] = f"tok-{start + size}"
        out(200, page)
    out(200, table_meta(rest))
if path == f"{base}/jobs" and opts.get("-X") == "POST":
    body = json.load(open(opts["--data-binary"][1:]))
    jid = body["jobReference"]["jobId"]
    if jid in state["jobs"]:
        out(409, {"error": {"code": 409, "status": "ALREADY_EXISTS"}})
    load = body["configuration"]["load"]
    t = load["destinationTable"]["tableId"]
    obj = load["sourceUris"][0][len(f"gs://{B}/"):]
    data = base64.b64decode(state["objects"][obj]["data"])
    rows = list(csv.reader(io.StringIO(data.decode(), newline="")))[1:]
    echoed = {k: v for k, v in load.items()
              if not (k in ("allowJaggedRows", "allowQuotedNewlines", "ignoreUnknownValues")
                      and v is False)}
    echoed.update(knobs.get("echo_override", {}))
    job = {"kind": "bigquery#job", "etag": "j", "id": f"{P}:{L}.{jid}",
           "selfLink": "https://bigquery.googleapis.com/x", "user_email": state["loader"],
           "principal_subject": f"serviceAccount:{state['loader']}",
           "configuration": {"load": echoed, "jobType": "LOAD"},
           "jobReference": body["jobReference"],
           "statistics": {"creationTime": str(now_ms), "startTime": str(now_ms),
                          "endTime": str(now_ms + 1000)},
           "status": {"state": "DONE"}}
    if state["tables"][t]["rows"] or knobs.get("fail_load") == t:
        job["status"] = {"state": "DONE", "errorResult": {
            "reason": "duplicate", "message": f"Already Exists: gs://{B}/{obj}"}}
    else:
        state["tables"][t]["rows"] = rows
        state["tables"][t]["etag"] = "loaded"
        state["tables"][t]["modified"] = now_ms + 1000
        job["statistics"]["load"] = {"inputFiles": "1", "inputFileBytes": str(len(data)),
                                     "outputRows": str(len(rows)), "outputBytes": "1",
                                     "badRecords": "0"}
    state["jobs"][jid] = job
    state["polls"][jid] = knobs.get("pending_polls", 0)
    out(200, {**job, "status": {"state": "RUNNING"}})
if path.startswith(f"{base}/jobs/"):
    jid = path[len(f"{base}/jobs/"):]
    assert query.get("location") == L
    if jid not in state["jobs"]:
        out(404, {"error": {"code": 404}})
    if state["polls"].get(jid, 0) > 0:
        state["polls"][jid] -= 1
        out(200, {**state["jobs"][jid], "status": {"state": "RUNNING"}})
    out(200, state["jobs"][jid])
sys.exit(f"fake curl: unhandled {opts.get('-X')} {url}")
'''


class FakeCloud:
    """Sandbox PATH with fake gcloud/curl and the real local tools the bundle needs."""

    def __init__(self, root: Path, packet: dict, knobs: dict | None = None):
        self.root = root
        self.bin = root / "bin"
        self.state_dir = root / "cloud"
        self.bin.mkdir(parents=True)
        self.state_dir.mkdir()
        for tool in REAL_TOOLS:
            found = shutil.which(tool)
            assert found, tool
            (self.bin / tool).symlink_to(found)
        (self.bin / "python3").symlink_to(sys.executable)
        for name, source in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL)):
            path = self.bin / name
            path.write_text(source.replace("__PYTHON__", sys.executable))
            path.chmod(0o700)
        manifest = packet["manifest"]
        schemas = {t: manifest["load_jobs"][t]["configuration"]["load"]["schema"]["fields"]
                   for t in manifest["tables"]}
        state = {"bucket": BUCKET, "project": TARGET["project_id"],
                 "dataset": TARGET["dataset_id"], "location": "europe-west2",
                 "loader": LOADER, "objects": {}, "jobs": {}, "polls": {},
                 "tables": {t: {"schema": s, "rows": [], "etag": "empty",
                                "modified": 1762000000000} for t, s in schemas.items()}}
        (self.state_dir / "state.json").write_text(json.dumps(state))
        self.set_knobs(knobs or {})

    def set_knobs(self, knobs: dict) -> None:
        (self.state_dir / "knobs.json").write_text(json.dumps(knobs))

    def state(self) -> dict:
        return json.loads((self.state_dir / "state.json").read_text())

    def calls(self) -> list[dict]:
        path = self.state_dir / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() \
            else []

    def env(self, approved: str | None) -> dict:
        env = {"PATH": str(self.bin), "HOME": str(self.root), "FAKE_CLOUD_DIR":
               str(self.state_dir), "FAKE_CLOUD_SRC": str(REPO / "src"), "LANG": "C"}
        if approved is not None:
            env["M7_APPROVED_BUNDLE_SHA256"] = approved
        return env

    def run(self, bundle: Path, approved: str | None, *step: str) -> subprocess.CompletedProcess:
        for tool in CLOUD_TOOLS:
            found = shutil.which(tool, path=str(self.bin))
            assert found is None or Path(found).parent == self.bin and tool in ("gcloud",
                                                                                "curl")
        return subprocess.run(["/bin/bash", str(bundle / "m7d.sh"), *step],
                              env=self.env(approved), capture_output=True, text=True,
                              timeout=300, cwd=self.root)


def environment_has_no_cloud_credentials() -> bool:
    return not any(key.startswith(("GOOGLE_", "CLOUDSDK_")) for key in os.environ)
