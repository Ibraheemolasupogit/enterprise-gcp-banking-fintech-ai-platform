"""M7C2 G3: operator command bundle rendering, re-derivation and fail-closed execution.

The generated bash script is executed only inside a sandbox whose PATH holds fake ``gcloud``
and ``curl`` (tests/m7c2_support.py) and no real cloud tool. No test contacts a network.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from m7c2_support import BUCKET, LOADER, REPO, FakeCloud, build_fixture_packet, open_window

from financial_crime_ml.cli import main as cli_main
from financial_crime_ml.deployment import ingestion_bundle as ib
from financial_crime_ml.deployment.ingestion_bundle import (
    BundleError,
    derive,
    load_bundle,
    render_bundle,
)
from financial_crime_ml.deployment.ingestion_packet import LOAD_SETTINGS, PacketError
from financial_crime_ml.evaluation.manifest import sha256_bytes

TABLES = ("customers", "accounts", "curated_transactions")


@pytest.fixture(scope="module")
def packet(tmp_path_factory):
    return build_fixture_packet(tmp_path_factory.mktemp("m7c2-bundle"))


def render(packet, tmp_path, name="bundle", window=None, bucket=BUCKET):
    start, expiry = window or open_window()
    result = render_bundle(packet["packet_dir"],
                           expected_manifest_sha256=packet["manifest_sha256"], bucket=bucket,
                           window_start=start, window_expiry=expiry,
                           output_dir=tmp_path / name)
    return tmp_path / name, result


# ------------------------------------------------------------------ rendering
def test_bundle_is_derived_from_the_packet(packet, tmp_path):
    bundle, summary = render(packet, tmp_path)
    manifest = packet["manifest"]
    record = json.loads((bundle / "bundle.json").read_bytes())
    assert summary["packet_id"] == record["packet_id"] == packet["packet_id"]
    assert record["loader_email"] == LOADER
    assert record["object_prefix"] == f"m7/ingestion/{packet['packet_id']}/"
    for table in TABLES:
        spec = manifest["load_jobs"][table]
        body = json.loads((bundle / f"requests/{table}.job.json").read_bytes())
        assert body["jobReference"] == {"jobId": spec["job_id"], "location": "europe-west2",
                                        "projectId": "ebank-pilot-dev-261031-k7m4"}
        load = body["configuration"]["load"]
        assert load["sourceUris"] == [f"gs://{BUCKET}/{manifest['objects']['names'][table]}"]
        assert load["schema"] == spec["configuration"]["load"]["schema"]
        assert load["destinationTable"] == spec["configuration"]["load"]["destinationTable"]
        for key, value in LOAD_SETTINGS.items():
            assert load[key] == value
        assert set(load) == {*LOAD_SETTINGS, "destinationTable", "schema", "sourceUris"}
        assert set(body) == {"configuration", "jobReference"}
        assert set(body["configuration"]) == {"load"}
        assert record["tables"][table]["md5_base64"] == manifest["tables"][table]["md5_base64"]
        assert record["tables"][table]["request_sha256"] == sha256_bytes(
            (bundle / f"requests/{table}.job.json").read_bytes())


def test_summary_never_contains_the_bucket(packet, tmp_path, capsys):
    _, summary = render(packet, tmp_path)
    assert BUCKET not in json.dumps(summary)
    start, expiry = open_window()
    (tmp_path / "work" / ".git").mkdir(parents=True)
    with pytest.raises(SystemExit):
        cli_main(["render-ingestion-bundle", "--packet-dir", str(packet["packet_dir"]),
                  "--expected-manifest-sha256", packet["manifest_sha256"], "--bucket", BUCKET,
                  "--window-start", start, "--window-expiry", expiry,
                  "--output-dir", str(tmp_path / "work" / "never-here")])
    err = capsys.readouterr().err
    assert BUCKET not in err and "outside any git worktree" in err
    assert not (tmp_path / "work" / "never-here").exists()
    cli_main(["render-ingestion-bundle", "--packet-dir", str(packet["packet_dir"]),
              "--expected-manifest-sha256", packet["manifest_sha256"], "--bucket", BUCKET,
              "--window-start", start, "--window-expiry", expiry,
              "--output-dir", str(tmp_path / "cli")])
    out = capsys.readouterr().out
    assert BUCKET not in out and json.loads(out)["packet_id"] == packet["packet_id"]


def test_rendering_is_deterministic(packet, tmp_path):
    window = open_window()
    first, a = render(packet, tmp_path, "a", window)
    second, b = render(packet, tmp_path, "b", window)
    assert a == b
    for name in ("m7d.sh", "bundle.json", "SHA256SUMS", "packet.sha256",
                 *(f"requests/{t}.job.json" for t in TABLES)):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_bundle_files_are_private_and_create_exclusive(packet, tmp_path):
    bundle, _ = render(packet, tmp_path)
    assert bundle.stat().st_mode & 0o777 == 0o700
    for path in bundle.rglob("*"):
        assert path.stat().st_mode & 0o077 == 0, path
    with pytest.raises(BundleError, match="never overwritten"):
        render(packet, tmp_path)


@pytest.mark.parametrize("bucket", ["", "UPPER-case-bucket", "a", "goog-bucket-name",
                                    "my-google-bucket", "bucket.with.dots", "bucket--double",
                                    "bucket'quote", "bucket$(x)", "-leading-dash", "x" * 64])
def test_invalid_bucket_names_are_refused(packet, tmp_path, bucket):
    with pytest.raises(BundleError, match="bucket"):
        render(packet, tmp_path, bucket=bucket)
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("window", [
    ("2026-11-20T09:00:00Z", "2026-11-20T17:00:01Z"),  # over 8 hours
    ("2026-11-20T09:00:00Z", "2026-11-20T09:00:00Z"),  # empty
    ("2026-11-20T09:00:00Z", "2026-11-20T09:15:00Z"),  # not longer than the margin
    ("2026-11-20T10:00:00Z", "2026-11-20T09:00:00Z"),  # reversed
    ("2026-11-20 09:00:00", "2026-11-20T10:00:00Z"),   # not the strict format
    ("2026-11-20T09:00:00+01:00", "2026-11-20T10:00:00Z"),
])
def test_invalid_windows_are_refused(packet, tmp_path, window):
    with pytest.raises(BundleError, match="window"):
        render(packet, tmp_path, window=window)


def test_manifest_hash_is_required_and_enforced(packet, tmp_path):
    start, expiry = open_window()
    for bad in ("", "abc", "G" * 64, None):
        with pytest.raises(BundleError, match="manifest SHA-256"):
            render_bundle(packet["packet_dir"], expected_manifest_sha256=bad, bucket=BUCKET,
                          window_start=start, window_expiry=expiry, output_dir=tmp_path / "x")
    with pytest.raises(PacketError, match="approved value"):
        render_bundle(packet["packet_dir"], expected_manifest_sha256="0" * 64, bucket=BUCKET,
                      window_start=start, window_expiry=expiry, output_dir=tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_bundle_must_live_outside_any_git_worktree(packet, tmp_path, monkeypatch):
    (tmp_path / "work" / ".git").mkdir(parents=True)
    with pytest.raises(BundleError, match="git worktree"):
        render(packet, tmp_path / "work")
    # The real repository: if the guard were broken, derivation (which precedes any write)
    # fails loudly instead, so nothing can ever be written into the checkout.
    def bypassed(*args, **kwargs):
        raise AssertionError("worktree guard bypassed")

    monkeypatch.setattr(ib, "derive", bypassed)
    with pytest.raises(BundleError, match="git worktree"):
        render_bundle(packet["packet_dir"], expected_manifest_sha256=packet["manifest_sha256"],
                      bucket=BUCKET, window_start=open_window()[0],
                      window_expiry=open_window()[1], output_dir=REPO / "bundle-here")
    with pytest.raises(BundleError, match="inside the packet"):
        render_bundle(packet["packet_dir"], expected_manifest_sha256=packet["manifest_sha256"],
                      bucket=BUCKET, window_start=open_window()[0],
                      window_expiry=open_window()[1], output_dir=packet["packet_dir"] / "b")
    assert not (REPO / "bundle-here").exists()


def test_unsafe_paths_cannot_reach_the_script(packet, tmp_path):
    odd = tmp_path / "it's"
    odd.mkdir()
    with pytest.raises(BundleError, match="unsafe"):
        render(packet, odd)
    copy = tmp_path / "pkt $HOME"
    shutil.copytree(packet["packet_dir"], copy / packet["packet_id"])
    with pytest.raises(BundleError, match="unsafe"):
        render_bundle(copy / packet["packet_id"],
                      expected_manifest_sha256=packet["manifest_sha256"], bucket=BUCKET,
                      window_start=open_window()[0], window_expiry=open_window()[1],
                      output_dir=tmp_path / "b")


def test_tampered_packet_is_refused_at_render(packet, tmp_path):
    copy = tmp_path / "copy" / packet["packet_id"]
    shutil.copytree(packet["packet_dir"], copy)
    data = copy / "data" / "accounts.csv"
    data.write_bytes(data.read_bytes() + b"x")
    with pytest.raises(PacketError):
        render_bundle(copy, expected_manifest_sha256=packet["manifest_sha256"], bucket=BUCKET,
                      window_start=open_window()[0], window_expiry=open_window()[1],
                      output_dir=tmp_path / "b")


def test_loader_identity_is_derived_not_supplied(packet):
    assert ib.loader_email({"project_id": "p-123456", "dataset_id": "bankpilot_dev"}) == \
        "bankpilot-dev-loader@p-123456.iam.gserviceaccount.com"
    assert "--loader" not in ib.render_main.__code__.co_consts


# ------------------------------------------------------------------ script content
def test_script_has_only_create_only_and_read_operations(packet, tmp_path):
    bundle, _ = render(packet, tmp_path)
    script = (bundle / "m7d.sh").read_text()
    methods = set(re.findall(r"\bapi (GET|PUT|POST|DELETE|PATCH)\b", script))
    assert methods == {"GET", "PUT", "POST"}
    assert script.count("api PUT") == 1 and script.count("api POST") == 1
    assert "x-goog-if-generation-match: 0" in script and "Content-MD5: $MD5" in script
    for forbidden in ("DELETE", "PATCH", "gsutil", "bq ", "terraform", "setIamPolicy",
                      "rm -rf", "--force", "WRITE_TRUNCATE", "WRITE_APPEND", "CREATE_IF_NEEDED",
                      "iam ", "storage rm", "objects/delete", "allowJaggedRows\": true",
                      "dryRun"):
        assert forbidden not in script, forbidden
    assert "set -euo pipefail" in script and "set -o noclobber" in script
    assert "formatOptions.useInt64Timestamp=true" in script
    assert 'Authorization: Bearer %s\\n\' "$TOKEN"' in script
    # The token reaches curl only through a builtin printf in a process substitution.
    assert re.search(r'-H @<\(printf .Authorization: Bearer %s', script)
    assert "echo \"$TOKEN\"" not in script and "set -x" not in script
    assert f"BUCKET='{BUCKET}'" in script


def test_load_requests_use_write_empty_and_create_never(packet, tmp_path):
    bundle, _ = render(packet, tmp_path)
    for table in TABLES:
        load = json.loads((bundle / f"requests/{table}.job.json").read_bytes())[
            "configuration"]["load"]
        assert load["writeDisposition"] == "WRITE_EMPTY"
        assert load["createDisposition"] == "CREATE_NEVER"
        assert load["maxBadRecords"] == 0 and load["autodetect"] is False
        assert load["schemaUpdateOptions"] == []


# ------------------------------------------------------------------ re-derivation
def test_load_bundle_re_derives_every_file(packet, tmp_path):
    bundle, summary = render(packet, tmp_path)
    record = load_bundle(bundle, packet["packet_dir"], packet["manifest_sha256"])
    assert record["bundle_sha256"] == summary["bundle_sha256"]


@pytest.mark.parametrize("target", ["m7d.sh", "bundle.json", "SHA256SUMS", "packet.sha256",
                                    "requests/customers.job.json",
                                    "requests/curated_transactions.job.json"])
def test_any_edited_bundle_file_is_rejected(packet, tmp_path, target):
    bundle, _ = render(packet, tmp_path)
    path = bundle / target
    content = path.read_bytes()
    if target == "bundle.json":
        record = json.loads(content)
        record["tables"]["customers"]["job_id"] = "m7_load_other_customers"
        path.write_bytes(json.dumps(record).encode())
    elif target.endswith(".job.json"):
        path.write_bytes(content.replace(b"WRITE_EMPTY", b"WRITE_APPEND"))
    else:
        path.write_bytes(content + b"# appended\n")
    with pytest.raises(BundleError):
        load_bundle(bundle, packet["packet_dir"], packet["manifest_sha256"])


@pytest.mark.parametrize("change", ["bucket", "start", "expiry", "repo_dir", "packet_dir"])
def test_recorded_inputs_cannot_be_swapped_consistently(packet, tmp_path, change):
    """Even a consistent rewrite of the record (re-hashed) is caught: re-derivation needs the
    script and requests to match too, and the packet directory must be the one verified."""
    bundle, _ = render(packet, tmp_path)
    record = json.loads((bundle / "bundle.json").read_bytes())
    if change == "bucket":
        record["bucket"] = "another-pilot-bucket"
    elif change in ("start", "expiry"):
        record["window"][change] = "2026-11-20T09:30:00Z" if change == "start" else \
            "2026-11-20T10:30:00Z"
    elif change == "repo_dir":
        record["repo_dir"] = "/tmp/elsewhere"
    else:
        record["packet_dir"] = "/tmp/elsewhere"
    (bundle / "bundle.json").write_bytes(ib.canonical_json(record))
    with pytest.raises(BundleError):
        load_bundle(bundle, packet["packet_dir"], packet["manifest_sha256"])


def test_extra_or_linked_bundle_files_are_rejected(packet, tmp_path):
    bundle, _ = render(packet, tmp_path)
    (bundle / "requests" / "extra.job.json").write_text("{}")
    with pytest.raises(BundleError, match="unexpected"):
        load_bundle(bundle, packet["packet_dir"], packet["manifest_sha256"])
    (bundle / "requests" / "extra.job.json").unlink()
    (bundle / "notes").mkdir()
    with pytest.raises(BundleError, match="unexpected directory"):
        load_bundle(bundle, packet["packet_dir"], packet["manifest_sha256"])
    (bundle / "notes").rmdir()
    (bundle / "link").symlink_to(bundle / "m7d.sh")
    with pytest.raises(BundleError, match="symbolic link"):
        load_bundle(bundle, packet["packet_dir"], packet["manifest_sha256"])


def test_derive_rejects_unexpected_packet_load_keys(packet, monkeypatch):
    real = ib.verify_packet

    def tampered(*args, **kwargs):
        result = real(*args, **kwargs)
        result["manifest"]["load_jobs"]["customers"]["configuration"]["load"]["x"] = 1
        return result

    monkeypatch.setattr(ib, "verify_packet", tampered)
    start, expiry = open_window()
    with pytest.raises(BundleError, match="unexpected keys"):
        derive(packet["packet_dir"], expected_manifest_sha256=packet["manifest_sha256"],
               bucket=BUCKET, window_start=start, window_expiry=expiry)


# ------------------------------------------------------------------ sandboxed execution
def approved_hash(bundle: Path) -> str:
    return sha256_bytes((bundle / "SHA256SUMS").read_bytes())


@pytest.fixture
def sandbox(packet, tmp_path):
    bundle, _ = render(packet, tmp_path)
    cloud = FakeCloud(tmp_path / "sandbox", packet)
    return bundle, cloud, approved_hash(bundle)


def run_ok(cloud, bundle, approved, *step):
    result = cloud.run(bundle, approved, *step)
    assert result.returncode == 0, (step, result.stdout, result.stderr)
    return result


def test_full_sandboxed_run_verifies_end_to_end(packet, sandbox):
    bundle, cloud, approved = sandbox
    cloud.set_knobs({"pending_polls": 1})
    run_ok(cloud, bundle, approved, "verify-bundle")
    run_ok(cloud, bundle, approved, "preflight")
    for table in TABLES:
        run_ok(cloud, bundle, approved, "upload", table)
    for table in TABLES:
        run_ok(cloud, bundle, approved, "load", table)
        pending = cloud.run(bundle, approved, "job", table)
        assert pending.returncode == 3 and "run this step again" in pending.stdout
        run_ok(cloud, bundle, approved, "job", table)
    run_ok(cloud, bundle, approved, "post-objects")
    for table in TABLES:
        run_ok(cloud, bundle, approved, "readback", table)
    run_ok(cloud, bundle, approved, "inventory")
    run_ok(cloud, bundle, approved, "verify-all")
    report = json.loads((bundle / "evidence" / "report.json").read_text())
    assert report["ok"] is True and report["inventory"] == "verified"
    assert BUCKET not in (bundle / "evidence" / "report.json").read_text()
    for table in TABLES:
        readback = report["tables"][table]["readback"]
        assert readback["sha256_matches_packet"] is True
        assert readback["rebuilt_sha256"] == packet["manifest"]["tables"][table]["sha256"]
        assert readback["num_bytes_matches_logical_model"] is True
    calls = cloud.calls()
    curl = [c for c in calls if c["tool"] == "curl"]
    assert {c["method"] for c in curl} == {"GET", "PUT", "POST"}
    assert sum(c["method"] == "PUT" for c in curl) == 3
    assert sum(c["method"] == "POST" for c in curl) == 3
    hosts = {re.match(r"https://([^/]+)/", c["url"]).group(1) for c in curl}
    assert hosts == {"storage.googleapis.com", "bigquery.googleapis.com"}
    # Every call except the operator's prefix listing uses the loader identity.
    listing = [c for c in curl if c["url"].endswith("/o")]
    assert len(listing) == 1 and listing[0]["token_kind"] == "ya29.operator"
    assert all(c["token_kind"] == "ya29.loader" for c in curl if c not in listing)
    # The access token never appears in any process argument list.
    assert not any("ya29." in arg for c in calls for arg in c["argv"])
    gcloud = [c["argv"] for c in calls if c["tool"] == "gcloud"]
    assert all(a[:2] == ["auth", "print-access-token"] for a in gcloud)
    assert sum(f"--impersonate-service-account={LOADER}" in a for a in gcloud) == len(gcloud) - 1


def test_nothing_runs_without_the_approved_bundle_hash(sandbox):
    bundle, cloud, approved = sandbox
    for value in (None, "", "0" * 64, approved.upper()):
        result = cloud.run(bundle, value, "preflight")
        assert result.returncode == 1 and "STOP" in result.stderr
    assert cloud.calls() == []


def test_steps_refuse_outside_the_window(packet, tmp_path):
    future = ("2099-01-01T00:00:00Z", "2099-01-01T04:00:00Z")
    bundle, _ = render(packet, tmp_path, window=future)
    cloud = FakeCloud(tmp_path / "sandbox", packet)
    result = cloud.run(bundle, approved_hash(bundle), "preflight")
    assert result.returncode == 1 and "before the approved window" in result.stderr
    past = ("2020-01-01T00:00:00Z", "2020-01-01T04:00:00Z")
    bundle, _ = render(packet, tmp_path, "past", window=past)
    result = cloud.run(bundle, approved_hash(bundle), "upload", "customers")
    assert result.returncode == 1 and "expiry" in result.stderr
    assert cloud.calls() == []


def test_edited_script_or_request_stops_every_step(sandbox):
    bundle, cloud, approved = sandbox
    request = bundle / "requests" / "customers.job.json"
    request.write_bytes(request.read_bytes().replace(b"WRITE_EMPTY", b"WRITE_APPEND"))
    result = cloud.run(bundle, approved, "load", "customers")
    assert result.returncode == 1 and "differ from SHA256SUMS" in result.stderr
    assert cloud.calls() == []


def test_tampered_packet_stops_before_upload(packet, tmp_path):
    copy_root = tmp_path / "pk"
    shutil.copytree(packet["packet_dir"], copy_root / packet["packet_id"])
    copied = {**packet, "packet_dir": copy_root / packet["packet_id"]}
    bundle, _ = render(copied, tmp_path)
    cloud = FakeCloud(tmp_path / "sandbox", packet)
    approved = approved_hash(bundle)
    run_ok(cloud, bundle, approved, "preflight")
    data = copied["packet_dir"] / "data" / "customers.csv"
    data.write_bytes(data.read_bytes() + b"\n")
    result = cloud.run(bundle, approved, "upload", "customers")
    assert result.returncode == 1 and "packet bytes differ" in result.stderr
    assert not any(c.get("method") == "PUT" for c in cloud.calls())


def test_failed_impersonation_stops(sandbox):
    bundle, cloud, approved = sandbox
    cloud.set_knobs({"deny_impersonation": True})
    result = cloud.run(bundle, approved, "preflight")
    assert result.returncode == 1 and "could not be minted" in result.stderr
    assert not [c for c in cloud.calls() if c["tool"] == "curl"]


def test_load_requires_verified_object_and_empty_table(sandbox):
    bundle, cloud, approved = sandbox
    result = cloud.run(bundle, approved, "load", "customers")
    assert result.returncode == 1
    assert not any(c.get("method") == "POST" for c in cloud.calls())


def test_upload_is_create_only_and_never_retried(sandbox):
    bundle, cloud, approved = sandbox
    run_ok(cloud, bundle, approved, "preflight")
    run_ok(cloud, bundle, approved, "upload", "customers")
    again = cloud.run(bundle, approved, "upload", "customers")
    assert again.returncode == 1 and "never overwritten" in again.stderr
    for name in ("upload/customers.headers", "upload/customers.body", "objects/customers.json"):
        (bundle / "evidence" / name).rename(bundle / "evidence" / f"{name}.moved")
    blocked = cloud.run(bundle, approved, "upload", "customers")
    assert blocked.returncode == 1 and "already exists" in blocked.stderr
    puts = [c for c in cloud.calls() if c.get("method") == "PUT"]
    assert len(puts) == 2 and len(cloud.state()["objects"]) == 1


def test_corrupted_object_is_detected_by_md5_crc32c(sandbox):
    bundle, cloud, approved = sandbox
    run_ok(cloud, bundle, approved, "preflight")
    cloud.set_knobs({"corrupt_upload": True})
    result = cloud.run(bundle, approved, "upload", "accounts")
    assert result.returncode == 1
    assert "md5Hash differs" in result.stdout and "crc32c differs" in result.stdout
    assert BUCKET not in result.stdout + result.stderr
    load = cloud.run(bundle, approved, "load", "accounts")
    assert load.returncode == 1 and not any(c.get("method") == "POST" for c in cloud.calls())


def test_load_job_id_is_submitted_once(sandbox):
    bundle, cloud, approved = sandbox
    run_ok(cloud, bundle, approved, "preflight")
    run_ok(cloud, bundle, approved, "upload", "customers")
    run_ok(cloud, bundle, approved, "load", "customers")
    again = cloud.run(bundle, approved, "load", "customers")
    assert again.returncode == 1 and "never overwritten" in again.stderr
    (bundle / "evidence/jobs/customers.insert.json").rename(bundle / "evidence/moved.json")
    duplicate = cloud.run(bundle, approved, "load", "customers")
    assert duplicate.returncode == 1 and "already exists" in duplicate.stderr
    assert len(cloud.state()["jobs"]) == 1


def test_failed_load_job_is_reported_and_stops_readback(sandbox):
    bundle, cloud, approved = sandbox
    run_ok(cloud, bundle, approved, "preflight")
    run_ok(cloud, bundle, approved, "upload", "customers")
    cloud.set_knobs({"fail_load": "customers"})
    run_ok(cloud, bundle, approved, "load", "customers")
    job = cloud.run(bundle, approved, "job", "customers")
    assert job.returncode == 1 and "BigQuery reported an error (duplicate)" in job.stdout
    assert BUCKET not in job.stdout
    readback = cloud.run(bundle, approved, "readback", "customers")
    assert readback.returncode == 1 and "not verified" in readback.stderr


def test_changed_readback_fails_reconciliation(sandbox):
    bundle, cloud, approved = sandbox
    run_ok(cloud, bundle, approved, "preflight")
    run_ok(cloud, bundle, approved, "upload", "customers")
    run_ok(cloud, bundle, approved, "load", "customers")
    run_ok(cloud, bundle, approved, "job", "customers")
    cloud.set_knobs({"mutate_readback": "customers"})
    result = cloud.run(bundle, approved, "readback", "customers")
    assert result.returncode == 1 and "row(s) differ" in result.stdout


def test_transport_error_on_load_stops_without_resubmission(sandbox):
    bundle, cloud, approved = sandbox
    run_ok(cloud, bundle, approved, "preflight")
    run_ok(cloud, bundle, approved, "upload", "customers")
    cloud.set_knobs({"transport_error": "/jobs"})
    result = cloud.run(bundle, approved, "load", "customers")
    assert result.returncode == 1 and "never resubmit" in result.stderr
    assert sum(c.get("method") == "POST" for c in cloud.calls()) == 1


def test_unknown_steps_and_tables_stop(sandbox):
    bundle, cloud, approved = sandbox
    for step in (("delete",), ("upload", "alerts"), ("upload",), ("load", "../x"), ()):
        result = cloud.run(bundle, approved, *step)
        assert result.returncode == 1 and "STOP" in result.stderr, step
    assert cloud.calls() == []
