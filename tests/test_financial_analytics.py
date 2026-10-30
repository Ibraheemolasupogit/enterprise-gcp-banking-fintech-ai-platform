"""SQL correctness, independent grouped expectations and hostile M3 inputs."""

import csv
import json
import sqlite3
from collections import defaultdict

import pytest

from financial_crime_ml import cli
from financial_crime_ml.analytics import workflow
from financial_crime_ml.analytics.contracts import COLUMNS, GRAINS, bigquery_schemas
from financial_crime_ml.data_generation.v2.contracts import SyntheticConfig
from financial_crime_ml.data_generation.v2.generator import generate_v2
from financial_crime_ml.evaluation.manifest import sha256_bytes
from financial_crime_ml.ingestion.foundation.pipeline import ingest


@pytest.fixture
def source(tmp_path):
    frames = generate_v2(SyntheticConfig(customers=4, accounts=6, beneficiaries=12,
                                        devices=8, transactions=100))
    path = tmp_path / "input"
    path.mkdir()
    for name, frame in frames.items():
        frame.to_csv(path / f"{name}.csv", index=False)
    result = ingest(path, source_id="analytics-fixture", source_time="2026-01-01T00:00:00Z",
                    ingestion_time="2026-01-02T00:00:00Z", run_id="source", runs_root=tmp_path)
    return result["run_dir"]


def rows(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def build(source, tmp_path, name="analytics"):
    return workflow.build_marts(source, name, runs_root=tmp_path)


def rewrite(source, relative, mutate):
    path = source / relative
    content = mutate(path.read_bytes())
    path.write_bytes(content)
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["artifacts_sha256"][relative] = sha256_bytes(content)
    (source / "manifest.json").write_text(json.dumps(manifest))


def test_sql_metrics_grains_joins_and_currency_against_independent_python(source, tmp_path):
    result = build(source, tmp_path)
    assert result["manifest"]["status"] == "completed"
    data = rows(source / "curated/transactions.csv")
    customers = {r["customer_id"]: r for r in rows(source / "validated/customers.csv")}
    accounts = {r["account_id"]: r for r in rows(source / "validated/accounts.csv")}
    with sqlite3.connect(result["run_dir"] / "analytics.sqlite") as connection:
        for table, grain in GRAINS.items():
            expected = defaultdict(list)
            for row in data:
                keys = {**row, "event_date": row["transaction_timestamp"][:10]}
                expected[tuple(keys[f] for f in grain)].append(row)
            actual = rows(result["run_dir"] / f"{table}.csv")
            assert len(actual) == len(expected)
            assert len({tuple(row[f] for f in grain) for row in actual}) == len(actual)
            for row in actual:
                observations = expected[tuple(row[f] for f in grain)]
                assert int(row["transaction_count"]) == len(observations)
                assert int(row["total_amount_minor"]) == sum(
                    int(r["amount_minor"]) for r in observations)
                assert row["first_event_time"] == min(r["transaction_timestamp"]
                                                        for r in observations)
                assert row["last_event_time"] == max(r["transaction_timestamp"]
                                                       for r in observations)
                for entity in ("customer", "account"):
                    field = f"active_{entity}_count"
                    if field in row:
                        assert int(row[field]) == len({r[f"{entity}_id"] for r in observations})
                if table == "customer_daily":
                    assert row["customer_country"] == customers[row["customer_id"]]["country"]
                if table == "account_daily":
                    assert row["customer_id"] == accounts[row["account_id"]]["customer_id"]
                assert all(value not in (None, "") for value in row.values())
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == len(actual)
    lineage = [json.loads(line) for line in (result["run_dir"] / "lineage.jsonl").read_text()
               .splitlines()]
    assert len(lineage) == 4 * len(data)
    for entry in lineage:
        original = next(row for row in data if row["transaction_id"] == entry["transaction_id"])
        assert entry["source_row"] == original["source_row"]
        assert entry["source_sha256"] == original["source_sha256"]
        keys = {**original, "event_date": original["transaction_timestamp"][:10]}
        assert entry["grain"] == {f: keys[f] for f in GRAINS[entry["mart"]]}


def test_deterministic_payloads_idempotence_collision_and_tampering(source, tmp_path):
    first = build(source, tmp_path)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in first["run_dir"].iterdir()}
    assert build(source, tmp_path)["reused"]
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in first["run_dir"].iterdir()}
    second = build(source, tmp_path, "second")
    assert first["manifest"]["artifacts_sha256"] == second["manifest"]["artifacts_sha256"]
    (first["run_dir"] / "transaction_daily.csv").write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        build(source, tmp_path)
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["run_id"] = "different-source-run"
    (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="collision"):
        build(source, tmp_path, "second")


@pytest.mark.parametrize("relative", ["curated/transactions.csv", "validated/customers.csv",
                                       "reconciliation.json", "raw/transactions.csv"])
def test_unhashed_source_changes_rejected(source, tmp_path, relative):
    with (source / relative).open("ab") as handle:
        handle.write(b"bad")
    with pytest.raises(ValueError, match="hash mismatch"):
        build(source, tmp_path)


@pytest.mark.parametrize("mutation", ["null", "duplicate", "malformed", "join", "amount",
                                      "time", "extra_column"])
def test_even_rehashed_invalid_curated_is_rejected(source, tmp_path, mutation):
    def mutate(content):
        text = content.decode()
        lines = text.splitlines()
        if mutation == "null":
            return text.replace("C2-00000001", "", 1).encode()
        if mutation == "duplicate":
            return (text + lines[1] + "\n").encode()
        if mutation == "malformed":
            return (text + "short,row\n").encode()
        if mutation == "extra_column":
            return text.replace("transaction_id,", "unexpected,transaction_id,", 1).encode()
        field = {"join": "customer_country", "amount": "amount_minor",
                 "time": "transaction_timestamp"}[mutation]
        index = lines[0].split(",").index(field)
        values = lines[1].split(",")
        values[index] = {"join": "US", "amount": "1.001", "time": "invalid"}[mutation]
        if mutation == "join":
            # Always corrupt the join, regardless of the fixture country.
            original = lines[1].split(",")[index]
            values[index] = "US" if original != "US" else "GB"
        lines[1] = ",".join(values)
        return ("\n".join(lines) + "\n").encode()
    rewrite(source, "curated/transactions.csv", mutate)
    with pytest.raises(ValueError):
        build(source, tmp_path)


def test_rehashed_bad_dimension_reconciliation_and_manifest_rejected(source, tmp_path):
    rewrite(source, "validated/accounts.csv", lambda b: b.replace(b"A2-00000001", b"A2-99999999"))
    with pytest.raises(ValueError, match="Validated accounts"):
        build(source, tmp_path)


def test_reconciliation_is_recomputed(source, tmp_path):
    rewrite(source, "reconciliation.json", lambda b: b.replace(b'"curated_rows": 100',
                                                              b'"curated_rows": 99'))
    with pytest.raises(ValueError, match="reconciliation"):
        build(source, tmp_path)


def test_failed_source_or_path_escape_is_rejected(source, tmp_path):
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["status"] = "failed"
    (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="completed"):
        build(source, tmp_path)
    manifest["status"] = "completed"
    manifest["artifacts_sha256"]["../escape"] = "invalid"
    (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Unsafe"):
        build(source, tmp_path)


def test_empty_batch_and_proposed_schema(source, tmp_path):
    # Build a valid empty snapshot with explicit headers through M3 itself.
    inputs = tmp_path / "empty"
    inputs.mkdir()
    for table in ("customers", "accounts", "beneficiaries", "devices", "transactions"):
        header = (source / f"raw/{table}.csv").read_text().splitlines()[0]
        (inputs / f"{table}.csv").write_text(header + "\n")
    empty = ingest(inputs, source_id="empty", source_time="2026-01-01T00:00:00Z",
                   ingestion_time="2026-01-02T00:00:00Z", run_id="empty",
                   runs_root=tmp_path / "runs")
    result = build(empty["run_dir"], tmp_path)
    assert result["manifest"]["status"] == "completed"
    for table in GRAINS:
        assert rows(result["run_dir"] / f"{table}.csv") == []
        schema = bigquery_schemas()["tables"][table]
        assert [f["name"] for f in schema["fields"]] == list(COLUMNS[table])
        assert all(f["mode"] == "REQUIRED" for f in schema["fields"])
        assert "currency" in schema["grain"]


def test_broken_sql_fails_without_publishing_success(source, tmp_path, monkeypatch):
    sql = tmp_path / "bad.sql"
    sql.write_text("CREATE TABLE broken AS SELECT nonexistent FROM curated_transactions;")
    monkeypatch.setattr(workflow, "SQL_PATH", sql)
    result = build(source, tmp_path)
    assert result["manifest"]["status"] == "failed"
    assert "OperationalError" == result["manifest"]["error"]["type"]


def test_source_with_quarantine_is_explicitly_supported(source, tmp_path):
    inputs = tmp_path / "damaged"
    inputs.mkdir()
    for path in (source / "raw").glob("*.csv"):
        content = path.read_bytes()
        if path.name == "transactions.csv":
            content += content.splitlines()[1] + b"\n"
        (inputs / path.name).write_bytes(content)
    partial = ingest(inputs, source_id="partial", source_time="2026-01-01T00:00:00Z",
                     ingestion_time="2026-01-02T00:00:00Z", run_id="partial", runs_root=tmp_path)
    result = build(partial["run_dir"], tmp_path)
    assert result["manifest"]["status"] == "completed"
    assert result["manifest"]["source_status"] == "completed_with_rejects"
    report = json.loads((result["run_dir"] / "reconciliation.json").read_text())
    assert sum(t[0] for t in report["transaction_daily"]
               ["count_and_amount_minor_by_currency"].values()) == 99


def test_cli_dispatch(source, tmp_path, monkeypatch, capsys):
    actual = workflow.build_marts
    monkeypatch.setattr(workflow, "build_marts", lambda **kw: actual(**kw, runs_root=tmp_path))
    cli.main(["build-financial-marts", "--source-run", str(source), "--run-id", "cli"])
    assert "Financial marts completed" in capsys.readouterr().out
    cli.main(["build-financial-marts", "--source-run", str(source), "--run-id", "cli"])
    assert "Reused: True" in capsys.readouterr().out


@pytest.mark.parametrize(("statement", "failure"), [
    ("UPDATE transaction_daily SET first_event_time = NULL;", "Null mart field"),
    ("INSERT INTO account_daily SELECT * FROM account_daily;", "UNIQUE constraint"),
    ("UPDATE operations_daily SET transaction_count = 0;", "reconciliation failed"),
    ("DELETE FROM customer_daily;", "reconciliation failed"),
])
def test_sql_runtime_gates_block_null_fanout_and_lost_totals(
    source, tmp_path, monkeypatch, statement, failure
):
    sql = tmp_path / "modified.sql"
    sql.write_text(workflow.SQL_PATH.read_text() + "\n" + statement)
    monkeypatch.setattr(workflow, "SQL_PATH", sql)
    result = build(source, tmp_path)
    assert result["manifest"]["status"] == "failed"
    assert failure in result["manifest"]["error"]["detail"]


def test_versioned_bigquery_schema_matches_contract():
    path = workflow.REPO_ROOT / "sql/analytics/v1/bigquery.schemas.json"
    assert json.loads(path.read_text()) == json.loads(json.dumps(bigquery_schemas()))
    assert bigquery_schemas()["status"] == "proposed_not_cloud_tested"
