#!/usr/bin/env python3
"""M6M encrypted Terraform state backups for a manual, browser-only OneDrive upload.

Never contacts a cloud API, never uploads or downloads, never reads OneDrive credentials.
Plaintext state is read only from the M6L custody tree and is never written outside it.

Each backup is one self-contained file, ``<root>-<UTC>-serial-<NNNNNN>.tfstate.enc``:
AES-256-GCM (authenticated) under a key derived with scrypt from an operator passphrase and
a fresh random salt. The public header (root, creation time, KDF and cipher parameters) is
bound to the ciphertext as associated data, so header edits, truncation, bit flips, a wrong
passphrase and cross-root substitution all fail closed.

Subcommands (real ones refuse to run inside a Claude Code session and need a TTY):
  create           Encrypt the custody state of one root into backups/outbox/ (ciphertext only).
  verify-download  Check a re-downloaded file in backups/inbox/ against the ledger, decrypt it in
                   a 0700 recovery directory, validate it, delete the plaintext, record the result.
  retention        Show which verified backups to keep and which may be deleted from OneDrive.
  mark-deleted     Record that an operator deleted a backup from OneDrive (refuses kept ones).
  drill            End-to-end rehearsal on disposable synthetic fixtures in a temporary fake home.
  rehearse-create  M6N: operator-run. Encrypt a synthetic fixture under the operator's real backup
                   passphrase into a disposable ~/m6n-onedrive-drill directory for a manual
                   browser upload. No custody tree, backend or real state is used.
  rehearse-verify  M6N: operator-run. Verify the file downloaded back from OneDrive: byte match,
                   wrong-passphrase and cross-root rejection, recovered fixture, terraform show.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state_custody as custody  # noqa: E402  (sibling module; stdlib only)

CRYPTO_REQUIREMENT = "cryptography>=46.0.0,<51"  # keep in sync with requirements.txt
try:  # Authenticated encryption is not in the standard library.
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # reported at use time with installation guidance
    AESGCM = None
    InvalidTag = Exception

MAGIC = b"M6MTFBK\x01"
FORMAT = "m6m-tfstate-backup"
SUFFIX = ".tfstate.enc"  # matched by .gitignore `*.tfstate.*` and the staged/stray guards
NAME_BODY = (r"(?P<root>bootstrap|pilot-dev)-(?P<stamp>\d{8}T\d{6}Z)"
             r"-serial-(?P<serial>\d{6,})\.tfstate\.enc$")
NAME_RE = re.compile("^" + NAME_BODY)  # real backups: anchored, so drill names never match
DRILL_PREFIX = "m6n-synthetic-drill-"  # M6N synthetic browser drill files only
DRILL_DIR_NAME = "m6n-onedrive-drill"
DRILL_MARKER = "SYNTHETIC-DRILL-NOT-REAL-STATE.txt"
STAMP = "%Y%m%dT%H%M%SZ"
KDF_PRODUCTION = {"n": 2**17, "r": 8, "p": 1}  # 128 MiB, roughly 0.3-1 s per derivation
KDF_LIMITS = {"n": (2**14, 2**18), "r": (8, 8), "p": (1, 1)}  # refuse weak or DoS parameters
MIN_PASSPHRASE = 24
MAX_STATE_BYTES = 64 * 2**20
MAX_HEADER_BYTES = 4096
RETAIN_COUNT, RETAIN_DAYS = 3, 30
BACKUP_DIRS = ("backups/outbox", "backups/inbox", "recovery")
LEDGER = Path("backups/ledger.jsonl")


class BackupError(Exception):
    """A refusal or failed verification. Messages never contain state or passphrases."""


# ---------------------------------------------------------------- container format

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _derive(passphrase: str, salt: bytes, kdf: dict) -> bytes:
    for key, (low, high) in KDF_LIMITS.items():
        value = kdf.get(key)
        if not isinstance(value, int) or not low <= value <= high:
            raise BackupError(f"KDF parameter {key} outside the accepted range")
    if kdf["n"] & (kdf["n"] - 1):
        raise BackupError("KDF parameter n is not a power of two")
    secret = unicodedata.normalize("NFC", passphrase).encode()
    return hashlib.scrypt(secret, salt=salt, n=kdf["n"], r=kdf["r"], p=kdf["p"],
                          maxmem=256 * kdf["r"] * kdf["n"] + 2**20, dklen=32)


def _require_crypto() -> None:
    if AESGCM is None:
        raise BackupError(f"the '{CRYPTO_REQUIREMENT}' package is required for AES-256-GCM "
                          "and is not installed for this Python; install the project "
                          "dependencies: python3 -m pip install -r requirements.txt")


def check_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"passphrase shorter than {MIN_PASSPHRASE} characters; generate one "
                          "in the password manager")


def encrypt_backup(state_bytes: bytes, manifest: dict, passphrase: str, *, root: str,
                   created: str, kdf: dict | None = None) -> bytes:
    """Return the container bytes. ``kdf`` overrides exist for fast unit tests only."""
    _require_crypto()
    check_passphrase(passphrase)
    kdf = dict(kdf or KDF_PRODUCTION)
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    header = {"format": FORMAT, "version": 1, "root": root, "created_utc": created,
              "kdf": {"name": "scrypt", **kdf, "salt": _b64(salt)},
              "cipher": {"name": "AES-256-GCM", "nonce": _b64(nonce)}}
    header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    aad = MAGIC + len(header_bytes).to_bytes(4, "big") + header_bytes
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    plaintext = len(manifest_bytes).to_bytes(4, "big") + manifest_bytes + state_bytes
    return aad + AESGCM(_derive(passphrase, salt, kdf)).encrypt(nonce, plaintext, aad)


def read_header(blob: bytes) -> tuple[dict, bytes, bytes]:
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 4:
        raise BackupError("not an M6M backup container")
    size = int.from_bytes(blob[len(MAGIC):len(MAGIC) + 4], "big")
    if size > MAX_HEADER_BYTES or len(blob) < len(MAGIC) + 4 + size + 16:
        raise BackupError("container header is malformed or truncated")
    aad = blob[:len(MAGIC) + 4 + size]
    try:
        header = json.loads(aad[len(MAGIC) + 4:])
    except ValueError as exc:
        raise BackupError("container header is not valid JSON") from exc
    if header.get("format") != FORMAT or header.get("version") != 1:
        raise BackupError("unsupported container format")
    return header, aad, blob[len(aad):]


def decrypt_backup(blob: bytes, passphrase: str) -> tuple[dict, dict, bytes]:
    """Return (header, manifest, state bytes) or raise BackupError. Fails closed."""
    _require_crypto()
    header, aad, ciphertext = read_header(blob)
    try:
        kdf = {k: header["kdf"][k] for k in ("n", "r", "p")}
        salt = base64.b64decode(header["kdf"]["salt"], validate=True)
        nonce = base64.b64decode(header["cipher"]["nonce"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupError("container header is missing parameters") from exc
    if header["kdf"].get("name") != "scrypt" or header["cipher"].get("name") != "AES-256-GCM" \
            or len(salt) != 16 or len(nonce) != 12:
        raise BackupError("unsupported KDF or cipher parameters")
    try:
        plaintext = AESGCM(_derive(passphrase, salt, kdf)).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise BackupError("authentication failed: wrong passphrase or modified file") from exc
    size = int.from_bytes(plaintext[:4], "big")
    manifest = json.loads(plaintext[4:4 + size])
    return header, manifest, plaintext[4 + size:]


# ---------------------------------------------------------------- state and manifest

def parse_state(state_bytes: bytes) -> dict:
    if len(state_bytes) > MAX_STATE_BYTES:
        raise BackupError("state file larger than the accepted limit")
    try:
        state = json.loads(state_bytes)
    except ValueError as exc:
        raise BackupError("state is not valid JSON (partial write?)") from exc
    if not isinstance(state, dict) or state.get("version") != 4 \
            or not isinstance(state.get("serial"), int) or not state.get("lineage") \
            or not isinstance(state.get("resources"), list):
        raise BackupError("state is not a Terraform v4 state document")
    return state


def build_manifest(state_bytes: bytes, root: str, created: str, git_commit: str | None) -> dict:
    state = parse_state(state_bytes)
    return {"root": root, "created_utc": created, "sha256": hashlib.sha256(state_bytes).hexdigest(),
            "size": len(state_bytes), "serial": state["serial"], "lineage": state["lineage"],
            "terraform_version": state.get("terraform_version"), "git_commit": git_commit}


def name_pattern(prefix: str = "") -> re.Pattern:
    return re.compile("^" + re.escape(prefix) + NAME_BODY)


def backup_name(root: str, created: str, serial: int, prefix: str = "") -> str:
    return f"{prefix}{root}-{created}-serial-{serial:06d}{SUFFIX}"


def validate_restored(header: dict, manifest: dict, state_bytes: bytes, *, name: str,
                      root: str, prefix: str = "") -> dict:
    """Cross-check file name, authenticated header, manifest and restored state."""
    match = name_pattern(prefix).match(name)
    state = parse_state(state_bytes)
    checks = {
        "name_format_ok": bool(match),
        "root_ok": bool(match) and match["root"] == header["root"] == manifest["root"] == root,
        "created_ok": bool(match) and match["stamp"] == header["created_utc"]
        == manifest["created_utc"],
        "serial_ok": bool(match) and int(match["serial"]) == manifest["serial"]
        == state["serial"],
        "sha256_ok": hashlib.sha256(state_bytes).hexdigest() == manifest["sha256"],
        "lineage_ok": state["lineage"] == manifest["lineage"],
    }
    return checks


# ---------------------------------------------------------------- ledger and retention

def ledger_entries(custody_root: Path) -> list[dict]:
    path = custody_root / LEDGER
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_ledger(custody_root: Path, entry: dict) -> None:
    path = custody_root / LEDGER
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def backup_status(entries: list[dict]) -> dict[str, dict]:
    """Latest status per file plus its creation record."""
    files: dict[str, dict] = {}
    for entry in entries:
        record = files.setdefault(entry["file"], {"file": entry["file"], "root": entry["root"]})
        if entry["event"] == "created":
            record.update({k: entry[k] for k in ("ciphertext_sha256", "state_sha256", "serial",
                                                 "lineage", "created_utc")})
        record["status"] = entry["event"]
    return files


def retention_plan(entries: list[dict], now: datetime, keep: int = RETAIN_COUNT,
                   max_age_days: int = RETAIN_DAYS) -> dict[str, dict]:
    """Per root: the newest verified backup is always kept, regardless of age; up to ``keep``
    of the most recent verified backups are kept while younger than ``max_age_days``. Other
    verified backups may be deleted. Unverified backups never count and are never kept as a
    substitute; they are listed for investigation."""
    plan: dict[str, dict] = {}
    records = backup_status(entries).values()
    for record in records:
        root = plan.setdefault(record["root"], {"keep": [], "delete": [], "unverified": [],
                                                "warnings": []})
        if record["status"] == "remote-deleted":
            continue
        if record["status"] != "verified":
            root["unverified"].append(record["file"])
    for name, root in plan.items():
        verified = sorted((r for r in records
                           if r["root"] == name and r["status"] == "verified"),
                          key=lambda r: r["created_utc"], reverse=True)
        for index, record in enumerate(verified):
            age = now - datetime.strptime(record["created_utc"], STAMP).replace(tzinfo=UTC)
            if index == 0 or (index < keep and age <= timedelta(days=max_age_days)):
                root["keep"].append(record["file"])
            else:
                root["delete"].append(record["file"])
        if not verified:
            root["warnings"].append("no verified backup: state has a single copy")
        elif now - datetime.strptime(verified[0]["created_utc"], STAMP).replace(tzinfo=UTC) \
                > timedelta(days=max_age_days):
            root["warnings"].append(f"newest verified backup is older than {max_age_days} days; "
                                    "kept regardless of age")
    return plan


# ---------------------------------------------------------------- guarded file operations

def write_new(path: Path, data: bytes) -> None:
    """Write via an exclusive partial file and a no-clobber hard link; never overwrite."""
    partial = path.with_name(path.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(partial, path)  # FileExistsError if a backup with this name already exists
    finally:
        partial.unlink(missing_ok=True)


def custody_problems(custody_root: Path, home: Path, repo: Path = custody.REPO,
                     need_filevault: bool = True) -> list[str]:
    problems = custody.path_violations(custody_root, home, repo)
    if not custody_root.is_dir():
        return problems + ["custody root does not exist (approval gate 1)"]
    for rel in ("", "backups", *BACKUP_DIRS):
        directory = custody_root / rel
        if not directory.is_dir():
            problems.append(f"missing custody directory {rel or '.'}")
        else:
            problems += custody.mode_problems(directory, True)
    if need_filevault and custody.filevault_for(custody_root) is not True:
        problems.append("FileVault not confirmed for the custody volume")
    return problems


def refuse_unsafe_session(env: dict[str, str], stdin_tty: bool) -> list[str]:
    problems = []
    if env.get("CLAUDECODE"):
        problems.append("running inside a Claude Code session: use a plain Terminal so state "
                        "and passphrases never reach an assistant")
    if not stdin_tty:
        problems.append("no interactive terminal: the passphrase must be typed or pasted")
    return problems


def read_stable_state(path: Path) -> bytes:
    """Read the live state twice and refuse while a lock or a concurrent write is present."""
    if (path.parent / ".terraform.tfstate.lock.info").exists():
        raise BackupError("state lock info present: a Terraform run may be in progress")
    if not path.is_file():
        raise BackupError("state file not found in custody")
    problems = custody.mode_problems(path, False)
    if problems:
        raise BackupError("; ".join(problems))
    first = path.read_bytes()
    if path.read_bytes() != first:
        raise BackupError("state changed while being read")
    return first


def _git_commit(repo: Path = custody.REPO) -> str | None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                            text=True)
    return result.stdout.strip() or None


def create_backup(custody_root: Path, root: str, passphrase: str, *, now: datetime,
                  kdf: dict | None = None, git_commit: str | None = None,
                  prefix: str = "") -> dict:
    """Encrypt the custody state for ``root`` into outbox/, verify it, record it."""
    state_path = custody_root / "state" / root / "terraform.tfstate"
    state_bytes = read_stable_state(state_path)
    created = now.strftime(STAMP)
    manifest = build_manifest(state_bytes, root, created, git_commit)
    name = backup_name(root, created, manifest["serial"], prefix)
    prior = [r for r in backup_status(ledger_entries(custody_root)).values() if r["root"] == root]
    lineages = {r["lineage"] for r in prior if "lineage" in r}
    if lineages and manifest["lineage"] not in lineages:
        raise BackupError("state lineage differs from earlier backups of this root")
    serials = [r["serial"] for r in prior if r.get("status") == "verified"]
    if serials and manifest["serial"] < max(serials):
        raise BackupError("state serial is lower than the newest verified backup "
                          "(rollback needs an explicit recovery approval)")
    blob = encrypt_backup(state_bytes, manifest, passphrase, root=root, created=created, kdf=kdf)
    header, checked, restored = decrypt_backup(blob, passphrase)  # verify-after-write
    if restored != state_bytes or checked != manifest:
        raise BackupError("verify-after-write failed")
    target = custody_root / "backups/outbox" / name
    write_new(target, blob)
    if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(blob).digest():
        target.unlink()
        raise BackupError("written backup differs from the encrypted bytes")
    record = {"event": "created", "file": name, "root": root, "at": now.strftime(STAMP),
              "created_utc": created, "ciphertext_sha256": hashlib.sha256(blob).hexdigest(),
              "state_sha256": manifest["sha256"], "serial": manifest["serial"],
              "lineage": manifest["lineage"], "size": len(blob)}
    append_ledger(custody_root, record)
    return record


def verify_download(custody_root: Path, root: str, name: str, passphrase: str, *,
                    now: datetime, keep_plaintext: bool = False, prefix: str = "") -> dict:
    """Byte-compare a re-downloaded backup with the ledger, decrypt and validate it in a private
    recovery directory, then remove the plaintext unless ``keep_plaintext`` (approved recovery)."""
    if not name_pattern(prefix).match(name) or Path(name).name != name:
        raise BackupError("file name is not an M6M backup name")
    downloaded = custody_root / "backups/inbox" / name
    if not downloaded.is_file() or downloaded.is_symlink():
        raise BackupError("downloaded backup not found in backups/inbox")
    blob = downloaded.read_bytes()
    created = backup_status(ledger_entries(custody_root)).get(name, {})
    report: dict = {"file": name, "root": root, "size": len(blob),
                    "ciphertext_sha256": hashlib.sha256(blob).hexdigest()}
    report["ledger_match"] = (created.get("ciphertext_sha256") == report["ciphertext_sha256"]
                              if created else None)
    recovery = Path(tempfile.mkdtemp(prefix="restore-", dir=custody_root / "recovery"))
    try:
        try:
            header, manifest, state_bytes = decrypt_backup(blob, passphrase)
            report["authenticated"] = True
        except BackupError as exc:
            report.update(authenticated=False, error=str(exc))
            header = manifest = state_bytes = None
        if state_bytes is not None:
            restored = recovery / "terraform.tfstate"
            custody.write_private(restored, state_bytes)
            try:
                report.update(validate_restored(header, manifest, restored.read_bytes(),
                                                name=name, root=root, prefix=prefix))
            except BackupError as exc:
                report.update(structure_ok=False, error=str(exc))
            report["restored_mode"] = oct(stat.S_IMODE(restored.stat().st_mode))
            if created and "lineage" in created:
                report["ledger_state_match"] = (created["state_sha256"] == manifest["sha256"]
                                                and created["lineage"] == manifest["lineage"])
        checks = [v for k, v in report.items() if k.endswith(("_ok", "_match"))]
        report["verified"] = bool(report.get("authenticated")) and report["ledger_match"] is True \
            and all(checks) and report.get("restored_mode") == "0o600"
    finally:
        if keep_plaintext and report.get("authenticated"):
            report["recovery_dir"] = str(recovery)
        else:
            shutil.rmtree(recovery, ignore_errors=True)
    if created:  # disaster recovery without a ledger records nothing
        append_ledger(custody_root, {"event": "verified" if report["verified"] else
                                     "verify-failed", "file": name, "root": root,
                                     "at": now.strftime(STAMP)})
    return report


def mark_deleted(custody_root: Path, name: str, *, now: datetime) -> None:
    status = backup_status(ledger_entries(custody_root)).get(name)
    if not status:
        raise BackupError("file not in ledger")
    plan = retention_plan(ledger_entries(custody_root), now).get(status["root"], {})
    if name in plan.get("keep", []):
        raise BackupError("backup is retained by policy; refusing to record deletion")
    append_ledger(custody_root, {"event": "remote-deleted", "file": name,
                                 "root": status["root"], "at": now.strftime(STAMP)})


# ---------------------------------------------------------------- synthetic drill

def fixture_state(root: str, serial: int, lineage: str | None = None) -> dict:
    return {"version": 4, "terraform_version": "1.16.5", "serial": serial,
            "lineage": lineage or f"m6m-fixture-{root}-{uuid.uuid4()}",
            "outputs": {"fixture_marker": {
                "value": f"M6M disposable {root} backup fixture; not real infrastructure",
                "type": "string"}},
            "resources": [], "check_results": None}


def make_fake_custody(home: Path) -> Path:
    root = home / custody.CUSTODY_ROOT
    for rel in ("state/bootstrap", "state/pilot-dev", *BACKUP_DIRS):
        (root / rel).mkdir(parents=True, mode=0o700, exist_ok=True)
    for directory in [root, *root.rglob("*")]:
        os.chmod(directory, 0o700)
    os.chmod(home, 0o700)
    return root


def run_drill(workdir: str | None = None, kdf: dict | None = None) -> dict:
    """Synthetic end-to-end: two roots, create, simulated upload/download, verify, failures,
    retention and cleanup. The 'OneDrive' is a local directory; nothing leaves the machine."""
    if workdir and (custody.REPO in Path(workdir).resolve().parents
                    or Path(workdir).resolve() == custody.REPO):
        raise BackupError("drill workdir must be outside the repository")
    old = os.umask(0o077)
    base = Path(tempfile.mkdtemp(prefix="m6m-drill-", dir=workdir))
    passphrase = secrets.token_urlsafe(32)  # disposable; never printed or persisted
    results: dict = {}
    try:
        home = base / "home"
        home.mkdir(mode=0o700)
        root_dir = make_fake_custody(home)
        results["custody_problems"] = custody_problems(root_dir, home, need_filevault=False)
        onedrive = base / "simulated-onedrive"
        onedrive.mkdir(mode=0o700)
        t0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
        created = {}
        for offset, root in enumerate(("bootstrap", "pilot-dev")):
            state = root_dir / "state" / root / "terraform.tfstate"
            custody.write_private(state, json.dumps(fixture_state(root, 5 + offset)).encode())
            created[root] = create_backup(root_dir, root, passphrase, kdf=kdf,
                                          now=t0 + timedelta(seconds=offset), git_commit=None)
        results["created"] = sorted(created[r]["file"] for r in created)
        outbox = root_dir / "backups/outbox"
        plaintext_leaks = []
        for item in outbox.iterdir():
            data = item.read_bytes()
            if b"lineage" in data or b"fixture_marker" in data or b"m6m-fixture" in data:
                plaintext_leaks.append(item.name)
        results["ciphertext_contains_no_plaintext"] = not plaintext_leaks
        results["outbox_modes"] = sorted({oct(stat.S_IMODE(p.stat().st_mode))
                                          for p in outbox.iterdir()})
        # Simulated browser upload, then download back into the inbox.
        for item in outbox.iterdir():
            shutil.copyfile(item, onedrive / item.name)
            shutil.copyfile(onedrive / item.name, root_dir / "backups/inbox" / item.name)
        for root in created:
            (root_dir / "state" / root / "terraform.tfstate").unlink()  # simulate state loss
        reports = {root: verify_download(root_dir, root, created[root]["file"], passphrase,
                                         now=t0 + timedelta(minutes=5)) for root in created}
        results["verified"] = {root: reports[root]["verified"] for root in reports}
        results["recovery_dir_empty"] = not any((root_dir / "recovery").iterdir())

        name = created["bootstrap"]["file"]
        inbox = root_dir / "backups/inbox" / name
        good = inbox.read_bytes()
        negative = {}
        negative["wrong_passphrase"] = verify_download(
            root_dir, "bootstrap", name, secrets.token_urlsafe(32), now=t0)["verified"]
        flipped = bytearray(good)
        flipped[-20] ^= 0x01
        inbox.unlink()
        inbox.write_bytes(bytes(flipped))
        negative["bit_flip"] = verify_download(root_dir, "bootstrap", name, passphrase,
                                               now=t0)["verified"]
        inbox.unlink()
        inbox.write_bytes(good[:-100])
        negative["truncated"] = verify_download(root_dir, "bootstrap", name, passphrase,
                                                now=t0)["verified"]
        header, aad, body = read_header(good)
        forged = json.dumps({**header, "root": "pilot-dev"}, sort_keys=True,
                            separators=(",", ":")).encode()
        inbox.unlink()
        inbox.write_bytes(MAGIC + len(forged).to_bytes(4, "big") + forged + body)
        negative["header_root_swapped"] = verify_download(root_dir, "bootstrap", name,
                                                          passphrase, now=t0)["verified"]
        inbox.unlink()
        inbox.write_bytes(good)
        negative["restored_as_other_root"] = verify_download(root_dir, "pilot-dev", name,
                                                             passphrase, now=t0)["verified"]
        results["negative_cases_all_rejected"] = not any(negative.values())
        results["negative_cases"] = negative
        # A later successful verification restores 'verified' status after the failures.
        results["reverified_after_failures"] = verify_download(
            root_dir, "bootstrap", name, passphrase, now=t0 + timedelta(minutes=10))["verified"]

        if shutil.which("terraform"):
            report = verify_download(root_dir, "pilot-dev", created["pilot-dev"]["file"],
                                     passphrase, now=t0 + timedelta(minutes=11),
                                     keep_plaintext=True)
            restored = Path(report["recovery_dir"]) / "terraform.tfstate"
            empty = base / "tf"
            empty.mkdir(mode=0o700)
            show = subprocess.run(["terraform", f"-chdir={empty}", "show", "-json",
                                   str(restored)], capture_output=True, text=True,
                                  env={**os.environ, "TF_IN_AUTOMATION": "1",
                                       "CHECKPOINT_DISABLE": "1"})
            parsed = json.loads(show.stdout) if show.returncode == 0 else {}
            results["terraform_show_parses"] = parsed.get("values", {}).get("outputs", {}) \
                .get("fixture_marker", {}).get("value", "").startswith("M6M disposable")
            shutil.rmtree(report["recovery_dir"])
        results["retention"] = retention_plan(ledger_entries(root_dir),
                                              t0 + timedelta(minutes=20))
        results["stray_state_under_repository"] = custody.stray_state_paths()
    finally:
        shutil.rmtree(base, ignore_errors=True)
        os.umask(old)
        results["workdir_removed"] = not base.exists()
    ok = (not results.get("custody_problems") and results.get("ciphertext_contains_no_plaintext")
          and results.get("outbox_modes") == ["0o600"]
          and all(results.get("verified", {}).values()) and len(results.get("verified", {})) == 2
          and results.get("recovery_dir_empty") and results.get("negative_cases_all_rejected")
          and results.get("reverified_after_failures") and results.get("workdir_removed")
          and results.get("terraform_show_parses", True)
          and results.get("stray_state_under_repository") == [])
    results["ok"] = bool(ok)
    return results


# ---------------------------------------------------------------- M6N synthetic browser drill
# Disposable directory with the same internal layout as custody, so the production create and
# verify code paths are exercised unchanged. It is not the custody tree and holds no real state.

DRILL_ROOT = "bootstrap"  # the drill fixture's root; the cross-root check uses the other one
DRILL_OTHER_ROOT = "pilot-dev"
DRILL_MARKER_TEXT = (
    "M6N SYNTHETIC ONEDRIVE DRILL. Contains only a fake Terraform state fixture with zero\n"
    "resources and no credentials. Not the custody tree. Delete this directory after the drill.\n")


def drill_dir_problems(drill_dir: Path, home: Path, repo: Path = custody.REPO, *,
                       must_exist: bool) -> list[str]:
    problems = custody.path_violations(drill_dir, home, repo)
    if (home / ".secure") in drill_dir.resolve().parents or drill_dir.resolve() == home / ".secure":
        problems.append("inside the custody area ~/.secure (drill must stay separate)")
    if must_exist:
        if not (drill_dir / DRILL_MARKER).is_file():
            problems.append("not an M6N drill directory (marker missing; run rehearse-create)")
        else:
            for rel in ("", "state", f"state/{DRILL_ROOT}", "backups", *BACKUP_DIRS):
                if (drill_dir / rel).is_dir():
                    problems += custody.mode_problems(drill_dir / rel, True)
                else:
                    problems.append(f"missing drill directory {rel or '.'}")
    elif drill_dir.exists():
        problems.append("drill directory already exists (delete it or choose another path)")
    return problems


def drill_fixture() -> dict:
    doc = fixture_state(DRILL_ROOT, 1, f"m6n-fixture-{DRILL_ROOT}-{uuid.uuid4()}")
    doc["outputs"]["fixture_marker"]["value"] = (
        "M6N synthetic OneDrive drill fixture; not real infrastructure")
    return doc


def rehearse_create(drill_dir: Path, passphrase: str, *, now: datetime,
                    kdf: dict | None = None) -> dict:
    """Create the drill directory, write a synthetic fixture, encrypt it with the production
    code path, then delete the plaintext fixture (simulated loss). Returns non-secret metadata."""
    drill_dir.mkdir(mode=0o700)  # exclusive: refuses an existing directory
    os.chmod(drill_dir, 0o700)
    (drill_dir / DRILL_MARKER).write_text(DRILL_MARKER_TEXT)
    for rel in ("state", f"state/{DRILL_ROOT}", "backups", *BACKUP_DIRS):
        (drill_dir / rel).mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(drill_dir / rel, 0o700)
    state = drill_dir / "state" / DRILL_ROOT / "terraform.tfstate"
    try:
        custody.write_private(state, json.dumps(drill_fixture(), indent=2).encode())
        record = create_backup(drill_dir, DRILL_ROOT, passphrase, now=now, kdf=kdf,
                               git_commit=None, prefix=DRILL_PREFIX)
    except BaseException:
        shutil.rmtree(drill_dir, ignore_errors=True)  # created above by this call only
        raise
    state.unlink()  # only the ciphertext and the ledger hash remain
    return {**record, "path": str(drill_dir / "backups/outbox" / record["file"])}


def rehearse_verify(drill_dir: Path, downloaded: Path, passphrase: str, *, now: datetime,
                    terraform: bool = True) -> dict:
    """Verify the operator's browser round trip. Never prints or stores the passphrase."""
    created = [r for r in backup_status(ledger_entries(drill_dir)).values()
               if r.get("ciphertext_sha256")]
    if len(created) != 1:
        raise BackupError("drill ledger must contain exactly one created backup")
    expected = created[0]
    if downloaded.is_symlink() or not downloaded.is_file():
        raise BackupError("downloaded file not found (or is a symlink)")
    if downloaded.stat().st_size > MAX_STATE_BYTES:
        raise BackupError("downloaded file is too large")
    data = downloaded.read_bytes()
    report: dict = {"file": expected["file"], "downloaded_name": downloaded.name,
                    "downloaded_name_matches": downloaded.name == expected["file"],
                    "size": len(data), "expected_size": expected.get("size"),
                    "ciphertext_sha256": hashlib.sha256(data).hexdigest(),
                    "byte_identical_round_trip":
                        hashlib.sha256(data).hexdigest() == expected["ciphertext_sha256"]}
    inbox = drill_dir / "backups/inbox" / expected["file"]
    inbox.unlink(missing_ok=True)
    custody.write_private(inbox, data)
    other = verify_download(drill_dir, DRILL_OTHER_ROOT, expected["file"], passphrase, now=now,
                            prefix=DRILL_PREFIX)
    report["cross_root_restore_rejected"] = other["verified"] is False and \
        other.get("root_ok") is False
    wrong = verify_download(drill_dir, DRILL_ROOT, expected["file"], secrets.token_urlsafe(32),
                            now=now, prefix=DRILL_PREFIX)
    report["wrong_passphrase_rejected"] = wrong.get("authenticated") is False
    final = verify_download(drill_dir, DRILL_ROOT, expected["file"], passphrase, now=now,
                            keep_plaintext=True, prefix=DRILL_PREFIX)
    report["operator_passphrase_authenticated"] = bool(final.get("authenticated"))
    report["verify"] = {k: v for k, v in final.items() if k.endswith(("_ok", "_match"))
                        or k in ("verified", "restored_mode")}
    if final.get("recovery_dir"):
        restored = Path(final["recovery_dir"]) / "terraform.tfstate"
        doc = json.loads(restored.read_bytes())
        report["recovered_fixture"] = {
            "sha256_matches_ledger":
                hashlib.sha256(restored.read_bytes()).hexdigest() == expected["state_sha256"],
            "synthetic_lineage": doc["lineage"].startswith(f"m6n-fixture-{DRILL_ROOT}-"),
            "zero_resources": doc["resources"] == [],
            "marker": doc["outputs"]["fixture_marker"]["value"].startswith("M6N synthetic"),
            "serial": doc["serial"]}
        report["recovery_path"] = str(restored)
        if terraform and shutil.which("terraform"):
            empty = Path(tempfile.mkdtemp(prefix="tf-show-", dir=drill_dir / "recovery"))
            show = subprocess.run(["terraform", f"-chdir={empty}", "show", "-json",
                                   str(restored)], capture_output=True, text=True,
                                  env={**os.environ, "TF_IN_AUTOMATION": "1",
                                       "CHECKPOINT_DISABLE": "1"})
            parsed = json.loads(show.stdout) if show.returncode == 0 else {}
            report["terraform_show_parses"] = parsed.get("values", {}).get("outputs", {}) \
                .get("fixture_marker", {}).get("value", "").startswith("M6N synthetic")
            empty.rmdir()
    report["ledger_status"] = backup_status(ledger_entries(drill_dir))[expected["file"]]["status"]
    fixture = report.get("recovered_fixture", {})
    report["ok"] = bool(report["byte_identical_round_trip"] and report["downloaded_name_matches"]
                        and report["cross_root_restore_rejected"]
                        and report["wrong_passphrase_rejected"]
                        and report["operator_passphrase_authenticated"]
                        and report["verify"].get("verified") and fixture
                        and all(v for k, v in fixture.items() if k != "serial")
                        and report.get("terraform_show_parses", True)
                        and report["ledger_status"] == "verified")
    evidence = drill_dir / f"evidence-{now.strftime(STAMP)}.json"
    custody.write_private(evidence, json.dumps(report, indent=2).encode())
    report["evidence_path"] = str(evidence)
    return report


def _operator_session(drill_dir: Path, *, must_exist: bool) -> None:
    problems = refuse_unsafe_session(dict(os.environ), sys.stdin.isatty())
    problems += drill_dir_problems(drill_dir, Path.home(), must_exist=must_exist)
    if custody.filevault_for(Path.home()) is not True:
        problems.append("FileVault not confirmed for the home volume")
    if problems:
        raise BackupError("; ".join(problems))
    os.umask(0o077)


# ---------------------------------------------------------------- CLI

def _passphrase(confirm: bool) -> str:
    first = getpass.getpass("Backup passphrase (from the password manager): ")
    if confirm and getpass.getpass("Repeat passphrase: ") != first:
        raise BackupError("passphrases do not match")
    return first


def _real_custody(args_root: str | None = None) -> Path:
    problems = refuse_unsafe_session(dict(os.environ), sys.stdin.isatty())
    home = Path.home()
    custody_root = home / custody.CUSTODY_ROOT
    problems += custody_problems(custody_root, home)
    if problems:
        raise BackupError("; ".join(problems))
    os.umask(0o077)
    return custody_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--root", choices=sorted(custody.ROOTS), required=True)
    verify = sub.add_parser("verify-download")
    verify.add_argument("--root", choices=sorted(custody.ROOTS), required=True)
    verify.add_argument("--file", required=True, help="Backup file name in backups/inbox")
    verify.add_argument("--keep-plaintext", action="store_true",
                        help="Leave the restored state in recovery/ (approved recovery only)")
    retention = sub.add_parser("retention")
    retention.add_argument("--root", choices=sorted(custody.ROOTS), default=None)
    deleted = sub.add_parser("mark-deleted")
    deleted.add_argument("--file", required=True)
    drill = sub.add_parser("drill")
    drill.add_argument("--workdir", default=None, help="Parent for the disposable directory")
    default_drill = str(Path.home() / DRILL_DIR_NAME)
    rehearse = sub.add_parser("rehearse-create")
    rehearse.add_argument("--drill-dir", default=default_drill)
    rverify = sub.add_parser("rehearse-verify")
    rverify.add_argument("--drill-dir", default=default_drill)
    rverify.add_argument("--downloaded", required=True,
                         help="Path of the file downloaded back from OneDrive")
    args = parser.parse_args(argv)
    now = datetime.now(UTC).replace(microsecond=0)
    try:
        if args.command == "rehearse-create":
            drill_dir = Path(args.drill_dir).expanduser()
            _operator_session(drill_dir, must_exist=False)
            record = rehearse_create(drill_dir, _passphrase(confirm=True), now=now)
            print(json.dumps({k: record[k] for k in ("path", "file", "size",
                                                     "ciphertext_sha256")}, indent=2))
            return 0
        if args.command == "rehearse-verify":
            drill_dir = Path(args.drill_dir).expanduser()
            _operator_session(drill_dir, must_exist=True)
            report = rehearse_verify(drill_dir, Path(args.downloaded).expanduser(),
                                     _passphrase(confirm=False), now=now)
            print(json.dumps(report, indent=2))
            return 0 if report["ok"] else 1
        if args.command == "drill":
            result = run_drill(args.workdir)
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        if args.command in ("retention", "mark-deleted"):  # ledger metadata only
            custody_root = Path.home() / custody.CUSTODY_ROOT
            if problems := custody_problems(custody_root, Path.home(), need_filevault=False):
                raise BackupError("; ".join(problems))
            if args.command == "mark-deleted":
                mark_deleted(custody_root, args.file, now=now)
                return 0
            plan = retention_plan(ledger_entries(custody_root), now)
            print(json.dumps(plan if not args.root else plan.get(args.root, {}), indent=2))
            return 0
        custody_root = _real_custody()
        if args.command == "create":
            record = create_backup(custody_root, args.root, _passphrase(confirm=True), now=now,
                                   git_commit=_git_commit())
            print(json.dumps({k: record[k] for k in ("file", "size", "ciphertext_sha256",
                                                     "serial")}, indent=2))
            return 0
        report = verify_download(custody_root, args.root, args.file,
                                 _passphrase(confirm=False), now=now,
                                 keep_plaintext=args.keep_plaintext)
        print(json.dumps(report, indent=2))
        return 0 if report["verified"] else 1
    except (BackupError, FileExistsError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
