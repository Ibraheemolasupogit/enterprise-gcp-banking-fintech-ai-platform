"""M6M encrypted state backup regression checks. Synthetic fixtures only; no cloud, no OneDrive."""

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("state_backup", ROOT / "scripts/state_backup.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)
custody = backup.custody

FAST = {"n": 2**14, "r": 8, "p": 1}  # unit tests only; the CLI always uses KDF_PRODUCTION
PASS = "correct-horse-battery-staple-fixture-0001"
T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def vault(tmp_path):
    old = os.umask(0o077)
    try:
        home = tmp_path / "home"
        home.mkdir()
        yield backup.make_fake_custody(home), home
    finally:
        os.umask(old)


def _state(vault_root, root="bootstrap", serial=3, lineage=None):
    path = vault_root / "state" / root / "terraform.tfstate"
    path.unlink(missing_ok=True)
    doc = backup.fixture_state(root, serial, lineage)
    custody.write_private(path, json.dumps(doc).encode())
    return doc


def _create(vault_root, root="bootstrap", now=T0, passphrase=PASS):
    return backup.create_backup(vault_root, root, passphrase, now=now, kdf=FAST)


def _download(vault_root, name):
    (vault_root / "backups/inbox" / name).write_bytes(
        (vault_root / "backups/outbox" / name).read_bytes())


# ---------------------------------------------------------------- container

def test_round_trip_is_authenticated_and_hides_plaintext():
    state = json.dumps(backup.fixture_state("bootstrap", 1)).encode()
    manifest = backup.build_manifest(state, "bootstrap", "20261109T120000Z", None)
    blob = backup.encrypt_backup(state, manifest, PASS, root="bootstrap",
                                 created="20261109T120000Z", kdf=FAST)
    assert b"lineage" not in blob and b"fixture" not in blob and manifest["sha256"].encode() \
        not in blob
    header, got_manifest, got_state = backup.decrypt_backup(blob, PASS)
    assert (got_manifest, got_state) == (manifest, state)
    assert header["cipher"]["name"] == "AES-256-GCM" and header["kdf"]["name"] == "scrypt"
    # Fresh salt and nonce every time: identical input never yields identical ciphertext.
    again = backup.encrypt_backup(state, manifest, PASS, root="bootstrap",
                                  created="20261109T120000Z", kdf=FAST)
    assert again != blob


@pytest.mark.parametrize("mutate", [
    lambda b: b[:-1] + bytes([b[-1] ^ 1]),                 # tag bit flip
    lambda b: b[:len(b) // 2],                              # truncation
    lambda b: b[:60] + bytes([b[60] ^ 1]) + b[61:],         # header (AAD) byte flip
    lambda b: b"X" + b[1:],                                 # magic
])
def test_modified_containers_fail_closed(mutate):
    state = json.dumps(backup.fixture_state("bootstrap", 1)).encode()
    manifest = backup.build_manifest(state, "bootstrap", "20261109T120000Z", None)
    blob = backup.encrypt_backup(state, manifest, PASS, root="bootstrap",
                                 created="20261109T120000Z", kdf=FAST)
    with pytest.raises(backup.BackupError):
        backup.decrypt_backup(mutate(blob), PASS)


def test_wrong_passphrase_and_weak_parameters_are_refused():
    state = json.dumps(backup.fixture_state("pilot-dev", 1)).encode()
    manifest = backup.build_manifest(state, "pilot-dev", "20261109T120000Z", None)
    blob = backup.encrypt_backup(state, manifest, PASS, root="pilot-dev",
                                 created="20261109T120000Z", kdf=FAST)
    with pytest.raises(backup.BackupError, match="authentication failed"):
        backup.decrypt_backup(blob, PASS + "x")
    with pytest.raises(backup.BackupError, match="shorter"):
        backup.encrypt_backup(state, manifest, "short", root="pilot-dev", created="x", kdf=FAST)
    for bad in ({"n": 2**10, "r": 8, "p": 1}, {"n": 2**20, "r": 8, "p": 1},
                {"n": 3 * 2**14, "r": 8, "p": 1}):
        with pytest.raises(backup.BackupError, match="KDF"):
            backup.encrypt_backup(state, manifest, PASS, root="pilot-dev", created="x", kdf=bad)


def test_production_kdf_is_strong_and_used_by_default():
    assert backup.KDF_PRODUCTION["n"] >= 2**17 and backup.MIN_PASSPHRASE >= 24
    assert backup.KDF_LIMITS["n"][1] <= 2**18  # bounded memory on decrypt (no DoS header)


# ---------------------------------------------------------------- create / verify workflow

def test_create_writes_ciphertext_only_to_outbox_and_records_ledger(vault):
    vault_root, _ = vault
    doc = _state(vault_root)
    record = _create(vault_root)
    out = vault_root / "backups/outbox" / record["file"]
    assert record["file"] == "bootstrap-20261109T120000Z-serial-000003.tfstate.enc"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert doc["lineage"].encode() not in out.read_bytes()
    assert hashlib.sha256(out.read_bytes()).hexdigest() == record["ciphertext_sha256"]
    assert not list((vault_root / "backups/outbox").glob("*.partial"))
    ledger = (vault_root / backup.LEDGER).read_text()
    assert PASS not in ledger and "fixture_marker" not in ledger  # metadata only
    assert stat.S_IMODE((vault_root / backup.LEDGER).stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):  # same second, same serial: never overwritten
        _create(vault_root)


def test_verify_download_round_trip_and_plaintext_cleanup(vault):
    vault_root, _ = vault
    _state(vault_root, "pilot-dev", 9)
    name = _create(vault_root, "pilot-dev")["file"]
    _download(vault_root, name)
    report = backup.verify_download(vault_root, "pilot-dev", name, PASS, now=T0)
    assert report["verified"] and report["ledger_match"] and report["restored_mode"] == "0o600"
    assert not any((vault_root / "recovery").iterdir())  # plaintext removed
    assert backup.backup_status(backup.ledger_entries(vault_root))[name]["status"] == "verified"


@pytest.mark.parametrize("case", ["wrong_passphrase", "corrupt", "wrong_root", "renamed",
                                  "missing_ledger"])
def test_verify_download_failures_are_not_verified(vault, case):
    vault_root, _ = vault
    _state(vault_root)
    name = _create(vault_root)["file"]
    _download(vault_root, name)
    inbox = vault_root / "backups/inbox" / name
    root, passphrase, target = "bootstrap", PASS, name
    if case == "wrong_passphrase":
        passphrase = PASS + "!"
    elif case == "corrupt":
        data = bytearray(inbox.read_bytes())
        data[-5] ^= 0xFF
        inbox.write_bytes(bytes(data))
    elif case == "wrong_root":
        root = "pilot-dev"
    elif case == "renamed":
        target = name.replace("serial-000003", "serial-000004")
        inbox.rename(inbox.with_name(target))
    elif case == "missing_ledger":
        (vault_root / backup.LEDGER).unlink()
    report = backup.verify_download(vault_root, root, target, passphrase, now=T0)
    assert report["verified"] is False
    assert not any((vault_root / "recovery").iterdir())
    if case == "missing_ledger":  # disaster restore-test: authenticated, but not recorded
        assert report["authenticated"] and report["ledger_match"] is None
        assert not (vault_root / backup.LEDGER).exists()


def test_keep_plaintext_leaves_private_recovery_copy_inside_custody(vault):
    vault_root, _ = vault
    doc = _state(vault_root)
    name = _create(vault_root)["file"]
    _download(vault_root, name)
    report = backup.verify_download(vault_root, "bootstrap", name, PASS, now=T0,
                                    keep_plaintext=True)
    restored = Path(report["recovery_dir"]) / "terraform.tfstate"
    assert vault_root / "recovery" in restored.parents
    assert stat.S_IMODE(restored.parent.stat().st_mode) == 0o700
    assert json.loads(restored.read_bytes()) == doc


def test_create_refuses_lock_mode_lineage_change_and_serial_rollback(vault):
    vault_root, _ = vault
    doc = _state(vault_root, serial=4)
    lock = vault_root / "state/bootstrap/.terraform.tfstate.lock.info"
    lock.write_text("{}")
    with pytest.raises(backup.BackupError, match="lock"):
        _create(vault_root)
    lock.unlink()
    os.chmod(vault_root / "state/bootstrap/terraform.tfstate", 0o644)
    with pytest.raises(backup.BackupError, match="mode"):
        _create(vault_root)
    _state(vault_root, serial=4, lineage=doc["lineage"])
    name = _create(vault_root)["file"]
    _download(vault_root, name)
    assert backup.verify_download(vault_root, "bootstrap", name, PASS, now=T0)["verified"]
    _state(vault_root, serial=5)  # new lineage: a different state replaced the original
    with pytest.raises(backup.BackupError, match="lineage"):
        _create(vault_root, now=T0 + timedelta(hours=1))
    _state(vault_root, serial=3, lineage=doc["lineage"])
    with pytest.raises(backup.BackupError, match="serial"):
        _create(vault_root, now=T0 + timedelta(hours=2))


def test_roots_are_kept_separate(vault):
    vault_root, _ = vault
    _state(vault_root, "bootstrap", 1)
    _state(vault_root, "pilot-dev", 1)
    names = {_create(vault_root, r)["file"] for r in ("bootstrap", "pilot-dev")}
    assert {n.split("-2026")[0] for n in names} == {"bootstrap", "pilot-dev"}
    entries = backup.ledger_entries(vault_root)
    assert {e["root"] for e in entries} == {"bootstrap", "pilot-dev"}
    assert len({e["lineage"] for e in entries}) == 2


# ---------------------------------------------------------------- retention

def _entries(root, ages_days, verified=True):
    entries = []
    for i, age in enumerate(ages_days):
        stamp = (T0 - timedelta(days=age)).strftime(backup.STAMP)
        name = backup.backup_name(root, stamp, 100 - i)
        entries.append({"event": "created", "file": name, "root": root, "created_utc": stamp,
                        "ciphertext_sha256": "x", "state_sha256": "y", "serial": 100 - i,
                        "lineage": "l"})
        if verified:
            entries.append({"event": "verified", "file": name, "root": root})
    return entries


def test_retention_keeps_three_recent_verified_and_releases_older():
    plan = backup.retention_plan(_entries("bootstrap", [0, 1, 2, 3, 40]), T0)["bootstrap"]
    assert len(plan["keep"]) == 3 and len(plan["delete"]) == 2 and not plan["warnings"]


def test_retention_never_deletes_newest_verified_regardless_of_age():
    plan = backup.retention_plan(_entries("pilot-dev", [45, 60, 90]), T0)["pilot-dev"]
    assert plan["keep"] == [backup.backup_name("pilot-dev", (T0 - timedelta(days=45))
                                               .strftime(backup.STAMP), 100)]
    assert len(plan["delete"]) == 2 and "older than 30 days" in plan["warnings"][0]


def test_retention_ignores_unverified_and_separates_roots():
    entries = (_entries("bootstrap", [0, 1], verified=False) + _entries("bootstrap", [5])
               + _entries("pilot-dev", [0]))
    plan = backup.retention_plan(entries, T0)
    assert len(plan["bootstrap"]["unverified"]) == 2 and len(plan["bootstrap"]["keep"]) == 1
    assert len(plan["pilot-dev"]["keep"]) == 1
    only_unverified = backup.retention_plan(_entries("bootstrap", [0], verified=False), T0)
    assert only_unverified["bootstrap"]["warnings"] == [
        "no verified backup: state has a single copy"]


def test_mark_deleted_refuses_retained_backup(vault):
    vault_root, _ = vault
    for entry in _entries("bootstrap", [0, 1, 2, 3]):
        backup.append_ledger(vault_root, entry)
    plan = backup.retention_plan(backup.ledger_entries(vault_root), T0)["bootstrap"]
    with pytest.raises(backup.BackupError, match="retained"):
        backup.mark_deleted(vault_root, plan["keep"][0], now=T0)
    backup.mark_deleted(vault_root, plan["delete"][0], now=T0)
    after = backup.retention_plan(backup.ledger_entries(vault_root), T0)["bootstrap"]
    assert after["delete"] == [] and len(after["keep"]) == 3


# ---------------------------------------------------------------- safeguards

def test_real_commands_refuse_claude_sessions_and_non_tty():
    assert backup.refuse_unsafe_session({"CLAUDECODE": "1"}, True)
    assert backup.refuse_unsafe_session({}, False)
    assert backup.refuse_unsafe_session({}, True) == []


def test_cli_create_refuses_here_without_touching_anything(tmp_path):
    # This test runs inside tooling (no TTY; possibly CLAUDECODE); the CLI must refuse.
    result = subprocess.run([sys.executable, str(ROOT / "scripts/state_backup.py"), "create",
                             "--root", "bootstrap"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, env={**os.environ, "HOME": str(tmp_path)})
    assert result.returncode == 2 and "REFUSED" in result.stderr
    assert "interactive terminal" in result.stderr and "custody root does not exist" in \
        result.stderr
    assert list(tmp_path.iterdir()) == []  # no custody tree created


def test_custody_checks_reject_repository_and_loose_modes(vault):
    vault_root, home = vault
    assert backup.custody_problems(vault_root, home, need_filevault=False) == []
    os.chmod(vault_root / "backups/outbox", 0o755)
    assert any("outbox" in p for p in backup.custody_problems(vault_root, home,
                                                                need_filevault=False))
    inside = ROOT / "infrastructure"
    assert "inside the Git repository" in backup.custody_problems(inside, ROOT.parent,
                                                                  need_filevault=False)


def test_drill_refuses_repository_workdir():
    with pytest.raises(backup.BackupError, match="outside the repository"):
        backup.run_drill(str(ROOT / "outputs"))


def test_backup_names_are_ignored_and_blocked_in_git():
    name = "bootstrap-20261109T120000Z-serial-000001.tfstate.enc"
    for path in (name, f"outputs/{name}", f"{name}.partial"):
        assert subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0
        assert "blocked file type" in custody.blob_findings(path, b"")
    assert custody.STRAY_STATE.search(name)


def test_cryptography_is_declared_consistently_and_installed():
    # Declared (not merely globally present), so a missing package fails here instead of skipping.
    import tomllib
    from importlib.metadata import version

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    requirements = (ROOT / "requirements.txt").read_text().split()
    assert backup.CRYPTO_REQUIREMENT in declared and backup.CRYPTO_REQUIREMENT in requirements
    assert backup.AESGCM is not None
    installed = tuple(int(part) for part in version("cryptography").split(".")[:3])
    assert (46, 0, 0) <= installed < (51,)  # the declared compatible range


def test_missing_cryptography_gives_installation_message(monkeypatch):
    monkeypatch.setattr(backup, "AESGCM", None)
    with pytest.raises(backup.BackupError, match=r"pip install -r requirements\.txt"):
        backup.encrypt_backup(b"{}", {}, PASS, root="bootstrap", created="x", kdf=FAST)
    result = backup.main(["drill"])  # the CLI reports it as a refusal, not a traceback
    assert result == 2


def test_synthetic_end_to_end_drill(tmp_path):
    result = backup.run_drill(str(tmp_path), kdf=FAST)
    assert result["ok"], result
    assert result["negative_cases_all_rejected"] and result["workdir_removed"]
    assert all(result["verified"].values()) and len(result["verified"]) == 2


# ---------------------------------------------------------------- M6N synthetic browser drill

@pytest.fixture
def drill_home(tmp_path):
    old = os.umask(0o077)
    try:
        home = tmp_path / "home"
        home.mkdir(mode=0o700)
        yield home
    finally:
        os.umask(old)


def _rehearse(home):
    drill = home / backup.DRILL_DIR_NAME
    assert backup.drill_dir_problems(drill, home, ROOT, must_exist=False) == []
    record = backup.rehearse_create(drill, PASS, now=T0, kdf=FAST)
    return drill, record


def test_rehearse_create_yields_one_labelled_ciphertext_and_no_plaintext(drill_home):
    drill, record = _rehearse(drill_home)
    outbox = list((drill / "backups/outbox").iterdir())
    assert [p.name for p in outbox] == [record["file"]] == [Path(record["path"]).name]
    assert record["file"].startswith("m6n-synthetic-drill-bootstrap-")
    assert record["file"].endswith(".tfstate.enc")
    assert not backup.NAME_RE.match(record["file"])  # real commands never accept drill files
    assert not list((drill / "state").rglob("terraform.tfstate"))  # fixture plaintext removed
    blob = outbox[0].read_bytes()
    assert b"m6n-fixture" not in blob and b"lineage" not in blob
    assert stat.S_IMODE(outbox[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(drill.stat().st_mode) == 0o700
    assert "SYNTHETIC" in (drill / backup.DRILL_MARKER).read_text()
    assert backup.drill_dir_problems(drill, drill_home, ROOT, must_exist=True) == []
    ledger = (drill / backup.LEDGER).read_text()
    assert PASS not in ledger and "m6n-fixture" in ledger  # synthetic lineage, no secret
    with pytest.raises(FileExistsError):  # never reuses or overwrites a drill directory
        backup.rehearse_create(drill, PASS, now=T0, kdf=FAST)


def test_rehearse_round_trip_verifies_isolation_and_recovered_fixture(drill_home, tmp_path):
    drill, record = _rehearse(drill_home)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    downloaded = downloads / record["file"]  # simulated browser upload + download
    downloaded.write_bytes(Path(record["path"]).read_bytes())
    report = backup.rehearse_verify(drill, downloaded, PASS, now=T0)
    assert report["ok"], report
    assert report["byte_identical_round_trip"] and report["cross_root_restore_rejected"]
    assert report["wrong_passphrase_rejected"] and report["operator_passphrase_authenticated"]
    assert all(v for k, v in report["recovered_fixture"].items() if k != "serial")
    assert report["ledger_status"] == "verified"
    evidence = Path(report["evidence_path"]).read_text()
    assert PASS not in evidence and stat.S_IMODE(Path(report["evidence_path"]).stat().st_mode) \
        == 0o600


@pytest.mark.parametrize("case", ["wrong_passphrase", "corrupted", "renamed"])
def test_rehearse_verify_fails_closed(drill_home, tmp_path, case):
    drill, record = _rehearse(drill_home)
    data = bytearray(Path(record["path"]).read_bytes())
    name, passphrase = record["file"], PASS
    if case == "wrong_passphrase":
        passphrase = PASS + "!"
    elif case == "corrupted":
        data[-3] ^= 0x10
    else:
        name = name.replace(".tfstate.enc", " (1).tfstate.enc")  # browser duplicate naming
    downloaded = tmp_path / name
    downloaded.write_bytes(bytes(data))
    report = backup.rehearse_verify(drill, downloaded, passphrase, now=T0, terraform=False)
    assert report["ok"] is False
    if case == "renamed":  # content is fine, but the exact-name rule must still fail
        assert report["byte_identical_round_trip"] and not report["downloaded_name_matches"]
    else:
        assert report["ledger_status"] == "verify-failed"


@pytest.mark.parametrize("where", ["repo", "custody", "desktop", "outside_home"])
def test_drill_dir_must_be_disposable_and_separate(drill_home, tmp_path, where):
    target = {"repo": ROOT / "m6n-onedrive-drill",
              "custody": drill_home / ".secure" / "m6n-onedrive-drill",
              "desktop": drill_home / "Desktop" / "m6n-onedrive-drill",
              "outside_home": tmp_path / "elsewhere"}[where]
    assert backup.drill_dir_problems(target, drill_home, ROOT, must_exist=False)


def test_rehearse_create_cleans_up_if_encryption_fails(drill_home):
    drill = drill_home / backup.DRILL_DIR_NAME
    with pytest.raises(backup.BackupError, match="shorter"):
        backup.rehearse_create(drill, "too-short", now=T0, kdf=FAST)
    assert not drill.exists()  # no plaintext fixture left behind


def test_rehearse_cli_refuses_inside_tooling(tmp_path):
    for args in (["rehearse-create"], ["rehearse-verify", "--downloaded", "x"]):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/state_backup.py"), *args],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                env={**os.environ, "HOME": str(tmp_path), "CLAUDECODE": "1"})
        assert result.returncode == 2 and "Claude Code session" in result.stderr
    assert list(tmp_path.iterdir()) == []  # nothing created
