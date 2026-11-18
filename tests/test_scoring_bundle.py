"""M9D: the BigQuery scoring bundle and its offline evidence verifier (no cloud calls).

A small M9A/M9B/M9C chain is built in temporary directories; the real M7B pilot packet is
scored. Evidence is synthesised in the shapes the BigQuery REST API returns.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import test_marts_bundle as tm
import test_scoring_sql as ts

from financial_crime_ml.data_generation.v2.contracts import SyntheticConfig
from financial_crime_ml.data_generation.v2.workflow import run_generation
from financial_crime_ml.deployment import m9c_workflow as m9c
from financial_crime_ml.deployment import model_spec as ms
from financial_crime_ml.deployment import scoring_bundle as sb
from financial_crime_ml.evaluation import evaluation_v2 as ev
from financial_crime_ml.governance.m9b_workflow import run_explainability

PACKET, MANIFEST = tm.PACKET, tm.MANIFEST
START, EXPIRY = "2026-11-20T10:00:00Z", "2026-11-20T13:00:00Z"
ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(not PACKET.is_dir(), reason="M7B sample packet not available")


@pytest.fixture(scope="module")
def m9c_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("m9d")
    run_generation(replace(SyntheticConfig(), seed=5, transactions=6000, customers=120,
                           accounts=180, beneficiaries=360, devices=240), "data", runs_root=root)
    (root / "fast.yaml").write_text(ts.FAST)
    ev.run_evaluation_v2(root / "data", root / "fast.yaml", "m9a", runs_root=root)
    run_explainability(root / "m9a", "m9b", runs_root=root, repeats=2)
    result = m9c.run_m9c(root / "m9a", root / "m9b", "m9c", runs_root=root,
                         approval_path=ts.write_approval(root))
    return result["run_dir"]


@pytest.fixture
def bundle(m9c_run, tmp_path):
    out = tmp_path / "m9d"
    summary = sb.render_bundle(PACKET, m9c_run, expected_manifest_sha256=MANIFEST,
                               window_start=START, window_expiry=EXPIRY, output_dir=out,
                               repo_dir=ROOT)
    return out, summary


# --------------------------------------------------------------------------- evidence
def _cells(row: list) -> dict:
    return {"f": [{"v": v} for v in row]}


def write_evidence(out: Path, *, score_pre_rows: int = 0) -> None:
    record = json.loads((out / "bundle.json").read_text())
    spec = json.loads((out / "model_spec.json").read_text())
    ev_dir = out / "evidence"
    source = json.loads((ROOT / "infrastructure/schemas/v1/source_tables.json").read_text())
    marts = json.loads((ROOT / "sql/analytics/v1/bigquery.schemas.json").read_text())["tables"]
    for t in sb.INPUTS:
        meta = tm._table_meta(t, source[t], record["inputs"][t]["num_rows"],
                              record["inputs"][t]["num_bytes"], "1794911068158")
        tm._write(ev_dir / "tables" / f"{t}.pre.json", meta)
        tm._write(ev_dir / "tables" / f"{t}.post.json", meta)
    for t in sb.MARTS:
        meta = tm._table_meta(t, marts[t]["fields"], record["marts"][t]["num_rows"],
                              record["marts"][t]["num_bytes"], "1794999999000")
        tm._write(ev_dir / "tables" / f"{t}.pre.json", meta)
        tm._write(ev_dir / "tables" / f"{t}.post.json", meta)
    score_fields = sb.score_schema()
    tm._write(ev_dir / "tables" / f"{sb.SCORE_TABLE}.pre.json",
              tm._table_meta(sb.SCORE_TABLE, score_fields, score_pre_rows,
                             123456 if score_pre_rows else 0, "1795000000000"))
    tm._write(ev_dir / "preflight" / "query.json", {
        "kind": "bigquery#queryResponse", "jobComplete": True, "totalRows": "1",
        "totalBytesProcessed": "500000",
        "schema": {"fields": [{"name": n, "type": "INTEGER", "mode": "NULLABLE"} for n in
                              ("row_count", "duplicate_ids", "invalid_rows",
                               "sub_second_timestamps")]},
        "rows": [_cells([str(record["expected"]["rows"]), "0", "0", "0"])]})
    for name in sb.DRY_RUNS:
        request = json.loads((out / "requests" / f"dryrun-{name}.json").read_text())
        query = {"statementType": "SELECT" if name == "select" else "SCRIPT",
                 "totalBytesProcessed": "650000"}
        if name == "select":
            query["referencedTables"] = [{"projectId": record["target"]["project_id"],
                                          "datasetId": "bankpilot_dev",
                                          "tableId": "curated_transactions"}]
        tm._write(ev_dir / "dryrun" / f"{name}.json", {
            "kind": "bigquery#job", "configuration": request["configuration"],
            "status": {"state": "DONE"}, "statistics": {"query": query}})
    request = json.loads((out / "requests" / "refresh.job.json").read_text())
    tm._write(ev_dir / "jobs" / "refresh.json", {
        "kind": "bigquery#job", "jobReference": request["jobReference"],
        "user_email": record["runner_email"], "configuration": request["configuration"],
        "status": {"state": "DONE"},
        "statistics": {"creationTime": tm.IN_WINDOW_MS, "startTime": tm.IN_WINDOW_MS,
                       "endTime": tm.IN_WINDOW_MS, "numChildJobs": "4",
                       "totalBytesBilled": str(20 * 1024**2),
                       "query": {"statementType": "SCRIPT"}}})
    children = []
    for kind in sb.CHILD_TYPES:
        query = {"statementType": kind,
                 "totalBytesBilled": str(20 * 1024**2) if kind == "INSERT" else "0"}
        if kind == "INSERT":
            query["dmlStats"] = {"insertedRowCount": str(record["expected"]["rows"])}
        if kind == "DELETE":
            query["dmlStats"] = {"deletedRowCount": str(score_pre_rows)}
        children.append({"kind": "bigquery#job", "state": "DONE", "status": {"state": "DONE"},
                         "user_email": record["runner_email"], "statistics": {"query": query}})
    tm._write(ev_dir / "jobs" / "children.json", {"kind": "bigquery#jobList", "jobs": children})
    scores = sb.expected_scores(spec, PACKET, json.loads((PACKET / "manifest.json").read_text()))
    rows = sb.expected_rows(spec, scores)[::-1]  # tabledata.list order is not guaranteed
    folder = ev_dir / "readback" / sb.SCORE_TABLE
    meta = tm._table_meta(sb.SCORE_TABLE, score_fields, len(rows), 98765, tm.IN_WINDOW_MS)
    tm._write(folder / "table.before.json", meta)
    tm._write(folder / "table.after.json", meta)
    chunks = [rows[i:i + 1000] for i in range(0, len(rows), 1000)]
    token = ""
    for n, chunk in enumerate(chunks, 1):
        nxt = f"tok{n}" if n < len(chunks) else None
        tm._write(folder / f"page-{n:04d}.token", token)
        tm._write(folder / f"page-{n:04d}.json", {
            "kind": "bigquery#tableDataList", "etag": "e", "totalRows": str(len(rows)),
            "rows": [_cells(r) for r in chunk], **({"pageToken": nxt} if nxt else {})})
        token = nxt or ""


@pytest.fixture
def evidence(bundle):
    out, _ = bundle
    write_evidence(out)
    return out


def run(out, stage="all", name=None):
    return sb.run_stage(out, PACKET, MANIFEST, stage, name)


# --------------------------------------------------------------------------- happy path
def test_complete_evidence_passes_with_exact_score_parity(evidence):
    report = run(evidence)
    assert report["ok"], report["problems"]
    readback = report["readback"]
    assert readback["rows"] == 4000 and set(readback["parity_mismatches"].values()) == {0}
    record = json.loads((evidence / "bundle.json").read_text())
    assert readback["alerts"] == record["expected"]["alerts"]
    monitoring = readback["monitoring"]
    assert monitoring["score_ppm_band"] in ("stable", "moderate", "significant")
    assert set(monitoring["feature_psi_offline_packet"]) == {
        *sb.MODEL_FEATURES} and report["ledger"]["jobs"] == 8


def test_idempotent_replay_over_an_already_scored_table(bundle):
    out, _ = bundle
    write_evidence(out, score_pre_rows=4000)
    assert run(out)["ok"]


# --------------------------------------------------------------------------- adversarial
def _edit(rel, change):
    def apply(out):
        path = out / "evidence" / rel
        doc = json.loads(path.read_text())
        change(doc)
        path.write_text(json.dumps(doc))
    return apply


def _cell(column: str, value_fn, row: int = 0):
    index = list(sb.sq.OUTPUT_COLUMNS).index(column)

    def change(page):
        cell = page["rows"][row]["f"][index]
        cell["v"] = value_fn(cell["v"])
    return _edit(f"readback/{sb.SCORE_TABLE}/page-0001.json", change)


def _child(kind, change):
    return _edit("jobs/children.json", lambda d: change(next(
        j for j in d["jobs"] if j["statistics"]["query"]["statementType"] == kind)))


EVIDENCE_BAD = {
    "score off by one ppm": _cell("score_ppm", lambda v: str(int(v) + 1)),
    "alert flipped": _cell("alert", lambda v: "0" if v == "1" else "1"),
    "reason code changed": _cell("reason_code_1", lambda v: "R99" if v else "R10"),
    "wrong spec hash": _cell("model_spec_sha256", lambda v: "0" * 64),
    "invalid row": _cell("input_valid", lambda v: "0"),
    "NULL score": _cell("score_ppm", lambda v: None),
    "row missing": lambda out: [_edit(f"readback/{sb.SCORE_TABLE}/page-000{n}.json",
                                      lambda p: (p.update(totalRows="3999"),
                                                 p["rows"].pop() if "pageToken" not in p
                                                 else None))(out) for n in range(1, 5)],
    "transaction id swapped": _cell("transaction_id", lambda v: "T2-99999999"),
    "preflight duplicates": _edit("preflight/query.json", lambda d: d["rows"][0]["f"][1]
                                  .update(v="2")),
    "preflight invalid rows": _edit("preflight/query.json", lambda d: d["rows"][0]["f"][2]
                                    .update(v="1")),
    "preflight incomplete": _edit("preflight/query.json", lambda d: d.update(
        jobComplete=False)),
    "input rows differ": _edit("tables/customers.pre.json", lambda m: m.update(numRows="99")),
    "mart rows differ from M8": _edit("tables/customer_daily.pre.json",
                                      lambda m: m.update(numRows="3411")),
    "score table already has other rows": _edit(f"tables/{sb.SCORE_TABLE}.pre.json",
                                                lambda m: m.update(numRows="17")),
    "score schema relaxed": _edit(f"tables/{sb.SCORE_TABLE}.pre.json", lambda m: m["schema"][
        "fields"][0].update(mode="NULLABLE")),
    "select reads a mart": _edit("dryrun/select.json", lambda d: d["statistics"]["query"][
        "referencedTables"].append({"datasetId": "bankpilot_dev",
                                    "tableId": "customer_daily"})),
    "dry run above cap": _edit("dryrun/refresh.json", lambda d: d["statistics"]["query"]
                               .update(totalBytesProcessed=str(200 * 1024**2))),
    "job not by runner": _edit("jobs/refresh.json", lambda j: j.update(
        user_email="someone@example.invalid")),
    "job SQL altered": _edit("jobs/refresh.json", lambda j: j["configuration"]["query"].update(
        query=re.sub(r">= (\d+) THEN 1", ">= 1 THEN 1", j["configuration"]["query"]["query"]))),
    "byte cap raised": _edit("jobs/refresh.json", lambda j: j["configuration"]["query"].update(
        maximumBytesBilled="1073741824")),
    "job outside window": _edit("jobs/refresh.json", lambda j: j["statistics"].update(
        endTime="1700000000000")),
    "extra child": _edit("jobs/refresh.json", lambda j: j["statistics"].update(
        numChildJobs="5")),
    "child inserted fewer rows": _child("INSERT", lambda j: j["statistics"]["query"][
        "dmlStats"].update(insertedRowCount="3999")),
    "child is a DROP": _child("DELETE", lambda j: j["statistics"]["query"].update(
        statementType="DROP_TABLE")),
    "input modified during M9D": _edit("tables/accounts.post.json",
                                       lambda m: m.update(lastModifiedTime="1795999999999")),
    "mart modified during M9D": _edit("tables/operations_daily.post.json",
                                      lambda m: m.update(numRows="0", numBytes="0")),
}


@pytest.mark.parametrize("name", EVIDENCE_BAD)
def test_adversarial_evidence_is_rejected(name, evidence):
    EVIDENCE_BAD[name](evidence)
    report = run(evidence)
    assert not report["ok"] and report["problems"], name


# --------------------------------------------------------------------------- bundle
def test_bundle_is_deterministic_capped_and_label_free(bundle, m9c_run, tmp_path):
    out, summary = bundle
    again = sb.render_bundle(PACKET, m9c_run, expected_manifest_sha256=MANIFEST,
                             window_start=START, window_expiry=EXPIRY,
                             output_dir=tmp_path / "again", repo_dir=ROOT)
    assert again["bundle_sha256"] == summary["bundle_sha256"]
    assert summary["job_reservation"]["total"] == 8 <= 20
    for name in ("preflight.query.json", "refresh.job.json"):
        text = (out / "requests" / name).read_text()
        assert '"maximumBytesBilled":"104857600"' in text, name
    label_names = r"is_suspicious|scenario_type|episode_id|outcomes\.csv"
    for path in out.rglob("*"):
        if not path.is_file():
            continue
        if path.name == "model_spec.json":  # may only *name* the excluded columns
            spec = json.loads(path.read_text())
            assert spec["feature_contract"]["labels_excluded"]
            spec["feature_contract"].pop("labels_excluded")
            text = json.dumps(spec)
        else:
            text = path.read_text(errors="ignore")
        assert not re.search(label_names, text), path
    assert (out / "expected" / "scores.csv").read_text().splitlines()[0] == \
        "transaction_id,score_ppm,alert,reason_code_1,reason_code_2,reason_code_3"
    refresh = (out / "sql" / "refresh.sql").read_text()
    threshold_ppm = json.loads((out / "model_spec.json").read_text())["decision"]["threshold_ppm"]
    assert f">= {threshold_ppm} THEN 1 ELSE 0 END AS alert" in refresh
    assert "PROJECT" not in refresh and "`ebank-pilot-dev-261031-k7m4.bankpilot_dev." \
        "transaction_risk_scores`" in refresh
    subprocess.run(["bash", "-n", str(out / "m9d.sh")], check=True)
    script = (out / "m9d.sh").read_text()
    assert script.count("print-access-token") == 1
    for forbidden in ("setIamPolicy", "add-iam-policy-binding", "-X DELETE", "tables.delete",
                      "keys create"):
        assert forbidden not in script


@pytest.mark.parametrize("tamper", ["model_spec.json", "sql/refresh.sql",
                                    "expected/scores.csv", "requests/refresh.job.json"])
def test_tampered_bundle_is_rejected(bundle, tamper):
    out, _ = bundle
    path = out / tamper
    path.write_text(path.read_text().replace("4", "5", 1))
    with pytest.raises(sb.ScoringError):
        run(out, "bundle")


def test_m9c_run_must_be_v2_untampered_and_regenerate_its_sql(m9c_run, tmp_path):
    copy_dir = tmp_path / "m9c"
    shutil.copytree(m9c_run, copy_dir)
    sql = copy_dir / "scoring_refresh.googlesql.sql"
    sql.write_text(sql.read_text() + "\n-- edited\n")
    with pytest.raises(sb.ScoringError, match="hash mismatch"):
        sb.load_m9c(copy_dir)
    manifest = json.loads((m9c_run / "manifest.json").read_text())
    assert manifest["spec_version"] == ms.SPEC_V2
    sb.load_m9c(m9c_run)  # the untouched run is accepted


def test_bundle_refuses_git_worktree(m9c_run):
    with pytest.raises(sb.ScoringError, match="git worktree"):
        sb.render_bundle(PACKET, m9c_run, expected_manifest_sha256=MANIFEST, window_start=START,
                         window_expiry=EXPIRY, output_dir=ROOT / "outputs" / "m9d-x",
                         repo_dir=ROOT)
