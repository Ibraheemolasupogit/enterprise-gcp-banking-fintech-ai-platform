"""M7B offline ingestion packet: determinism, tamper evidence, limits, paths and load specs."""

from __future__ import annotations

import csv
import io
import json
import shutil
from pathlib import Path

import pytest
import yaml

from financial_crime_ml.cli import main as cli_main
from financial_crime_ml.data_generation.v2.contracts import ENUMS, SyntheticConfig
from financial_crime_ml.data_generation.v2.generator import generate_v2
from financial_crime_ml.data_generation.v2.workflow import config_from_mapping
from financial_crime_ml.deployment import ingestion_packet as packet_module
from financial_crime_ml.deployment.ingestion_packet import (
    CONTROLS_PATH,
    LOAD_SETTINGS,
    SCHEMA_PATH,
    PacketError,
    analyse_tables,
    build_packet,
    canonical_json,
    load_schemas,
    verify_packet,
)
from financial_crime_ml.evaluation.manifest import sha256_bytes
from financial_crime_ml.ingestion.foundation.pipeline import ingest
from financial_crime_ml.ingestion.load_data import REPO_ROOT

TARGET = {"project_id": "ebank-pilot-dev-261031-k7m4", "dataset_id": "bankpilot_dev"}
SNAP = {"source_time": "2026-01-01T00:00:00Z", "ingestion_time": "2026-01-02T00:00:00Z"}


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    base = tmp_path_factory.mktemp("m7b")
    frames = generate_v2(SyntheticConfig(customers=4, accounts=6, beneficiaries=12,
                                        devices=8, transactions=100))
    inputs = base / "input"
    inputs.mkdir()
    for name, frame in frames.items():
        frame.to_csv(inputs / f"{name}.csv", index=False, lineterminator="\n")
    run = ingest(inputs, source_id="m7b-fixture", run_id="m3", runs_root=base / "runs", **SNAP)
    assert run["manifest"]["status"] == "completed"
    return {"base": base, "inputs": inputs, "run": run["run_dir"], "m3": run["manifest"]}


@pytest.fixture
def built(source, tmp_path):
    result = build_packet(source["run"], output_root=tmp_path / "packets", **TARGET)
    return result


def read_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(path.read_text(), newline="")))
    return rows[0], rows[1:]


def write_rows(path: Path, header, rows) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    path.write_text(buffer.getvalue())


def edit_field(path: Path, field: str, value: str, row: int = 0) -> None:
    header, rows = read_rows(path)
    rows[row][header.index(field)] = value
    write_rows(path, header, rows)


def rewrite_manifest(packet: Path, mutate) -> None:
    manifest = json.loads((packet / "manifest.json").read_bytes())
    mutate(manifest)
    content = canonical_json(manifest)
    (packet / "manifest.json").write_bytes(content)
    (packet / "manifest.sha256").write_text(f"{sha256_bytes(content)}  manifest.json\n")


def data(packet: Path, table: str) -> Path:
    return packet / "data" / f"{table}.csv"


# ------------------------------------------------------------------ determinism and contents
def test_independent_builds_are_byte_identical(source, tmp_path):
    first = build_packet(source["run"], output_root=tmp_path / "a", **TARGET)
    second = build_packet(source["run"], output_root=tmp_path / "b", **TARGET)
    assert first["packet_id"] == second["packet_id"]
    assert first["manifest_sha256"] == second["manifest_sha256"]
    files = sorted(p.relative_to(first["packet_dir"]) for p in first["packet_dir"].rglob("*"))
    assert files == sorted(p.relative_to(second["packet_dir"])
                           for p in second["packet_dir"].rglob("*"))
    for relative in files:
        if (first["packet_dir"] / relative).is_file():
            assert ((first["packet_dir"] / relative).read_bytes()
                    == (second["packet_dir"] / relative).read_bytes())


def test_independent_m3_runs_of_same_snapshot_share_packet_identity(source, tmp_path):
    rerun = ingest(source["inputs"], source_id="m7b-fixture", run_id="m3-again",
                   runs_root=tmp_path / "runs", **SNAP)
    first = build_packet(source["run"], output_root=tmp_path / "a", **TARGET)
    second = build_packet(rerun["run_dir"], output_root=tmp_path / "b", **TARGET)
    assert first["packet_id"] == second["packet_id"]
    assert first["manifest"]["identity"] == second["manifest"]["identity"]
    assert first["manifest"]["load_jobs"] == second["manifest"]["load_jobs"]
    # M3 run provenance is a local record only, so the public packet hash is identical.
    assert first["provenance"] != second["provenance"]
    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert "provenance" not in first["manifest"]


def test_packet_preserves_source_bytes_columns_and_reconciles_with_m3(built, source):
    manifest, packet = built["manifest"], built["packet_dir"]
    schemas, _ = load_schemas()
    for table, entry in manifest["tables"].items():
        content = (packet / entry["file"]).read_bytes()
        assert content == (source["run"] / entry["source_artifact"]).read_bytes()
        assert entry["sha256"] == source["m3"]["artifacts_sha256"][entry["source_artifact"]]
        assert entry["size_bytes"] == len(content)
        assert entry["columns"] == [field["name"] for field in schemas[table]]
        assert entry["row_count"] == content.count(b"\n") - 1
    reconciliation = json.loads((source["run"] / "reconciliation.json").read_text())
    m3_money = reconciliation["transactions"]["curated_money"]["amount_minor_by_currency"]
    money = manifest["reconciliation"]["amount_minor_by_currency"]
    assert {c: v["amount_minor"] for c, v in money.items()} == m3_money
    assert sum(v["row_count"] for v in money.values()) == 100
    assert all(type(v["amount_minor"]) is int for v in money.values())
    assert money.get("JPY", {"exponent": 0})["exponent"] == 0


def test_md5_is_recorded_separately_from_sha256(built):
    import base64
    import hashlib
    for entry in built["manifest"]["tables"].values():
        content = (built["packet_dir"] / entry["file"]).read_bytes()
        assert entry["md5_hex"] == hashlib.md5(content).hexdigest()
        assert base64.b64decode(entry["md5_base64"]).hex() == entry["md5_hex"]
        assert entry["sha256"] != entry["md5_hex"] and len(entry["sha256"]) == 64
    assert "not interchangeable" in built["manifest"]["checksum_note"]


def test_manifest_is_canonical_and_free_of_machine_metadata(built, source):
    raw = (built["packet_dir"] / "manifest.json").read_bytes()
    assert canonical_json(json.loads(raw)) == raw
    text = raw.decode()
    for leak in (str(source["base"]), str(Path.home()), "/Users/", "/tmp", "git_",
                 "started_at", "finished_at", "source_directory", "\"versions\""):
        assert leak not in text
    assert "m3_run_id" not in text and "m3_manifest" not in text
    assert "provenance" not in built["manifest"]
    sidecar = (built["packet_dir"] / "manifest.sha256").read_text()
    assert sidecar == f"{sha256_bytes(raw)}  manifest.json\n" == \
        f"{built['manifest_sha256']}  manifest.json\n"


def test_object_names_and_job_ids_are_unique_stable_and_strict(built):
    manifest = built["manifest"]
    packet_id = manifest["packet_id"]
    short = packet_id.removeprefix("m7p-")
    assert manifest["identity_sha256"].startswith(short)
    names = manifest["objects"]["names"]
    assert manifest["objects"]["bucket"] == "bound_at_execution_from_custody_inputs"
    assert manifest["objects"]["precondition"] == "ifGenerationMatch=0"
    assert len(set(names.values())) == 3
    schemas, _ = load_schemas()
    jobs = manifest["load_jobs"]
    assert len({spec["job_id"] for spec in jobs.values()}) == 3
    for table, spec in jobs.items():
        assert names[table] == f"m7/ingestion/{packet_id}/{table}.csv"
        assert spec["job_id"] == f"m7_load_{short}_{table}"
        assert spec["location"] == "europe-west2"
        load = spec["configuration"]["load"]
        assert {k: load[k] for k in LOAD_SETTINGS} == LOAD_SETTINGS
        assert load["sourceFormat"] == "CSV" and load["skipLeadingRows"] == 1
        assert load["autodetect"] is False and load["maxBadRecords"] == 0
        assert load["createDisposition"] == "CREATE_NEVER"
        assert load["writeDisposition"] == "WRITE_EMPTY"
        assert load["schema"] == {"fields": schemas[table]}
        assert load["destinationTable"] == {"projectId": TARGET["project_id"],
                                            "datasetId": TARGET["dataset_id"], "tableId": table}
        assert load["sourceUriTemplate"] == "gs://{bucket}/" + names[table]
    assert manifest["limits"] == {"load_jobs": 3, "query_bytes_billed": 0,
                                  "total_upload_bytes": sum(e["size_bytes"] for e in
                                                            manifest["tables"].values()),
                                  "transactions": 100}


def test_verify_accepts_packet_and_pinned_hash(built):
    result = verify_packet(built["packet_dir"], expected_manifest_sha256=built["manifest_sha256"])
    assert result["packet_id"] == built["packet_id"]


def test_m7_pilot_config_is_4000_transactions_and_defaults_are_unchanged():
    pilot = yaml.safe_load((REPO_ROOT / "configs/synthetic_v2_m7_pilot.yaml").read_text())
    default = yaml.safe_load((REPO_ROOT / "configs/synthetic_v2.yaml").read_text())
    assert default["transactions"] == 5000
    assert pilot["transactions"] == 4000
    assert {k: v for k, v in pilot.items() if k != "transactions"} == \
        {k: v for k, v in default.items() if k != "transactions"}
    assert config_from_mapping(pilot).transactions == 4000


# ------------------------------------------------------------------ tamper evidence
def test_byte_tampering_with_valid_content_is_rejected(built):
    path = data(built["packet_dir"], "curated_transactions")
    header, rows = read_rows(path)
    current = rows[0][header.index("channel")]
    other = next(value for value in ENUMS["channel"] if value != current)
    edit_field(path, "channel", other)
    with pytest.raises(PacketError, match="do not match the manifest"):
        verify_packet(built["packet_dir"])


@pytest.mark.parametrize("mutate,message", [
    (lambda m: m["reconciliation"]["row_counts"].update(customers=999), "recomputation"),
    (lambda m: m["tables"]["customers"].update(sha256="0" * 64), "do not match the manifest"),
    (lambda m: m["tables"]["accounts"].update(md5_base64="AAAA"), "do not match the manifest"),
    (lambda m: m["limits"].update(transactions=1), "recomputation"),
    (lambda m: m["objects"].update(precondition="none"), "recomputation"),
    (lambda m: m.update(extra="x"), "recomputation"),
    (lambda m: m.update(provenance={"path": "/Users/x"}), "recomputation"),
    (lambda m: m.pop("identity"), "identity missing"),
    (lambda m: m["identity"]["source_snapshot"].update(source_id="other"), "lineage source_id"),
    (lambda m: m["identity"]["controls"].update(max_transactions=10**6), "recomputation"),
])
def test_manifest_tampering_with_consistent_sidecar_is_rejected(built, mutate, message):
    rewrite_manifest(built["packet_dir"], mutate)
    with pytest.raises(PacketError, match=message):
        verify_packet(built["packet_dir"])


def test_manifest_hash_sidecar_canonical_form_and_pin(built):
    packet, manifest = built["packet_dir"], built["packet_dir"] / "manifest.json"
    with pytest.raises(PacketError, match="approved value"):
        verify_packet(packet, expected_manifest_sha256="0" * 64)
    original = manifest.read_bytes()
    manifest.write_bytes(original.replace(b'"classification":"synthetic"',
                                          b'"classification":"synthetiC"'))
    with pytest.raises(PacketError, match="sidecar"):
        verify_packet(packet)
    pretty = (json.dumps(json.loads(original), indent=2) + "\n").encode()
    manifest.write_bytes(pretty)
    (packet / "manifest.sha256").write_text(f"{sha256_bytes(pretty)}  manifest.json\n")
    with pytest.raises(PacketError, match="canonical"):
        verify_packet(packet)
    manifest.write_bytes(b"{not json")
    (packet / "manifest.sha256").write_text(f"{sha256_bytes(b'{not json')}  manifest.json\n")
    with pytest.raises(PacketError, match="not JSON"):
        verify_packet(packet)


@pytest.mark.parametrize("mutate,message", [
    (lambda m: m.update(packet_id="m7p-" + "0" * 24), "directory name"),
    (lambda m: m.update(packet_id="../escape"), "invalid packet_id"),
    (lambda m: m.update(identity_sha256="f" * 64), "recomputation"),
    (lambda m: m["identity"].update(classification="production"), "recomputation"),
    (lambda m: m["identity"]["target"].update(project_id="other-project-123"),
     "destination differs"),
    (lambda m: m["identity"]["target"].update(location="us"), "location"),
    (lambda m: m["identity"].update(packet_version="9.9.9"), "recomputation"),
])
def test_altered_packet_identity_is_rejected(built, mutate, message):
    rewrite_manifest(built["packet_dir"], mutate)
    with pytest.raises(PacketError, match=message):
        verify_packet(built["packet_dir"])


def test_renamed_packet_with_matching_fake_id_is_rejected(built, tmp_path):
    fake = "m7p-" + "1" * 24
    moved = built["packet_dir"].rename(built["packet_dir"].parent / fake)
    rewrite_manifest(moved, lambda m: m.update(packet_id=fake))
    with pytest.raises(PacketError, match="recomputation"):
        verify_packet(moved)


# ------------------------------------------------------------------ schema and leakage
def test_missing_additional_and_reordered_columns_are_rejected(built, tmp_path):
    original = data(built["packet_dir"], "accounts").read_bytes()
    header, rows = read_rows(data(built["packet_dir"], "accounts"))
    cases = {
        "missing": ([h for h in header if h != "base_currency"],
                    [[v for h, v in zip(header, r, strict=True) if h != "base_currency"]
                     for r in rows]),
        "unknown": ([*header, "branch"], [[*r, "x"] for r in rows]),
        "column order": ([header[1], header[0], *header[2:]],
                         [[r[1], r[0], *r[2:]] for r in rows]),
    }
    for message, (new_header, new_rows) in cases.items():
        write_rows(data(built["packet_dir"], "accounts"), new_header, new_rows)
        with pytest.raises(PacketError, match=message):
            verify_packet(built["packet_dir"])
        data(built["packet_dir"], "accounts").write_bytes(original)
    verify_packet(built["packet_dir"])


@pytest.mark.parametrize("column", ["is_suspicious", "scenario_type", "episode_id",
                                    "fraud_score", "label"])
def test_outcome_and_label_columns_are_rejected(built, column):
    path = data(built["packet_dir"], "curated_transactions")
    header, rows = read_rows(path)
    write_rows(path, [*header, column], [[*r, "1"] for r in rows])
    with pytest.raises(PacketError, match="forbidden outcome/label"):
        verify_packet(built["packet_dir"])


def test_schema_file_with_label_or_drift_is_rejected(tmp_path):
    schemas = json.loads(SCHEMA_PATH.read_text())
    leaked = json.loads(json.dumps(schemas))
    leaked["curated_transactions"].append({"name": "is_suspicious", "type": "INT64",
                                           "mode": "REQUIRED"})
    (tmp_path / "leak.json").write_text(json.dumps(leaked))
    with pytest.raises(PacketError, match="forbidden"):
        load_schemas(tmp_path / "leak.json")
    drift = json.loads(json.dumps(schemas))
    drift["customers"].reverse()
    (tmp_path / "drift.json").write_text(json.dumps(drift))
    with pytest.raises(PacketError, match="differ from the M3 contract"):
        load_schemas(tmp_path / "drift.json")
    nullable = json.loads(json.dumps(schemas))
    nullable["accounts"][0]["mode"] = "NULLABLE"
    (tmp_path / "nullable.json").write_text(json.dumps(nullable))
    with pytest.raises(PacketError, match="unsupported type or mode"):
        load_schemas(tmp_path / "nullable.json")


@pytest.mark.parametrize("table,field,value,message", [
    ("accounts", "customer_id", "C2-99999999", "reference.customer_id"),
    ("curated_transactions", "account_id", "A2-99999999", "reference"),
    ("curated_transactions", "source_id", "other-source", "lineage source_id"),
    ("curated_transactions", "source_sha256", "0" * 64, "lineage source_sha256"),
    ("customers", "ingestion_time", "2026-01-03T00:00:00Z", "lineage ingestion_time"),
    ("accounts", "source_row", "0", "positive INT64"),
    ("customers", "onboarded_at", "2026-01-01 00:00:00", "Timestamp"),
    ("curated_transactions", "customer_country", "ZZ", "customer_country"),
    ("curated_transactions", "currency", "SGD", "currency|money|consistency"),
    ("customers", "country", "", "empty REQUIRED"),
])
def test_invalid_values_foreign_keys_and_lineage_are_rejected(built, table, field, value,
                                                              message):
    edit_field(data(built["packet_dir"], table), field, value)
    with pytest.raises(PacketError, match=message):
        verify_packet(built["packet_dir"])


def test_transaction_customer_must_own_the_account(built):
    path = data(built["packet_dir"], "curated_transactions")
    header, rows = read_rows(path)
    customer = rows[0][header.index("customer_id")]
    other = next(r[header.index("customer_id")] for r in rows
                 if r[header.index("customer_id")] != customer)
    edit_field(path, "customer_id", other)
    with pytest.raises(PacketError, match="ownership"):
        verify_packet(built["packet_dir"])


@pytest.mark.parametrize("corrupt,message", [
    (lambda r: r["transactions"]["curated_money"]["amount_minor_by_currency"].update(
        GBP=-1), "money differs from M3"),
    (lambda r: r["customers"].update(validated_rows=0), "row counts differ from M3"),
    (lambda r: r["transactions"].update(curated_rows=1), "row counts differ from M3"),
])
def test_packet_is_cross_checked_against_m3_reconciliation(source, tmp_path, monkeypatch,
                                                           corrupt, message):
    real = packet_module.verified_source

    def disagreeing(directory):
        manifest, validated, payload, manifest_bytes = real(directory)
        reconciliation = json.loads(json.dumps(payload["reconciliation"]))
        corrupt(reconciliation)
        return manifest, validated, {**payload, "reconciliation": reconciliation}, manifest_bytes

    monkeypatch.setattr(packet_module, "verified_source", disagreeing)
    with pytest.raises(PacketError, match=message):
        build_packet(source["run"], output_root=tmp_path / "p", **TARGET)
    assert not (tmp_path / "p").exists()


def test_duplicate_keys_and_reordered_lineage_rows_are_rejected(built):
    path = data(built["packet_dir"], "customers")
    header, rows = read_rows(path)
    write_rows(path, header, [rows[1], rows[0], *rows[2:]])
    with pytest.raises(PacketError, match="source_row must strictly increase"):
        verify_packet(built["packet_dir"])
    duplicate = list(rows[1])
    duplicate[header.index("customer_id")] = rows[0][header.index("customer_id")]
    write_rows(path, header, [rows[0], duplicate, *rows[2:]])
    with pytest.raises(PacketError, match="duplicate primary key"):
        verify_packet(built["packet_dir"])


@pytest.mark.parametrize("currency,minor,amount,ok", [
    ("GBP", "1234", "12.34", True),
    ("GBP", "1234", "12.3", False),
    ("GBP", "1234", "12.340", False),
    ("JPY", "1234", "1234", True),
    ("JPY", "1234", "12.34", False),
    ("EUR", "1000000001", "10000000.01", False),  # above MAX_AMOUNT_MINOR
])
def test_currency_precision_is_exact(source, currency, minor, amount, ok):
    contents = {t: (source["run"] / a).read_bytes()
                for t, (a, _, _) in packet_module.TABLES.items()}
    header, rows = read_rows(source["run"] / "curated/transactions.csv")
    accounts_header, accounts = read_rows(source["run"] / "validated/accounts.csv")
    account = next(a for a in accounts if a[accounts_header.index("base_currency")] == currency) \
        if any(a[accounts_header.index("base_currency")] == currency for a in accounts) else None
    if account is None:
        pytest.skip(f"fixture has no {currency} account")
    row = next(r for r in rows if r[header.index("account_id")] == account[0])
    row[header.index("amount_minor")], row[header.index("amount")] = minor, amount
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows([header, *rows])
    contents["curated_transactions"] = buffer.getvalue().encode()
    m3 = source["m3"]
    snapshot = {"source_id": m3["source_id"], "source_time": m3["source_time"],
                "ingestion_time": m3["ingestion_time"],
                "raw_source_sha256": {f: m3["input_sha256"][f]
                                      for f in ("customers.csv", "accounts.csv",
                                                "transactions.csv")}}
    schemas, _ = load_schemas()
    if ok:
        facts = analyse_tables(contents, schemas, snapshot)
        assert type(facts["money"][currency]["amount_minor"]) is int
    else:
        with pytest.raises(PacketError, match=r"money\.precision|schema\.amount_minor"):
            analyse_tables(contents, schemas, snapshot)


def test_csv_encoding_rules_are_enforced(built):
    path = data(built["packet_dir"], "customers")
    original = path.read_bytes()
    for content, message in [
        (b"\xef\xbb\xbf" + original, "byte-order mark"),
        (original.replace(b"\n", b"\r\n"), "carriage returns"),
        (original.rstrip(b"\n"), "LF-terminated"),
        (original.split(b"\n")[0] + b"\n", "no data rows"),
        (original.replace(b"C2-", b"\xff2-", 1), "UTF-8"),
    ]:
        path.write_bytes(content)
        with pytest.raises(PacketError, match=message):
            verify_packet(built["packet_dir"])
    path.write_bytes(original)
    header, rows = read_rows(path)
    rows[0][header.index("source_id")] = "multi\nline"
    write_rows(path, header, rows)
    with pytest.raises(PacketError, match="quoted newlines"):
        verify_packet(built["packet_dir"])


# ------------------------------------------------------------------ limits
def write_controls(tmp_path: Path, **changes) -> Path:
    controls = json.loads(CONTROLS_PATH.read_text())
    controls.update(changes)
    path = tmp_path / "controls.json"
    path.write_text(json.dumps(controls))
    return path


@pytest.mark.parametrize("changes,message", [
    ({"max_transactions": 99}, "exceed max_transactions"),
    ({"max_total_upload_bytes": 1000}, "max_total_upload_bytes"),
    ({"max_stored_bytes_all_versions": 1000}, "max_stored_bytes_all_versions"),
    ({"max_jobs_per_approved_execution": 2}, "max_jobs_per_approved_execution"),
    ({"max_transactions": "5000"}, "positive integer"),
    ({"pilot_hours": 0}, "positive integer"),
    ({"budget_is_spending_cap": True}, "spending cap"),
])
def test_transaction_size_and_cost_limits_are_enforced(source, tmp_path, changes, message):
    with pytest.raises(PacketError, match=message):
        build_packet(source["run"], output_root=tmp_path / "p", **TARGET,
                     controls_path=write_controls(tmp_path, **changes))
    assert not (tmp_path / "p").exists() or not any((tmp_path / "p").iterdir())


def test_tightened_controls_invalidate_an_existing_packet(built, tmp_path):
    with pytest.raises(PacketError, match="exceed max_transactions"):
        verify_packet(built["packet_dir"],
                      controls_path=write_controls(tmp_path, max_transactions=50))
    with pytest.raises(PacketError, match="recomputation"):
        verify_packet(built["packet_dir"],
                      controls_path=write_controls(tmp_path, max_transactions=4999))


@pytest.mark.parametrize("kwargs,message", [
    ({"location": "us-central1"}, "location"),
    ({"location": "EU"}, "location"),
    ({"project_id": "Bad_Project"}, "project_id"),
    ({"dataset_id": "bad-dataset"}, "dataset_id"),
])
def test_invalid_targets_are_rejected(source, tmp_path, kwargs, message):
    with pytest.raises(PacketError, match=message):
        build_packet(source["run"], output_root=tmp_path, **{**TARGET, **kwargs})


# ------------------------------------------------------------------ paths and files
@pytest.mark.parametrize("protected", ["data/sample", "outputs/sample", "reports/sample",
                                       "infrastructure", "src", "docs"])
def test_protected_output_roots_are_refused(source, protected, tmp_path, monkeypatch):
    # Sandbox the repository root so a broken guard can never write into the real checkout.
    fake_repo = tmp_path / "repo"
    monkeypatch.setattr(packet_module, "REPO_ROOT", fake_repo)
    with pytest.raises(PacketError, match="cannot be written under"):
        build_packet(source["run"], output_root=fake_repo / protected / "packets", **TARGET)
    assert not (fake_repo / protected / "packets").exists()


def test_protected_roots_are_anchored_to_the_real_repository():
    assert packet_module.REPO_ROOT == REPO_ROOT
    assert {"data", "outputs/sample", "reports/sample"} <= set(packet_module.PROTECTED)


def test_output_root_inside_source_run_is_refused(source):
    with pytest.raises(PacketError, match="inside the source run"):
        build_packet(source["run"], output_root=source["run"] / "packets", **TARGET)


def test_symlinks_missing_extra_files_and_directories_are_rejected(built, tmp_path):
    packet = built["packet_dir"]
    link = tmp_path / "link"
    link.symlink_to(packet, target_is_directory=True)
    with pytest.raises(PacketError, match="symbolic link"):
        verify_packet(link)
    (packet / "notes.txt").write_text("x")
    with pytest.raises(PacketError, match="unexpected"):
        verify_packet(packet)
    (packet / "notes.txt").unlink()
    (packet / "nested").mkdir()
    with pytest.raises(PacketError, match="unexpected directory"):
        verify_packet(packet)
    (packet / "nested").rmdir()
    target = data(packet, "customers")
    saved = tmp_path / "customers.csv"
    shutil.move(target, saved)
    with pytest.raises(PacketError, match="missing"):
        verify_packet(packet)
    target.symlink_to(saved)
    with pytest.raises(PacketError, match="symbolic link"):
        verify_packet(packet)
    target.unlink()
    shutil.move(saved, target)
    verify_packet(packet)


# ------------------------------------------------------------------ overwrite and collisions
def test_rebuild_reuses_identical_packet_without_writing(source, built):
    before = {p: (p.stat().st_mtime_ns, p.read_bytes())
              for p in built["packet_dir"].rglob("*") if p.is_file()}
    again = build_packet(source["run"], output_root=built["packet_dir"].parent, **TARGET)
    assert again["reused"] is True and again["manifest_sha256"] == built["manifest_sha256"]
    assert before == {p: (p.stat().st_mtime_ns, p.read_bytes())
                      for p in built["packet_dir"].rglob("*") if p.is_file()}


def test_conflicting_identity_is_refused(source, built, monkeypatch):
    # A valid packet already holds this ID but with a different manifest (simulated, since a
    # genuine collision needs a 96-bit identity-hash collision).
    real = packet_module.verify_packet

    def other_manifest(*args, **kwargs):
        return {**real(*args, **kwargs), "manifest_sha256": "f" * 64}

    monkeypatch.setattr(packet_module, "verify_packet", other_manifest)
    with pytest.raises(PacketError, match="identity collision"):
        build_packet(source["run"], output_root=built["packet_dir"].parent, **TARGET)
    monkeypatch.undo()
    verify_packet(built["packet_dir"], expected_manifest_sha256=built["manifest_sha256"])


def test_rebuild_from_independent_m3_run_reuses_the_same_packet(source, built, tmp_path):
    rerun = ingest(source["inputs"], source_id="m7b-fixture", run_id="m3-other",
                   runs_root=tmp_path / "runs", **SNAP)
    again = build_packet(rerun["run_dir"], output_root=built["packet_dir"].parent, **TARGET)
    assert again["reused"] is True and again["manifest_sha256"] == built["manifest_sha256"]


def test_existing_corrupt_packet_is_never_overwritten(source, built):
    path = data(built["packet_dir"], "customers")
    corrupted = path.read_bytes() + b"tampered\n"
    path.write_bytes(corrupted)
    with pytest.raises(PacketError):
        build_packet(source["run"], output_root=built["packet_dir"].parent, **TARGET)
    assert path.read_bytes() == corrupted


def test_partial_write_leaves_no_packet_or_staging(source, tmp_path, monkeypatch):
    real = packet_module._write_new

    def failing(path, content):
        if path.name == "manifest.json":
            raise OSError("disk full")
        real(path, content)

    monkeypatch.setattr(packet_module, "_write_new", failing)
    root = tmp_path / "packets"
    with pytest.raises(OSError, match="disk full"):
        build_packet(source["run"], output_root=root, **TARGET)
    assert list(root.iterdir()) == []


def test_leftover_partial_directory_is_rejected_not_completed(source, built):
    packet = built["packet_dir"]
    (packet / "manifest.json").unlink()
    (packet / "manifest.sha256").unlink()
    with pytest.raises(PacketError, match="missing"):
        verify_packet(packet)
    with pytest.raises(PacketError, match="missing"):
        build_packet(source["run"], output_root=packet.parent, **TARGET)
    assert not (packet / "manifest.json").exists()


# ------------------------------------------------------------------ load specifications
@pytest.mark.parametrize("mutate,message", [
    (lambda j: j["accounts"].update(job_id=j["customers"]["job_id"]), "duplicate"),
    (lambda j: j["accounts"].update(job_id="bad id!"), "invalid job ID"),
    (lambda j: j.pop("accounts"), "exactly the three"),
    (lambda j: j["customers"]["configuration"]["load"].update(
        writeDisposition="WRITE_TRUNCATE"), "writeDisposition"),
    (lambda j: j["customers"]["configuration"]["load"].update(
        createDisposition="CREATE_IF_NEEDED"), "createDisposition"),
    (lambda j: j["customers"]["configuration"]["load"].update(autodetect=True), "autodetect"),
    (lambda j: j["customers"]["configuration"]["load"].update(maxBadRecords=1),
     "maxBadRecords"),
    (lambda j: j["customers"]["configuration"]["load"].update(skipLeadingRows=0),
     "skipLeadingRows"),
    (lambda j: j["customers"]["configuration"]["load"].update(
        schemaUpdateOptions=["ALLOW_FIELD_ADDITION"]), "schemaUpdateOptions"),
    (lambda j: j["customers"]["configuration"]["load"].update(
        sourceFormat="NEWLINE_DELIMITED_JSON"), "sourceFormat"),
    (lambda j: j["customers"]["configuration"]["load"]["destinationTable"].update(
        tableId="accounts"), "destination"),
    (lambda j: j["customers"]["configuration"]["load"].update(
        sourceObject="m7/ingestion/other.csv"), "source object"),
    (lambda j: j["customers"].update(location="US"), "location"),
    (lambda j: j["customers"]["configuration"]["load"]["schema"]["fields"].pop(), "recomputation"),
    (lambda j: j["customers"]["configuration"]["load"].update(
        sourceUriTemplate="gs://{bucket}/*"), "recomputation"),
    (lambda j: j["customers"].update(configuration="none"), "malformed"),
])
def test_modified_load_settings_are_rejected(built, mutate, message):
    rewrite_manifest(built["packet_dir"], lambda m: mutate(m["load_jobs"]))
    with pytest.raises(PacketError, match=message):
        verify_packet(built["packet_dir"])


def test_duplicate_object_names_are_rejected(built):
    def mutate(manifest):
        names = manifest["objects"]["names"]
        names["accounts"] = names["customers"]
    rewrite_manifest(built["packet_dir"], mutate)
    with pytest.raises(PacketError, match="duplicate or missing object names"):
        verify_packet(built["packet_dir"])


# ------------------------------------------------------------------ source acceptance
def test_source_run_with_rejects_or_tampering_is_refused(source, tmp_path):
    inputs = tmp_path / "input"
    shutil.copytree(source["inputs"], inputs)
    transactions = inputs / "transactions.csv"
    lines = transactions.read_text().splitlines(keepends=True)
    transactions.write_text("".join([*lines, lines[1]]))  # duplicate key -> quarantine
    rejected = ingest(inputs, source_id="m7b-rejects", run_id="rejects",
                      runs_root=tmp_path / "runs", **SNAP)
    assert rejected["manifest"]["status"] == "completed_with_rejects"
    with pytest.raises(PacketError, match="must be 'completed'"):
        build_packet(rejected["run_dir"], output_root=tmp_path / "p", **TARGET)
    copy = tmp_path / "copy"
    shutil.copytree(source["run"], copy)
    with (copy / "validated/customers.csv").open("a") as handle:
        handle.write("x\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        build_packet(copy, output_root=tmp_path / "p2", **TARGET)


# ------------------------------------------------------------------ CLI
def test_cli_build_and_verify(source, tmp_path, capsys):
    root = tmp_path / "cli"
    cli_main(["build-ingestion-packet", "--source-run", str(source["run"]),
              "--project-id", TARGET["project_id"], "--dataset-id", TARGET["dataset_id"],
              "--output-root", str(root)])
    out = capsys.readouterr().out
    assert "Packet built:" in out and "Manifest SHA-256:" in out
    assert "Local source record (not in packet): M3 run m3" in out
    packet = next(root.iterdir())
    digest = (packet / "manifest.sha256").read_text().split()[0]
    cli_main(["verify-ingestion-packet", "--packet-dir", str(packet),
              "--expected-manifest-sha256", digest])
    assert "Packet verified." in capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_info:
        cli_main(["verify-ingestion-packet", "--packet-dir", str(packet),
                  "--expected-manifest-sha256", "0" * 64])
    assert exit_info.value.code == 1
    with pytest.raises(SystemExit) as exit_info:
        cli_main(["build-ingestion-packet", "--source-run", str(source["run"]),
                  "--project-id", TARGET["project_id"], "--dataset-id", TARGET["dataset_id"],
                  "--location", "US", "--output-root", str(root)])
    assert exit_info.value.code == 1
