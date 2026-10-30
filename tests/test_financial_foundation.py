"""Adversarial snapshots, deterministic lineage, SQL reconciliation and compatibility."""

import csv
import io
import json
import sqlite3

import pytest

from financial_crime_ml import cli
from financial_crime_ml.data_generation.v2.contracts import SyntheticConfig
from financial_crime_ml.data_generation.v2.generator import generate_v2
from financial_crime_ml.evaluation.fraud_baseline import run_fraud_evaluation
from financial_crime_ml.ingestion.foundation.contracts import TABLES
from financial_crime_ml.ingestion.foundation.pipeline import ingest, transform

SOURCE_TIME = "2026-01-01T00:00:00Z"
INGESTION_TIME = "2026-01-02T00:00:00Z"


@pytest.fixture
def data():
    frames = generate_v2(SyntheticConfig(customers=6, accounts=10, beneficiaries=20,
                                        devices=12, transactions=600, fraud_prevalence=0.15))
    return {name: frame.astype(str) for name, frame in frames.items()}


def encode(data):
    return {table: data[table].to_csv(index=False, lineterminator="\n").encode()
            for table in TABLES}


def run(tmp_path, data, name="first", **kwargs):
    source = tmp_path / "input"
    source.mkdir(exist_ok=True)
    for name_, content in encode(data).items():
        (source / f"{name_}.csv").write_bytes(content)
    return ingest(source, source_id="synthetic-v2", source_time=SOURCE_TIME,
                  ingestion_time=INGESTION_TIME, run_id=name, runs_root=tmp_path / "runs",
                  **kwargs)


def read_csv(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_end_to_end_raw_lineage_sql_reconciliation_and_idempotence(tmp_path, data):
    first = run(tmp_path, data)
    directory = first["run_dir"]
    assert first["manifest"]["status"] == "completed"
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
              for p in directory.rglob("*") if p.is_file()}
    assert run(tmp_path, data)["reused"]
    assert before == {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in directory.rglob("*") if p.is_file()}
    second = run(tmp_path, data, "second")
    assert first["manifest"]["artifacts_sha256"] == second["manifest"]["artifacts_sha256"]
    curated = read_csv(directory / "curated/transactions.csv")
    raw = read_csv(directory / "raw/transactions.csv")
    assert len(curated) == 600
    assert not {"is_suspicious", "scenario_type", "episode_id"} & curated[0].keys()
    for row in curated:
        original = raw[int(row["source_row"]) - 1]
        assert all(row[key] == value for key, value in original.items())
        assert row["source_sha256"] == first["manifest"]["input_sha256"]["transactions.csv"]
    connection = sqlite3.connect(":memory:")
    connection.executescript((directory / "curated/schema.sql").read_text())
    fields = list(curated[0])
    connection.executemany(
        f'INSERT INTO curated_transactions VALUES ({",".join("?" for _ in fields)})',
        [[row[field] for field in fields] for row in curated],
    )
    sql_totals = dict(connection.execute(
        "SELECT currency, SUM(amount_minor) FROM curated_transactions GROUP BY currency"
    ))
    report = json.loads((directory / "reconciliation.json").read_text())["transactions"]
    assert sql_totals == report["source_money"]["amount_minor_by_currency"]
    assert report["source_rows"] == report["validated_rows"] == report["curated_rows"]
    assert report["curated_money"] == report["validated_money"] == report["source_money"]


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("account_id", "A2-99999999", "reference.account_id"),
    ("transaction_timestamp", "2027-01-01T00:00:00Z", "time.after_source_snapshot"),
    ("transaction_timestamp", "2000-01-01T00:00:00Z", "time.before_customers"),
    ("transaction_timestamp", "2025-01-01", "schema.transaction_timestamp"),
    ("amount_minor", "0", "schema.amount_minor"),
    ("amount_minor", "10.5", "schema.amount_minor"),
    ("amount_minor", "00010", "schema.amount_minor"),
    ("amount", "0.001", "money.precision"),
    ("currency", "XXX", "schema.currency"),
    ("transaction_id", "bad", "schema.transaction_id"),
])
def test_quarantine_explains_invalid_observations(data, field, value, reason):
    data["transactions"].loc[0, field] = value
    accepted, rejected, totals = transform(encode(data), SOURCE_TIME)
    assert reason in rejected["transactions"][0]["reasons"]
    assert len(accepted["transactions"]) == 599
    assert totals["transactions"]["source_rows"] == 600
    assert rejected["transactions"][0]["source_row"] == 1


def test_duplicate_keys_all_rejected_and_children_cascade(data):
    data["customers"].loc[1, "customer_id"] = data["customers"].loc[0, "customer_id"]
    accepted, rejected, _ = transform(encode(data), SOURCE_TIME)
    assert len(rejected["customers"]) == 2
    assert all("duplicate.primary_key" in row["reasons"] for row in rejected["customers"])
    assert rejected["accounts"] and rejected["transactions"]
    assert len(accepted["customers"]) == 4


def test_duplicate_transaction_no_arbitrary_survivor_and_money_reconciles(tmp_path, data):
    data["transactions"].loc[1, "transaction_id"] = data["transactions"].loc[0, "transaction_id"]
    data["transactions"].loc[2, "amount_minor"] = "not-money"
    result = run(tmp_path, data)
    assert result["manifest"]["status"] == "completed_with_rejects"
    report = json.loads((result["run_dir"] / "reconciliation.json").read_text())["transactions"]
    assert report["source_rows"] == 600
    assert report["validated_rows"] == report["curated_rows"] == 597
    assert report["rejected_rows"] == 3
    assert report["source_money"]["unaccountable_rows"] == 1
    for currency, total in report["source_money"]["amount_minor_by_currency"].items():
        assert total == sum(report[s]["amount_minor_by_currency"].get(currency, 0)
                            for s in ("validated_money", "rejected_money"))
    quarantine = [json.loads(line) for line in
                  (result["run_dir"] / "quarantine/transactions.jsonl").read_text().splitlines()]
    assert quarantine[0]["source_sha256"]
    assert quarantine[0]["values"][0] == data["transactions"].loc[0, "transaction_id"]


def test_ownership_currency_entity_chronology(data):
    row = data["transactions"].iloc[0]
    other = data["accounts"].loc[data["accounts"]["customer_id"] != row["customer_id"]].iloc[0]
    data["transactions"].loc[0, "account_id"] = other["account_id"]
    _, rejects, _ = transform(encode(data), SOURCE_TIME)
    assert "ownership.accounts" in rejects["transactions"][0]["reasons"]
    data["accounts"].loc[0, "opened_at"] = "1990-01-01T00:00:00Z"
    _, rejects, _ = transform(encode(data), SOURCE_TIME)
    assert "time.before_customers" in rejects["accounts"][0]["reasons"]


@pytest.mark.parametrize("change", ["extra_header", "short_row", "wide_row"])
def test_malformed_csv_rows_quarantined(data, change):
    contents = encode(data)
    rows = list(csv.reader(io.StringIO(contents["transactions"].decode())))
    if change == "extra_header":
        rows[0].append("is_suspicious")
    elif change == "short_row":
        rows[1].pop()
    else:
        rows[1].append("uncontracted")
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    contents["transactions"] = output.getvalue().encode()
    _, rejects, _ = transform(contents, SOURCE_TIME)
    assert "schema.header" in rejects["transactions"][0]["reasons"] if (
        change == "extra_header"
    ) else "schema.row_width" in rejects["transactions"][0]["reasons"]


def test_corrupt_encoding_fatal_but_raw_preserved(tmp_path, data):
    run(tmp_path, data)
    source = tmp_path / "input"
    (source / "transactions.csv").write_bytes(b"\xff")
    result = ingest(source, source_id="bad", source_time=SOURCE_TIME,
                    ingestion_time=INGESTION_TIME, run_id="bad", runs_root=tmp_path / "runs")
    assert result["manifest"]["status"] == "failed"
    assert (result["run_dir"] / "raw/transactions.csv").read_bytes() == b"\xff"
    assert result["manifest"]["error"]["type"] == "UnicodeDecodeError"


def test_changed_input_or_tampered_run_rejected(tmp_path, data):
    result = run(tmp_path, data)
    data["transactions"].loc[0, "amount"] = "changed"
    with pytest.raises(ValueError, match="collision"):
        run(tmp_path, data)
    (result["run_dir"] / "curated/transactions.csv").write_text("corrupt")
    data["transactions"].loc[0, "amount"] = read_csv(
        result["run_dir"] / "raw/transactions.csv")[0]["amount"]
    with pytest.raises(ValueError, match="integrity"):
        run(tmp_path, data)


def test_snapshot_times_and_path_safety(tmp_path, data):
    source = tmp_path / "input"
    source.mkdir()
    with pytest.raises(ValueError, match="at or after"):
        ingest(source, source_id="test", source_time=SOURCE_TIME,
               ingestion_time="2025-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="run_id"):
        run(tmp_path, data, "../escape")
    assert not (tmp_path / "escape").exists()


def test_cli_m1_m2_and_legacy_compatibility(tmp_path, data, monkeypatch, capsys):
    run(tmp_path, data)
    from financial_crime_ml.ingestion.foundation import cli as entry

    actual = entry.ingest
    monkeypatch.setattr(entry, "ingest", lambda **kw: actual(**kw, runs_root=tmp_path / "runs"))
    cli.main(["ingest-financial-data", "--dataset-dir", str(tmp_path / "input"),
              "--source-id", "synthetic-v2", "--source-time", SOURCE_TIME,
              "--ingestion-time", INGESTION_TIME, "--run-id", "cli"])
    assert "completed" in capsys.readouterr().out
    data["evaluation_transactions"].to_csv(tmp_path / "evaluation.csv", index=False)
    metrics = run_fraud_evaluation(tmp_path / "evaluation.csv", run_id="m1",
                                   runs_root=tmp_path / "runs")
    assert metrics["manifest"]["test_evaluations"] == 1
    assert metrics["metrics"]["threshold_source"] == "validation"
    monkeypatch.setattr(cli, "generate_all_datasets", lambda: {})
    cli.main(["generate-data"])


@pytest.mark.parametrize("timestamp", ["2025-01-01T00:00:00+00:00",
                                        "2025-01-01T00:00:00.001Z",
                                        "2025-02-30T00:00:00Z"])
def test_timestamp_lexical_rules(data, timestamp):
    data["transactions"].loc[0, "transaction_timestamp"] = timestamp
    _, rejects, _ = transform(encode(data), SOURCE_TIME)
    assert "schema.transaction_timestamp" in rejects["transactions"][0]["reasons"]


def test_currency_is_not_silently_converted_and_jpy_precision(data):
    from financial_crime_ml.data_generation.v2.contracts import format_amount

    old_currency = data["transactions"].loc[0, "currency"]
    new_currency = "JPY" if old_currency != "JPY" else "GBP"
    data["transactions"].loc[0, "currency"] = new_currency
    data["transactions"].loc[0, "amount"] = format_amount(
        int(data["transactions"].loc[0, "amount_minor"]), new_currency)
    _, rejects, _ = transform(encode(data), SOURCE_TIME)
    assert "consistency.currency" in rejects["transactions"][0]["reasons"]
    config = SyntheticConfig(customers=1, accounts=1, beneficiaries=1, devices=1,
                             transactions=10, currencies=("JPY",))
    frames = {name: frame.astype(str) for name, frame in generate_v2(config).items()}
    accepted, _, _ = transform(encode(frames), SOURCE_TIME)
    assert len(accepted["transactions"]) == 10
    frames["transactions"].loc[0, "amount"] += ".00"
    _, rejects, _ = transform(encode(frames), SOURCE_TIME)
    assert "money.precision" in rejects["transactions"][0]["reasons"]


def test_new_device_cannot_be_referenced_before_first_seen(data):
    device = data["transactions"].loc[0, "device_id"]
    data["devices"].loc[data["devices"]["device_id"] == device, "first_seen_at"] = (
        "2025-12-31T23:59:59Z"
    )
    _, rejects, _ = transform(encode(data), SOURCE_TIME)
    assert "time.before_devices" in rejects["transactions"][0]["reasons"]


def test_rejected_header_values_and_money_are_not_fabricated(data):
    contents = encode(data)
    contents["transactions"] = b"unknown\n123\n"
    _, rejects, totals = transform(contents, SOURCE_TIME)
    assert rejects["transactions"][0]["values"] == ["123"]
    assert totals["transactions"]["source_money"] == {
        "amount_minor_by_currency": {}, "unaccountable_rows": 1,
    }


def test_historical_sample_output_protection(tmp_path):
    from financial_crime_ml.ingestion.load_data import REPO_ROOT

    with pytest.raises(ValueError, match="historical samples"):
        ingest(tmp_path, source_id="test", source_time=SOURCE_TIME,
               ingestion_time=INGESTION_TIME, runs_root=REPO_ROOT / "outputs/sample")
