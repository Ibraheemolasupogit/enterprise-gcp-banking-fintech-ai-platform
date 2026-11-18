"""M8: offline tests for the mart bundle, reference derivation and evidence verifier.

Evidence is synthesised here from the expected marts in the exact shapes the BigQuery REST
API returns; nothing calls a cloud API. Every negative case must be rejected.
"""

from __future__ import annotations

import copy
import io
import json
import subprocess
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import pytest

from financial_crime_ml.analytics.contracts import COLUMNS, GRAINS, bigquery_schemas
from financial_crime_ml.cli import main as cli_main
from financial_crime_ml.deployment import marts_bundle as mb
from financial_crime_ml.deployment.ingestion_readback import ReadbackError, convert_value

ROOT = Path(__file__).resolve().parents[1]
PACKET = ROOT / "outputs/packets/m7p-a89332976c69d583137f9323"
MANIFEST = "7e2fb8b7500a4aaf0652d2f1381359d333011a5f2ff5f92b1b684be0142b540f"
START, EXPIRY = "2026-11-20T10:00:00Z", "2026-11-20T13:00:00Z"
IN_WINDOW_MS = str(int(datetime(2026, 11, 20, 11, tzinfo=UTC).timestamp() * 1000))
EXPECTED_ROWS = {"transaction_daily": 1312, "customer_daily": 3412, "account_daily": 3428,
                 "operations_daily": 3762}

pytestmark = pytest.mark.skipif(not PACKET.is_dir(), reason="M7B sample packet not available")


@pytest.fixture(scope="module")
def expected():
    return mb.expected_marts(PACKET, MANIFEST)


# --------------------------------------------------------------------------- reference
def test_expected_mart_row_counts_and_currency_reconciliation(expected):
    assert {m: s["row_count"] for m, s in expected["marts"].items()} == EXPECTED_ROWS
    packet = json.loads((PACKET / "manifest.json").read_text())["reconciliation"]
    for mart, spec in expected["marts"].items():
        totals = mb.currency_totals(spec["rows"], COLUMNS[mart])
        assert totals == {c: {"amount_minor": v["amount_minor"], "row_count": v["row_count"]}
                          for c, v in packet["amount_minor_by_currency"].items()}
        assert sum(t["row_count"] for t in totals.values()) == 4000
        assert mb.financial_problems(mart, spec["rows"], expected) == []


def test_only_the_date_function_and_identifiers_are_translated():
    select = ("SELECT DATE(t.transaction_timestamp, 'UTC') AS event_date, SUM(t.amount_minor)"
              " FROM `PROJECT.DATASET.curated_transactions` t")
    assert mb.sqlite_translation(select) == (
        "SELECT substr(t.transaction_timestamp, 1, 10) AS event_date, SUM(t.amount_minor)"
        " FROM curated_transactions t")


def _script() -> str:
    return mb.SCRIPT_SQL_PATH.read_text()


SCRIPT_BAD = {
    "missing delete": lambda s: s.replace(
        "DELETE FROM `PROJECT.DATASET.operations_daily` WHERE TRUE;\n", ""),
    "partial delete": lambda s: s.replace(
        "`PROJECT.DATASET.transaction_daily` WHERE TRUE", "`PROJECT.DATASET.transaction_daily` "
        "WHERE currency = 'GBP'"),
    "no transaction": lambda s: s.replace("BEGIN TRANSACTION;\n", ""),
    "no commit": lambda s: s.replace("COMMIT TRANSACTION;", "ROLLBACK TRANSACTION;"),
    "column order swapped": lambda s: s.replace(
        "(event_date, currency, transaction_count", "(currency, event_date, transaction_count"),
    "insert order swapped": lambda s: s.replace(
        "INSERT INTO `PROJECT.DATASET.transaction_daily`",
        "INSERT INTO `PROJECT.DATASET.TMP`").replace(
        "INSERT INTO `PROJECT.DATASET.customer_daily`",
        "INSERT INTO `PROJECT.DATASET.transaction_daily`"),
    "reads a mart": lambda s: s.replace(
        "FROM `PROJECT.DATASET.curated_transactions`\nGROUP BY event_date, currency, "
        "transaction_type", "FROM `PROJECT.DATASET.transaction_daily`\nGROUP BY event_date, "
        "currency, transaction_type"),
    "float money": lambda s: s.replace("SUM(amount_minor) AS total_amount_minor,\n       "
                                       "COUNT(DISTINCT customer_id)",
                                       "CAST(SUM(amount_minor) AS FLOAT64) AS "
                                       "total_amount_minor,\n       COUNT(DISTINCT customer_id)"),
    "extra statement": lambda s: s.replace("COMMIT TRANSACTION;",
                                           "DROP TABLE `PROJECT.DATASET.customers`;\n"
                                           "COMMIT TRANSACTION;"),
    "non-deterministic": lambda s: s.replace("COUNT(*) AS transaction_count, SUM(amount_minor)",
                                             "COUNT(*) AS transaction_count, SUM(amount_minor) "
                                             "+ CAST(RAND() AS INT64)"),
}


@pytest.mark.parametrize("name", SCRIPT_BAD)
def test_script_structure_and_forbidden_sql_are_rejected(name):
    mutated = SCRIPT_BAD[name](_script())
    assert mutated != _script(), name
    with pytest.raises(mb.MartError):
        mb.parse_script(mutated)


SEMANTIC_BAD = {
    "distinct dropped": ("COUNT(DISTINCT customer_id) AS active_customer_count",
                         "COUNT(customer_id) AS active_customer_count"),
    "money minus one": ("SELECT DATE(transaction_timestamp, 'UTC') AS event_date, currency,\n"
                   "       COUNT(*) AS transaction_count, SUM(amount_minor) AS total_amount_minor"
                   ",\n       COUNT(DISTINCT customer_id)",
                   "SELECT DATE(transaction_timestamp, 'UTC') AS event_date, currency,\n"
                   "       COUNT(*) AS transaction_count, SUM(amount_minor) - 1 AS "
                   "total_amount_minor,\n       COUNT(DISTINCT customer_id)"),
    "wrong join key": ("ON t.account_id = a.account_id", "ON t.customer_id = a.customer_id"),
    "min for max": ("MAX(transaction_timestamp) AS last_event_time\nFROM "
                    "`PROJECT.DATASET.curated_transactions` GROUP BY",
                    "MIN(transaction_timestamp) AS last_event_time\nFROM "
                    "`PROJECT.DATASET.curated_transactions` GROUP BY"),
}


@pytest.mark.parametrize("name", SEMANTIC_BAD)
def test_semantic_changes_to_the_bigquery_sql_fail_the_m4_reference(name, tmp_path,
                                                                      monkeypatch):
    old, new = SEMANTIC_BAD[name]
    assert _script().count(old) == 1, name
    path = tmp_path / "script.sql"
    path.write_text(_script().replace(old, new))
    monkeypatch.setattr(mb, "SCRIPT_SQL_PATH", path)
    with pytest.raises(mb.MartError, match="differs from the M4 reference|reconcile"):
        mb.expected_marts(PACKET, MANIFEST)


def test_render_substitutes_only_approved_identifiers():
    script = mb.rendered_script("ebank-pilot-dev-261031-k7m4", "bankpilot_dev")
    assert "PROJECT" not in script and "DATASET" not in script and "--" not in script
    assert script.count("`ebank-pilot-dev-261031-k7m4.bankpilot_dev.") == 14
    for bad in (("Bad Project", "bankpilot_dev"), ("ebank-pilot-dev-261031-k7m4", "x;DROP"),
                ("ebank-pilot-dev-261031-k7m4", "a`b")):
        with pytest.raises(mb.MartError):
            mb.render(_script(), *bad)


def test_manifest_hash_is_required(tmp_path):
    with pytest.raises(mb.MartError):
        mb.expected_marts(PACKET, "0" * 63)
    with pytest.raises(ValueError):
        mb.expected_marts(PACKET, "0" * 64)


def test_cost_envelope_fits_the_budget_and_job_limit(expected):
    cost = mb.cost_envelope(expected, 104857600)
    assert cost["conservative_bytes_billed"] == 140 * 1024**2
    assert cost["upper_bound_bytes_billed"] == 800 * 1024**2
    assert cost["upper_bound_usd"] < 0.01  # vs the £5 alert budget (not a cap)
    record, _ = mb.derive(PACKET, expected_manifest_sha256=MANIFEST, window_start=START,
                          window_expiry=EXPIRY)
    assert record["job_reservation"] == {"dry_runs": 5, "script_parent": 1,
                                         "script_children": 10, "total": 16}


# --------------------------------------------------------------------------- bundle
@pytest.fixture
def bundle(tmp_path):
    out = tmp_path / "m8"
    summary = mb.render_bundle(PACKET, expected_manifest_sha256=MANIFEST, window_start=START,
                               window_expiry=EXPIRY, output_dir=out, repo_dir=ROOT)
    return out, summary


def test_bundle_is_deterministic_and_reverifies(bundle, tmp_path):
    out, summary = bundle
    again = mb.render_bundle(PACKET, expected_manifest_sha256=MANIFEST, window_start=START,
                             window_expiry=EXPIRY, output_dir=tmp_path / "again", repo_dir=ROOT)
    assert again["bundle_sha256"] == summary["bundle_sha256"]
    assert summary["runner_email"] == (
        "bankpilot-dev-runner@ebank-pilot-dev-261031-k7m4.iam.gserviceaccount.com")
    assert summary["script_job_id"].startswith("m8_marts_a89332976c69d583137f9323_")
    assert summary["script_job_id"].endswith("_a1")
    assert mb.run_stage(out, PACKET, MANIFEST, "bundle")["ok"]
    request = json.loads((out / "requests/script.job.json").read_text())
    assert request["configuration"]["query"]["maximumBytesBilled"] == "104857600"
    assert "dryRun" not in json.dumps(request)
    for name in mb.DRY_RUNS:
        dry = json.loads((out / f"requests/dryrun-{name}.json").read_text())
        assert dry["configuration"]["dryRun"] is True and "jobId" not in dry["jobReference"]
    with pytest.raises(mb.MartError, match="already exists"):
        mb.render_bundle(PACKET, expected_manifest_sha256=MANIFEST, window_start=START,
                         window_expiry=EXPIRY, output_dir=out, repo_dir=ROOT)


def test_new_attempt_gets_a_new_job_id(tmp_path):
    a = mb.derive(PACKET, expected_manifest_sha256=MANIFEST, window_start=START,
                  window_expiry=EXPIRY)[0]["script_job_id"]
    b = mb.derive(PACKET, expected_manifest_sha256=MANIFEST, window_start=START,
                  window_expiry=EXPIRY, attempt=2)[0]["script_job_id"]
    assert a != b and a[:-1] == b[:-1]
    with pytest.raises(mb.MartError):
        mb.derive(PACKET, expected_manifest_sha256=MANIFEST, window_start=START,
                  window_expiry=EXPIRY, attempt=0)


def test_bundle_refuses_git_worktree_and_long_windows(tmp_path):
    with pytest.raises(mb.MartError, match="git worktree"):
        mb.render_bundle(PACKET, expected_manifest_sha256=MANIFEST, window_start=START,
                         window_expiry=EXPIRY, output_dir=ROOT / "outputs/m8-x", repo_dir=ROOT)
    with pytest.raises(ValueError, match="8 hours"):
        mb.render_bundle(PACKET, expected_manifest_sha256=MANIFEST, window_start=START,
                         window_expiry="2026-11-20T19:00:00Z", output_dir=tmp_path / "w",
                         repo_dir=ROOT)


@pytest.mark.parametrize("tamper", ["sql", "request", "extra", "script"])
def test_tampered_bundle_is_rejected(bundle, tamper):
    out, _ = bundle
    if tamper == "sql":
        (out / "sql/marts.sql").write_text("SELECT 1;\n")
    elif tamper == "request":
        p = out / "requests/script.job.json"
        p.write_text(p.read_text().replace("104857600", "1048576000"))
    elif tamper == "extra":
        (out / "requests/extra.json").write_text("{}")
    else:
        p = out / "m8.sh"
        p.write_text(p.read_text() + "\ngcloud projects add-iam-policy-binding x\n")
    with pytest.raises(mb.MartError):
        mb.run_stage(out, PACKET, MANIFEST, "bundle")


def test_operator_script_is_syntactically_valid_and_least_privilege(bundle):
    out, _ = bundle
    script = (out / "m8.sh").read_text()
    subprocess.run(["bash", "-n", str(out / "m8.sh")], check=True)
    assert script.count("print-access-token") == 1
    assert '--impersonate-service-account="$RUNNER"' in script
    for forbidden in ("add-iam-policy-binding", "set-iam-policy", "setIamPolicy",
                      "-X DELETE", "tables.delete", "keys create", "maximumBytesBilled=",
                      "bq rm", "Token Creator"):
        assert forbidden not in script
    assert "evidence already exists, never overwritten" in script


# --------------------------------------------------------------------------- evidence
def _table_meta(table: str, fields: list[dict], rows: int, size: int, modified="1") -> dict:
    target = {"projectId": "ebank-pilot-dev-261031-k7m4", "datasetId": "bankpilot_dev",
              "tableId": table}
    return {"kind": "bigquery#table", "id": f"ebank-pilot-dev-261031-k7m4:bankpilot_dev.{table}",
            "tableReference": target, "type": "TABLE", "location": "europe-west2",
            "schema": {"fields": [{**f, "type": "INTEGER" if f["type"] == "INT64" else f["type"]}
                                  for f in fields]},
            "numRows": str(rows), "numBytes": str(size), "numLongTermBytes": "0",
            "lastModifiedTime": modified}


def _micros(ts: str) -> str:
    return str(int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()) * 1_000_000)


def _pages(rows: list[list[str]], fields: list[dict]) -> list[tuple[str, dict]]:
    cells = [{"f": [{"v": _micros(v) if f["type"] == "TIMESTAMP" else v}
                    for v, f in zip(r, fields, strict=True)]} for r in rows]
    chunks = [cells[i:i + 1000] for i in range(0, len(cells), 1000)] or [[]]
    pages, token = [], ""
    for n, chunk in enumerate(chunks):
        nxt = f"tok{n + 1}" if n < len(chunks) - 1 else None
        page = {"kind": "bigquery#tableDataList", "etag": "e", "totalRows": str(len(rows)),
                "rows": chunk, **({"pageToken": nxt} if nxt else {})}
        pages.append((token, page))
        token = nxt or ""
    return pages


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value))


def write_evidence(out: Path, expected: dict, *, mart_pre_rows: int | None = 0) -> None:
    record = json.loads((out / "bundle.json").read_text())
    ev, marts = out / "evidence", bigquery_schemas()["tables"]
    source = json.loads((ROOT / "infrastructure/schemas/v1/source_tables.json").read_text())
    for t in mb.INPUTS:
        meta = _table_meta(t, source[t], record["inputs"][t]["num_rows"],
                           record["inputs"][t]["num_bytes"], "1794910873011")
        _write(ev / "tables" / f"{t}.pre.json", meta)
        _write(ev / "tables" / f"{t}.post.json", meta)
    for m in mb.MARTS:
        rows = expected["marts"][m]["row_count"] if mart_pre_rows is None else mart_pre_rows
        size = expected["marts"][m]["logical_bytes"] if rows else 0
        _write(ev / "tables" / f"{m}.pre.json", _table_meta(m, marts[m]["fields"], rows, size))
    refs = {"transaction_daily": ["curated_transactions"], "operations_daily":
            ["curated_transactions"], "customer_daily": ["curated_transactions", "customers"],
            "account_daily": ["curated_transactions", "accounts"]}
    for name in mb.DRY_RUNS:
        request = json.loads((out / f"requests/dryrun-{name}.json").read_text())
        query = {"statementType": "SCRIPT" if name == "script" else "SELECT",
                 "totalBytesProcessed": "188000"}
        if name != "script":
            query["referencedTables"] = [{"projectId": "ebank-pilot-dev-261031-k7m4",
                                          "datasetId": "bankpilot_dev", "tableId": t}
                                         for t in refs[name]]
            query["schema"] = {"fields": [
                {"name": f["name"], "type": "INTEGER" if f["type"] == "INT64" else f["type"],
                 "mode": "NULLABLE"} for f in marts[name]["fields"]]}
        _write(ev / "dryrun" / f"{name}.json", {
            "kind": "bigquery#job", "configuration": request["configuration"],
            "status": {"state": "DONE"}, "statistics": {"query": query}})
    request = json.loads((out / "requests/script.job.json").read_text())
    _write(ev / "jobs" / "script.json", {
        "kind": "bigquery#job", "jobReference": request["jobReference"],
        "user_email": record["runner_email"], "configuration": request["configuration"],
        "status": {"state": "DONE"},
        "statistics": {"creationTime": IN_WINDOW_MS, "startTime": IN_WINDOW_MS,
                       "endTime": IN_WINDOW_MS, "numChildJobs": "10",
                       "totalBytesBilled": str(140 * 1024**2),
                       "totalBytesProcessed": "1000000", "query": {"statementType": "SCRIPT"}}})
    children = []
    for kind, count in mb.CHILD_TYPES.items():
        for i in range(count):
            query = {"statementType": kind, "totalBytesBilled": "0" if "TRANS" in kind
                     else str(10 * 1024**2)}
            if kind == "INSERT":
                query["dmlStats"] = {"insertedRowCount": str(
                    expected["marts"][mb.MARTS[i]]["row_count"])}
            if kind == "DELETE":
                pre = json.loads((ev / "tables" / f"{mb.MARTS[i]}.pre.json").read_text())
                query["dmlStats"] = {"deletedRowCount": pre["numRows"]}
            children.append({"kind": "bigquery#job", "state": "DONE",
                             "user_email": record["runner_email"], "status": {"state": "DONE"},
                             "statistics": {"query": query}})
    _write(ev / "jobs" / "children.json", {"kind": "bigquery#jobList", "jobs": children})
    for m in mb.MARTS:
        fields = marts[m]["fields"]
        meta = _table_meta(m, fields, expected["marts"][m]["row_count"],
                           expected["marts"][m]["logical_bytes"], IN_WINDOW_MS)
        folder = ev / "readback" / m
        _write(folder / "table.before.json", meta)
        _write(folder / "table.after.json", meta)
        for n, (token, page) in enumerate(_pages(expected["marts"][m]["rows"], fields), 1):
            _write(folder / f"page-{n:04d}.token", token)
            _write(folder / f"page-{n:04d}.json", page)


@pytest.fixture
def evidence(bundle, expected):
    out, _ = bundle
    write_evidence(out, expected)
    return out


def test_complete_evidence_passes_every_stage(evidence):
    report = mb.run_stage(evidence, PACKET, MANIFEST, "all")
    assert report["ok"], report["problems"]
    assert {m: s["row_count"] for m, s in report["marts"].items()} == EXPECTED_ROWS
    assert report["ledger"]["jobs"] == 16 and report["ledger"]["bytes_billed"] == 140 * 1024**2
    for stage, name in (("preflight", None), ("dryrun", "script"), ("job", None),
                        ("children", None), ("readback", "operations_daily"),
                        ("post-inputs", None)):
        assert mb.run_stage(evidence, PACKET, MANIFEST, stage, name)["ok"], stage


def test_idempotent_replay_over_already_built_marts_passes(bundle, expected):
    out, _ = bundle
    write_evidence(out, expected, mart_pre_rows=None)
    assert mb.run_stage(out, PACKET, MANIFEST, "all")["ok"]


def _j(out: Path, rel: str):
    return json.loads((out / "evidence" / rel).read_text())


def _edit(rel: str, change):
    def apply(out: Path):
        path = out / "evidence" / rel
        doc = json.loads(path.read_text())
        change(doc)
        path.write_text(json.dumps(doc))
    return apply


def _page_cell(mart: str, column: str, value_fn, row: int = 0):
    def change(page):
        index = COLUMNS[mart].index(column)
        cell = page["rows"][row]["f"][index]
        cell["v"] = value_fn(cell["v"])
    return _edit(f"readback/{mart}/page-0001.json", change)


def _child(kind: str, change):
    def apply(job_list):
        change(next(j for j in job_list["jobs"]
                    if j["statistics"]["query"]["statementType"] == kind))
    return _edit("jobs/children.json", apply)


EVIDENCE_BAD = {
    # financial accuracy / reconciliation
    "amount off by one minor unit": _page_cell("transaction_daily", "total_amount_minor",
                                               lambda v: str(int(v) + 1)),
    "count off by one": _page_cell("operations_daily", "transaction_count",
                                   lambda v: str(int(v) + 1)),
    "currency relabelled": _page_cell("account_daily", "currency",
                                      lambda v: "USD" if v != "USD" else "EUR"),
    "customer outside customers": _page_cell("customer_daily", "customer_id",
                                             lambda v: "C2-99999999"),
    "account owner changed": _page_cell("account_daily", "customer_id",
                                        lambda v: "C2-99999999"),
    "event moved a day": _page_cell("transaction_daily", "event_date",
                                    lambda v: "2024-12-31"),
    "sub-second timestamp": _page_cell("customer_daily", "first_event_time",
                                       lambda v: str(int(v) + 1)),
    "invalid date": _page_cell("operations_daily", "event_date", lambda v: "2025-02-30"),
    "null amount": _page_cell("account_daily", "total_amount_minor", lambda v: None),
    "float amount": _page_cell("transaction_daily", "total_amount_minor",
                               lambda v: f"{v}.0"),
    "duplicate row": _edit("readback/transaction_daily/page-0001.json", lambda p: (
        p["rows"].__setitem__(1, copy.deepcopy(p["rows"][0])))),
    "row missing": lambda out: [_edit(f"readback/account_daily/page-000{n}.json", lambda p: (
        p.update(totalRows=str(int(p["totalRows"]) - 1)),
        p["rows"].pop() if "pageToken" not in p else None))(out) for n in range(1, 5)],
    "truncated pagination": _edit("readback/operations_daily/page-0004.json",
                                  lambda p: p.update(pageToken="more")),
    "numBytes off model": _edit("readback/customer_daily/table.after.json",
                                lambda m: m.update(numBytes=str(int(m["numBytes"]) + 8))),
    "table changed during read-back": _edit("readback/customer_daily/table.after.json",
                                            lambda m: m.update(lastModifiedTime="9" * 13)),
    # identity, SQL and ledger
    "job not by runner": _edit("jobs/script.json",
                               lambda j: j.update(user_email="someone@example.invalid")),
    "job SQL altered": _edit("jobs/script.json", lambda j: j["configuration"]["query"].update(
        query=j["configuration"]["query"]["query"].replace("WHERE TRUE", "WHERE FALSE", 1))),
    "byte cap raised": _edit("jobs/script.json", lambda j: j["configuration"]["query"].update(
        maximumBytesBilled="1073741824")),
    "job failed": _edit("jobs/script.json", lambda j: j["status"].update(
        errorResult={"reason": "invalidQuery"})),
    "job still running": _edit("jobs/script.json", lambda j: j["status"].update(
        state="RUNNING")),
    "extra child job": _edit("jobs/script.json", lambda j: j["statistics"].update(
        numChildJobs="11")),
    "bytes above ledger": _edit("jobs/script.json", lambda j: j["statistics"].update(
        totalBytesBilled=str(900 * 1024**2))),
    "job outside window": _edit("jobs/script.json", lambda j: j["statistics"].update(
        endTime="1700000000000")),
    "wrong job id": _edit("jobs/script.json", lambda j: j["jobReference"].update(
        jobId="m8_other")),
    "child is a DROP": _child("DELETE", lambda j: j["statistics"]["query"].update(
        statementType="DROP_TABLE")),
    "child billed above cap": _child("INSERT", lambda j: j["statistics"]["query"].update(
        totalBytesBilled=str(200 * 1024**2))),
    "child error": _child("INSERT", lambda j: j.update(errorResult={"reason": "x"})),
    "child by another identity": _child("COMMIT_TRANSACTION",
                                        lambda j: j.update(user_email="x@example.invalid")),
    "child inserted wrong rows": _child("INSERT", lambda j: j["statistics"]["query"][
        "dmlStats"].update(insertedRowCount="1")),
    "children listing incomplete": _edit("jobs/children.json",
                                         lambda d: d.update(nextPageToken="t")),
    # dry runs
    "dry run reads a mart": _edit("dryrun/transaction_daily.json", lambda d: d["statistics"][
        "query"]["referencedTables"].append({"projectId": "ebank-pilot-dev-261031-k7m4",
                                             "datasetId": "bankpilot_dev",
                                             "tableId": "customer_daily"})),
    "dry run is DML": _edit("dryrun/account_daily.json", lambda d: d["statistics"][
        "query"].update(statementType="INSERT")),
    "dry run float money": _edit("dryrun/customer_daily.json", lambda d: d["statistics"][
        "query"]["schema"]["fields"][5].update(type="FLOAT")),
    "dry run above cap": _edit("dryrun/operations_daily.json", lambda d: d["statistics"][
        "query"].update(totalBytesProcessed=str(200 * 1024**2))),
    "dry run not dry": _edit("dryrun/script.json", lambda d: d["configuration"].pop("dryRun")),
    # inputs and preflight
    "input rows differ": _edit("tables/customers.pre.json", lambda m: m.update(numRows="99")),
    "input modified during M8": _edit("tables/accounts.post.json",
                                      lambda m: m.update(lastModifiedTime="1794999999999")),
    "mart has unexpected rows": _edit("tables/account_daily.pre.json",
                                      lambda m: m.update(numRows="5", numBytes="40")),
    "mart schema relaxed": _edit("tables/operations_daily.pre.json", lambda m: m["schema"][
        "fields"][0].update(mode="NULLABLE")),
}


@pytest.mark.parametrize("name", EVIDENCE_BAD)
def test_adversarial_evidence_is_rejected(name, evidence):
    EVIDENCE_BAD[name](evidence)
    report = mb.run_stage(evidence, PACKET, MANIFEST, "all")
    assert not report["ok"], name
    assert report["problems"], name


def test_missing_evidence_fails_closed(evidence):
    (evidence / "evidence/jobs/children.json").unlink()
    with pytest.raises(mb.MartError, match="children.json"):
        mb.run_stage(evidence, PACKET, MANIFEST, "all")


# --------------------------------------------------------------------------- direct checks
def test_financial_checks_name_their_cause(expected):
    rows = copy.deepcopy(expected["marts"]["customer_daily"]["rows"])
    cols = COLUMNS["customer_daily"]
    rows[0][cols.index("customer_id")] = "C2-99999999"
    rows[1][cols.index("active_account_count")] = "0"
    rows[2][cols.index("first_event_time")] = "2099-01-01T00:00:00Z"
    rows.append(copy.deepcopy(rows[3]))
    problems = mb.financial_problems("customer_daily", rows, expected)
    text = " ".join(problems)
    for cause in ("anti-join", "active_account_count", "first_event_time after",
                  "duplicate grain", "per-currency"):
        assert cause in text, cause


def test_grains_carry_currency_and_money_stays_integer():
    for mart, grain in GRAINS.items():
        assert "currency" in grain
        types = {f["name"]: f["type"] for f in bigquery_schemas()["tables"][mart]["fields"]}
        assert types["total_amount_minor"] == "INT64"


def test_date_readback_conversion_is_strict():
    field = {"name": "event_date", "type": "DATE"}
    assert convert_value({"v": "2025-01-31"}, field) == "2025-01-31"
    for bad in ("2025-1-31", "2025-02-30", "20250131", "2025-01-31T00:00:00Z", None):
        with pytest.raises(ReadbackError):
            convert_value({"v": bad}, field)


def test_cli_prints_expectations_without_rows():
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        cli_main(["mart-expectations", "--packet-dir", str(PACKET),
                  "--expected-manifest-sha256", MANIFEST])
    report = json.loads(buffer.getvalue())
    assert {m: s["row_count"] for m, s in report["marts"].items()} == EXPECTED_ROWS
    assert "rows" not in json.dumps(report["marts"])
