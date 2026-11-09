#!/usr/bin/env python3
"""M6L Terraform state custody tooling. Standard library only; never contacts a cloud API.

Subcommands:
  check   Read-only check of the proposed state layout (location, encryption, permissions,
          stray state under the repository). With --root, also the Terraform preflight:
          TF_DATA_DIR, backend cache path/workspace_dir, TF_WORKSPACE and CHECKPOINT_DISABLE.
  staged  Refuse staged Git files that look like state, plans, inputs, keys or tokens
          (intended for an optional pre-commit hook; installing it is a separate approval).
  drill   Backup/restore rehearsal using a disposable fixture and a disposable encrypted
          disk image under a temporary directory. Never reads or writes real state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROJECT_ID = "ebank-pilot-dev-261031-k7m4"
# Relative to $HOME so no username is committed.
CUSTODY_ROOT = Path(".secure") / PROJECT_ID
LAYOUT = {
    "state/bootstrap": "terraform.tfstate",
    "state/pilot-dev": "terraform.tfstate",
    "plans/bootstrap": None,
    "plans/pilot-dev": None,
    "tfdata/bootstrap": None,
    "tfdata/pilot-dev": None,
    "inputs": None,
    "logs": None,
    # M6M: ciphertext-only staging for the manual OneDrive upload/download, and a private
    # directory for restore tests (scripts/state_backup.py). Created only at gate 1.
    "backups": None,
    "backups/outbox": None,
    "backups/inbox": None,
    "recovery": None,
}
# Terraform root directories (relative to the repository) keyed by custody root name.
ROOTS = {"bootstrap": Path("infrastructure/terraform/bootstrap"),
         "pilot-dev": Path("infrastructure/terraform")}
STRAY_STATE = re.compile(r"(\.tfstate$|\.tfstate\.|^terraform\.tfstate\.d$|\.tfplan$|"
                         r"^\.terraform\.tfstate\.lock\.info$)")
# Locations synchronised or indexed by macOS services that must never hold state.
FORBIDDEN_HOME_PREFIXES = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents",
                           "Library/CloudStorage")

BLOCKED_NAMES = re.compile(
    r"(\.tfstate(\..*)?$|\.tfplan(\.json)?$|\.tfvars(\.json)?$|(^|/)crash(\..*)?\.log$|"
    r"\.sparseimage$|\.sparsebundle(/|$)|\.dmg$|\.age$|\.gpg$|\.pem$|\.p12$|"
    r"(^|/)application_default_credentials\.json$|(^|/)\.terraformrc$|(^|/)terraform\.rc$)"
)
CONTENT_RULES = {
    "oauth_access_token": re.compile(rb"ya29\.[A-Za-z0-9_\-]{20,}"),
    "private_key_block": re.compile(rb"-----BEGIN (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----"),
    "refresh_token_field": re.compile(rb'"refresh_token"\s*:\s*"[^"]{8,}'),
    "service_account_key": re.compile(rb'"private_key_id"\s*:\s*"[0-9a-f]{16,}'),
}
STATE_MARKERS = (re.compile(rb'"terraform_version"\s*:\s*"'), re.compile(rb'"lineage"\s*:\s*"'))


# ---------------------------------------------------------------- path policy

def path_violations(target: Path, home: Path, repo: Path = REPO) -> list[str]:
    """Policy violations for a proposed custody path (pure; no filesystem writes)."""
    target, home, repo = target.resolve(), home.resolve(), repo.resolve()
    problems = []
    if target == repo or repo in target.parents:
        problems.append("inside the Git repository")
    if home not in target.parents:
        problems.append("outside the operator home directory")
    else:
        relative = target.relative_to(home).as_posix()
        for prefix in FORBIDDEN_HOME_PREFIXES:
            if relative == prefix or relative.startswith(prefix + "/"):
                problems.append(f"under synchronised/indexed location ~/{prefix}")
    return problems


def mode_problems(path: Path, expect_dir: bool) -> list[str]:
    info = path.lstat()
    problems = []
    if stat.S_ISLNK(info.st_mode):
        return [f"{path.name} is a symlink"]
    if info.st_uid != os.getuid():
        problems.append(f"{path.name} not owned by operator")
    want = 0o700 if expect_dir else 0o600
    if stat.S_IMODE(info.st_mode) & ~want:
        problems.append(f"{path.name} mode {oct(stat.S_IMODE(info.st_mode))} exceeds {oct(want)}")
    return problems


def filevault_for(path: Path) -> bool | None:
    """FileVault status of the volume holding ``path`` (diskutil needs the mount point)."""
    try:
        df = subprocess.run(["df", "-P", str(path)], capture_output=True, text=True,
                            check=True).stdout.splitlines()[-1]
        mount_point = df.split(None, 5)[5]
        out = subprocess.run(["diskutil", "info", "-plist", mount_point], capture_output=True,
                             check=True).stdout
        return bool(plistlib.loads(out).get("FileVault"))
    except (OSError, IndexError, subprocess.CalledProcessError,
            plistlib.InvalidFileException):
        return None


# ---------------------------------------------------------------- backend preflight
# M6L probes showed the local backend fails closed when `init` is omitted or TF_DATA_DIR is
# forgotten ("Backend initialization required"), but fails OPEN when `init` runs without
# -backend-config (path defaults to ./terraform.tfstate) or when TF_WORKSPACE selects a
# workspace without workspace_dir (terraform.tfstate.d/ is created in the working directory).
# These checks catch the fail-open cases before any Terraform command.

def expected_backend(home: Path, root: str) -> dict[str, Path]:
    custody = home / CUSTODY_ROOT
    return {"tf_data_dir": custody / "tfdata" / root,
            "path": custody / "state" / root / "terraform.tfstate",
            "workspace_dir": custody / "state" / root / "workspaces"}


def _same(value: str | None, expected: Path) -> bool:
    return bool(value) and Path(value).is_absolute() and \
        Path(value).resolve() == expected.resolve()


def backend_cache_problems(tf_data_dir: Path, home: Path, root: str) -> list[str]:
    """Problems with the Terraform backend cache in ``tf_data_dir`` (read-only)."""
    want = expected_backend(home, root)
    problems = []
    if tf_data_dir.resolve() != want["tf_data_dir"].resolve():
        problems.append("TF_DATA_DIR is not the custody tfdata directory for this root")
    cache = tf_data_dir / "terraform.tfstate"
    if not cache.is_file():
        return problems + ["backend not initialised in TF_DATA_DIR"]
    backend = json.loads(cache.read_bytes()).get("backend") or {}
    config = backend.get("config") or {}
    if backend.get("type") != "local":
        problems.append("backend type is not local")
    if not _same(config.get("path"), want["path"]):
        problems.append("backend path is not the custody state file "
                        "(unset means ./terraform.tfstate in the working directory)")
    if not _same(config.get("workspace_dir"), want["workspace_dir"]):
        problems.append("backend workspace_dir is not in custody "
                        "(unset means ./terraform.tfstate.d in the working directory)")
    return problems


def environment_problems(env: dict[str, str]) -> list[str]:
    problems = []
    if env.get("TF_WORKSPACE", "default") not in ("", "default"):
        problems.append("TF_WORKSPACE selects a non-default workspace")
    if env.get("CHECKPOINT_DISABLE") != "1":
        problems.append("CHECKPOINT_DISABLE is not 1 (Terraform would contact HashiCorp)")
    return problems


def stray_state_paths(repo: Path = REPO) -> list[str]:
    """State, plan, lock or workspace artefacts anywhere under the repository, ignored or not."""
    found = []
    for path in repo.rglob("*"):
        relative = path.relative_to(repo)
        if relative.parts[0] == ".git" or "providers" in relative.parts:
            continue
        if STRAY_STATE.search(path.name):
            found.append(relative.as_posix())
    return sorted(found)


def cmd_check(args: argparse.Namespace) -> int:
    home = Path.home()
    root = home / CUSTODY_ROOT
    report = {"custody_root": f"~/{CUSTODY_ROOT.as_posix()}", "policy_violations":
              path_violations(root, home), "filevault": filevault_for(home),
              "root_exists": root.exists(), "umask": oct(_current_umask()), "mode_problems": [],
              "stray_state_under_repository": stray_state_paths(), "preflight_problems": []}
    if args.root:
        data_dir = Path(os.environ.get("TF_DATA_DIR") or REPO / ROOTS[args.root] / ".terraform")
        report["preflight_problems"] = (backend_cache_problems(data_dir, home, args.root)
                                        + environment_problems(dict(os.environ)))
    if root.exists():
        for rel in ("", *LAYOUT):
            directory = root / rel
            if directory.exists():
                report["mode_problems"] += mode_problems(directory, True)
                for child in directory.iterdir():
                    report["mode_problems"] += mode_problems(child, child.is_dir())
    report["umask_ok"] = (_current_umask() & 0o077) == 0o077  # run Terraform under umask 077
    print(json.dumps(report, indent=2))
    ok = (not report["policy_violations"] and report["filevault"] is True
          and not report["mode_problems"] and report["umask_ok"]
          and not report["stray_state_under_repository"] and not report["preflight_problems"])
    return 0 if ok else 1


def _current_umask() -> int:
    current = os.umask(0o077)
    os.umask(current)
    return current


# ---------------------------------------------------------------- staged guard

def blob_findings(name: str, data: bytes) -> list[str]:
    findings = []
    if BLOCKED_NAMES.search(name) and not name.endswith(".example"):
        findings.append("blocked file type")
    for rule, pattern in CONTENT_RULES.items():
        if pattern.search(data):
            findings.append(rule)
    if all(marker.search(data) for marker in STATE_MARKERS):
        findings.append("terraform_state_content")
    return findings


def cmd_staged(_: argparse.Namespace) -> int:
    names = subprocess.run(["git", "diff", "--cached", "--name-only", "-z",
                            "--diff-filter=ACMR"], cwd=REPO, capture_output=True,
                           check=True).stdout.split(b"\0")
    blocked = {}
    for raw in filter(None, names):
        name = raw.decode()
        data = subprocess.run(["git", "show", f":{name}"], cwd=REPO, capture_output=True,
                              check=True).stdout
        if findings := blob_findings(name, data):
            blocked[name] = findings
    for name, findings in blocked.items():
        print(f"BLOCKED {name}: {', '.join(findings)}", file=sys.stderr)  # never print content
    return 1 if blocked else 0


# ---------------------------------------------------------------- restore drill

def fixture_state(serial: int) -> dict:
    return {"version": 4, "terraform_version": "1.16.5", "serial": serial,
            "lineage": f"m6l-fixture-{uuid.uuid4()}",
            "outputs": {"fixture_marker": {
                "value": "M6L disposable restore fixture; not real infrastructure",
                "type": "string"}},
            "resources": [], "check_results": None}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def snapshot(state: Path, destination: Path) -> dict:
    """Copy a state file plus manifest into a fresh 0700 snapshot directory."""
    meta = json.loads(state.read_bytes())
    name = f"serial-{meta['serial']:06d}"
    target = destination / name
    target.mkdir(mode=0o700)
    write_private(target / "terraform.tfstate", state.read_bytes())
    manifest = {"sha256": sha256(state), "serial": meta["serial"], "lineage": meta["lineage"],
                "terraform_version": meta["terraform_version"]}
    write_private(target / "MANIFEST.json", json.dumps(manifest, indent=2).encode())
    return {"snapshot": name, **manifest}


def verify_snapshot(directory: Path) -> dict:
    manifest = json.loads((directory / "MANIFEST.json").read_bytes())
    state_file = directory / "terraform.tfstate"
    state = json.loads(state_file.read_bytes())
    return {"hash_ok": sha256(state_file) == manifest["sha256"],
            "lineage_ok": state["lineage"] == manifest["lineage"],
            "serial_ok": state["serial"] == manifest["serial"]}


def _hdiutil(*args: str, passphrase: str | None = None, check: bool = True):
    data = None if passphrase is None else passphrase.encode() + b"\0"
    return subprocess.run(["hdiutil", *args], input=data, capture_output=True, check=check)


def cmd_drill(args: argparse.Namespace) -> int:
    os.umask(0o077)
    base = Path(tempfile.mkdtemp(prefix="m6l-drill-", dir=args.workdir))
    image, mount = base / "fixture-backup.sparsebundle", base / "mnt"
    passphrase = secrets.token_urlsafe(32)  # disposable; never printed or persisted
    results: dict = {"workdir_mode": oct(stat.S_IMODE(base.stat().st_mode))}
    mounted = False
    try:
        live = base / "live"
        live.mkdir(mode=0o700)
        state = live / "terraform.tfstate"
        write_private(state, json.dumps(fixture_state(7), indent=2).encode())
        results["fixture_mode"] = oct(stat.S_IMODE(state.stat().st_mode))
        original_hash = sha256(state)

        _hdiutil("create", "-quiet", "-size", "20m", "-type", "SPARSEBUNDLE", "-fs", "APFS",
                 "-encryption", "AES-256", "-stdinpass", "-volname", "M6LDRILL", str(image),
                 passphrase=passphrase)
        mount.mkdir(mode=0o700)
        _hdiutil("attach", "-quiet", "-stdinpass", "-nobrowse", "-noautoopen",
                 "-mountpoint", str(mount), str(image), passphrase=passphrase)
        mounted = True
        results["backup"] = snapshot(state, mount)
        _hdiutil("detach", "-quiet", str(mount))
        mounted = False

        state.unlink()  # simulate loss of the live state
        wrong = _hdiutil("attach", "-quiet", "-stdinpass", "-nobrowse", "-noautoopen",
                         "-mountpoint", str(mount), str(image),
                         passphrase=secrets.token_urlsafe(32), check=False)
        results["wrong_passphrase_rejected"] = wrong.returncode != 0

        _hdiutil("attach", "-quiet", "-readonly", "-stdinpass", "-nobrowse", "-noautoopen",
                 "-mountpoint", str(mount), str(image), passphrase=passphrase)
        mounted = True
        snap = mount / results["backup"]["snapshot"]
        results["verify_in_backup"] = verify_snapshot(snap)
        restored = base / "restored"
        restored.mkdir(mode=0o700)
        write_private(restored / "terraform.tfstate", (snap / "terraform.tfstate").read_bytes())
        _hdiutil("detach", "-quiet", str(mount))
        mounted = False

        restored_state = restored / "terraform.tfstate"
        results["restored_hash_matches_original"] = sha256(restored_state) == original_hash
        results["restored_mode"] = oct(stat.S_IMODE(restored_state.stat().st_mode))

        tampered = base / "tampered"
        shutil.copytree(restored, tampered)
        (tampered / "MANIFEST.json").write_text(json.dumps(results["backup"]))
        doc = json.loads(restored_state.read_bytes())
        doc["serial"] += 1
        (tampered / "terraform.tfstate").write_text(json.dumps(doc))
        results["tamper_detected"] = not verify_snapshot(tampered)["hash_ok"]

        if shutil.which("terraform"):
            empty = base / "tf"
            empty.mkdir(mode=0o700)
            show = subprocess.run(["terraform", f"-chdir={empty}", "show", "-json",
                                   str(restored_state)], capture_output=True, text=True,
                                  env={**os.environ, "TF_IN_AUTOMATION": "1",
                                       "CHECKPOINT_DISABLE": "1"})
            parsed = json.loads(show.stdout) if show.returncode == 0 else {}
            results["terraform_show_parses"] = parsed.get("values", {}).get("outputs", {}) \
                .get("fixture_marker", {}).get("value", "").startswith("M6L disposable")
    finally:
        if mounted:
            _hdiutil("detach", "-force", "-quiet", str(mount), check=False)
        shutil.rmtree(base, ignore_errors=True)
        results["workdir_removed"] = not base.exists()
    print(json.dumps(results, indent=2))
    required = ("wrong_passphrase_rejected", "restored_hash_matches_original",
                "tamper_detected", "workdir_removed")
    ok = all(results.get(k) for k in required) and all(results["verify_in_backup"].values())
    ok = ok and results["restored_mode"] == results["fixture_mode"] == "0o600"
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--root", choices=sorted(ROOTS), default=None,
                       help="Also run the Terraform preflight for this root")
    check.set_defaults(func=cmd_check)
    sub.add_parser("staged").set_defaults(func=cmd_staged)
    drill = sub.add_parser("drill")
    drill.add_argument("--workdir", default=None, help="Parent for the disposable directory")
    drill.set_defaults(func=cmd_drill)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
