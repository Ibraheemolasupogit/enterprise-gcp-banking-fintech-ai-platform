"""Offline migration parity, exact integers, ordering, malformed values and provenance."""

import csv
import io
import json
import shutil

import pytest

from financial_crime_ml.analytics.contracts import GRAINS
from financial_crime_ml.analytics.workflow import build_marts
from financial_crime_ml.data_generation.v2.contracts import SyntheticConfig
from financial_crime_ml.data_generation.v2.generator import generate_v2
from financial_crime_ml.deployment import reconcile as reconciliation
from financial_crime_ml.deployment.reconcile import main, normalize, reconcile
from financial_crime_ml.ingestion.foundation.pipeline import ingest


@pytest.fixture
def runs(tmp_path):
    frames = generate_v2(SyntheticConfig(customers=4, accounts=6, beneficiaries=12,
                                        devices=8, transactions=100))
    inputs = tmp_path / "input"
    inputs.mkdir()
    for name, frame in frames.items():
        frame.to_csv(inputs / f"{name}.csv", index=False)
    source = ingest(inputs, source_id="m5-fixture", source_time="2026-01-01T00:00:00Z",
                    ingestion_time="2026-01-02T00:00:00Z", run_id="source", runs_root=tmp_path)
    reference = build_marts(source["run_dir"], "reference", runs_root=tmp_path)
    exports = tmp_path / "exports"
    exports.mkdir()
    for name, relative in {"curated_transactions": "curated/transactions.csv",
                           "customers": "validated/customers.csv",
                           "accounts": "validated/accounts.csv"}.items():
        shutil.copyfile(source["run_dir"] / relative, exports / f"{name}.csv")
    for name in GRAINS:
        shutil.copyfile(reference["run_dir"] / f"{name}.csv", exports / f"{name}.csv")
    return source["run_dir"], reference["run_dir"], exports


def change(path, mutate):
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fields, rows = reader.fieldnames, list(reader)
    mutate(rows)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def test_all_seven_exports_match_with_different_order_and_timestamp_format(runs):
    source, reference, exports = runs
    for path in exports.glob("*.csv"):
        def mutate(rows):
            rows.reverse()
            for row in rows:
                for field, value in row.items():
                    if value.endswith("Z"):
                        row[field] = value.replace("T", " ").removesuffix("Z") + ".000000 UTC"
        change(path, mutate)
    report = reconcile(source, reference, exports)
    assert report["status"] == "matched"
    assert report["cloud_execution_verified"] is False
    assert len(report["tables"]) == 7
    totals = report["tables"]["curated_transactions"]["count_and_amount_minor_by_currency"]
    assert sum(count for count, _ in totals.values()) == 100
    for name in GRAINS:
        assert report["tables"][name]["count_and_amount_minor_by_currency"] == totals
        assert report["tables"][name]["row_count"] > 0


@pytest.mark.parametrize("value", [str(2**53 + 1), str(2**63 - 1), str(-(2**63)), "0"])
def test_int64_never_rounds_through_float(value):
    assert normalize(value, "INT64") == int(value)


@pytest.mark.parametrize("value,kind", [
    (str(2**63), "INT64"), (str(-2**63 - 1), "INT64"), ("1.0", "INT64"),
    ("1e3", "INT64"), ("NaN", "INT64"), ("", "STRING"), (None, "INT64"),
    ("2025-02-30", "DATE"), ("20250101", "DATE"),
    ("2025-01-01T00:00:00", "TIMESTAMP"), ("2025-01-01T00:00:00.000001Z", "TIMESTAMP"),
    ("2025-01-01T00:00:00.0000001Z", "TIMESTAMP"),
    ("2025-01-01T00:00:00+25:00", "TIMESTAMP"), ("1", "FLOAT64"),
])
def test_invalid_or_lossy_values_fail(value, kind):
    with pytest.raises(ValueError):
        normalize(value, kind)


def test_timestamp_offsets_compare_as_utc_instants():
    assert normalize("2025-01-01T01:00:00+01:00", "TIMESTAMP") == normalize(
        "2025-01-01T00:00:00Z", "TIMESTAMP")


@pytest.mark.parametrize("table,field", [
    ("curated_transactions", "amount_minor"), ("customers", "country"),
    ("accounts", "customer_id"), ("transaction_daily", "active_account_count"),
    ("customer_daily", "customer_country"), ("account_daily", "last_event_time"),
    ("operations_daily", "total_amount_minor"),
])
def test_any_source_or_mart_value_mismatch_fails(runs, table, field):
    source, reference, exports = runs
    change(exports / f"{table}.csv", lambda rows: rows[0].update({field: "999"}))
    with pytest.raises(ValueError):
        reconcile(source, reference, exports)


def test_equal_currency_totals_do_not_hide_wrong_grain_values(runs):
    source, reference, exports = runs

    def mutate(rows):
        first = rows[0]
        second = next(row for row in rows[1:] if row["currency"] == first["currency"])
        first["total_amount_minor"] = str(int(first["total_amount_minor"]) + 1)
        second["total_amount_minor"] = str(int(second["total_amount_minor"]) - 1)

    change(exports / "transaction_daily.csv", mutate)
    with pytest.raises(ValueError, match="Row/value reconciliation failed"):
        reconcile(source, reference, exports)


@pytest.mark.parametrize("defect", ["duplicate", "missing", "extra", "null", "field_count"])
def test_malformed_export_or_inventory_fails(runs, defect):
    source, reference, exports = runs
    path = exports / "accounts.csv"
    if defect == "duplicate":
        change(path, lambda rows: rows.append(dict(rows[0])))
    elif defect == "missing":
        path.unlink()
    elif defect == "extra":
        (exports / "unexpected.csv").write_text("x\n")
    elif defect == "null":
        change(path, lambda rows: rows[0].update(customer_id=""))
    else:
        path.write_text(path.read_text() + "too,few,fields\n")
    with pytest.raises(ValueError):
        reconcile(source, reference, exports)


@pytest.mark.parametrize("defect", ["tamper", "source", "traversal", "failed"])
def test_reference_integrity_and_source_binding_fail_closed(runs, defect):
    source, reference, exports = runs
    path = reference / "manifest.json"
    manifest = json.loads(path.read_text())
    if defect == "tamper":
        (reference / "account_daily.csv").write_text("changed")
    elif defect == "source":
        manifest["source_manifest_sha256"] = "0" * 64
    elif defect == "traversal":
        manifest["artifacts_sha256"]["../outside"] = "0" * 64
    else:
        manifest["status"] = "failed"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        reconcile(source, reference, exports)


def test_empty_snapshot_exports_match_without_inventing_zero_rows(runs, tmp_path):
    source, _, exports = runs
    inputs = tmp_path / "empty-input"
    inputs.mkdir()
    for path in (source / "raw").glob("*.csv"):
        (inputs / path.name).write_text(path.read_text().splitlines()[0] + "\n")
    empty = ingest(inputs, source_id="empty", source_time="2026-01-01T00:00:00Z",
                   ingestion_time="2026-01-02T00:00:00Z", run_id="empty", runs_root=tmp_path)
    reference = build_marts(empty["run_dir"], "empty-marts", runs_root=tmp_path)
    for path in exports.glob("*.csv"):
        path.write_text(path.read_text().splitlines()[0] + "\n")
    report = reconcile(empty["run_dir"], reference["run_dir"], exports)
    assert all(table["row_count"] == 0 for table in report["tables"].values())
    assert report["tables"]["transaction_daily"]["count_and_amount_minor_by_currency"] == {}


def test_cli_returns_report_and_nonzero_on_mismatch(runs, capsys):
    source, reference, exports = runs
    args = ["--source-run", str(source), "--reference-run", str(reference),
            "--export-dir", str(exports)]
    main(args)
    assert json.loads(capsys.readouterr().out)["status"] == "matched"
    (exports / "accounts.csv").write_text("bad\n")
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 1
    assert "Reconciliation rejected" in capsys.readouterr().err


@pytest.mark.parametrize("limit", ["max_transactions", "max_stored_bytes_all_versions"])
def test_local_volume_guards_fail_closed(runs, tmp_path, monkeypatch, limit):
    root = tmp_path / "policy"
    (root / "infrastructure/schemas/v1").mkdir(parents=True)
    original = reconciliation.REPO_ROOT / "infrastructure"
    controls = json.loads((original / "pilot_controls.json").read_text())
    controls[limit] = 1
    (root / "infrastructure/pilot_controls.json").write_text(json.dumps(controls))
    shutil.copyfile(original / "schemas/v1/source_tables.json",
                    root / "infrastructure/schemas/v1/source_tables.json")
    monkeypatch.setattr(reconciliation, "REPO_ROOT", root)
    with pytest.raises(ValueError, match="volume exceeds pilot limit"):
        reconcile(*runs)


def test_export_symlinks_cannot_escape_acquisition_directory(runs):
    source, reference, exports = runs
    path = exports / "accounts.csv"
    path.unlink()
    path.symlink_to(source / "validated/accounts.csv")
    with pytest.raises(ValueError, match="Unsafe export path"):
        reconcile(source, reference, exports)


def test_normalization_does_not_strip_or_coerce_strings():
    assert normalize("001.00", "STRING") == "001.00"
    assert normalize(" GBP ", "STRING") == " GBP "
    # Actual CSV NULL/empty is rejected independently of the string literal NULL.
    assert normalize("NULL", "STRING") == "NULL"
    assert list(csv.DictReader(io.StringIO("a,b\n,1\n")))[0]["a"] == ""
