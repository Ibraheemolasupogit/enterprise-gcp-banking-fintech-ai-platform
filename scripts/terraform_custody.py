#!/usr/bin/env python3
"""M6O secure local Terraform backends: custody initialisation, fail-closed preflight and a
guarded Terraform wrapper. Standard library only; never contacts a cloud API by itself.

Both roots declare ``backend "local"`` with an impossible sentinel ``path``/``workspace_dir``
under ``/dev/null``. Any ``terraform init`` without the custody ``-backend-config`` therefore
fails closed (M6O sandbox probes), and repository-local state is never the default.

Subcommands (real use is operator-only: refused inside a Claude Code session):
  init-custody  Create ~/.secure/<project>/ with 0700 directories and a Spotlight marker.
                Idempotent; never creates state; refuses symlinks, loose modes, foreign owners
                and forbidden locations. Requires --confirm.
  preflight     Fail-closed checks for one root (--phase init|run). Read-only.
  run           Preflight, then run an allow-listed Terraform command for one root with the
                custody TF_DATA_DIR, backend config, umask 077 and CHECKPOINT_DISABLE=1.
                --dry-run prints the exact command and preflight result without running it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state_custody as custody  # noqa: E402  (sibling module; stdlib only)

SENTINEL_DIR = "/dev/null/ebank-custody-init-required"
SENTINEL = {"path": f"{SENTINEL_DIR}/terraform.tfstate",
            "workspace_dir": f"{SENTINEL_DIR}/workspaces"}
BACKEND_BLOCK = (
    '  backend "local" {\n'
    f'    path          = "{SENTINEL["path"]}"\n'
    f'    workspace_dir = "{SENTINEL["workspace_dir"]}"\n'
    "  }\n")
SPOTLIGHT_MARKER = ".metadata_never_index"
STATE_DIR_ALLOWED = {"terraform.tfstate", "terraform.tfstate.backup",
                     ".terraform.tfstate.lock.info", "workspaces"}

# Commands the wrapper will run. Anything else (destroy, import, state rm/mv/push, taint,
# force-unlock, workspace, console, refresh, ...) needs a reviewed wrapper change first,
# because those commands either mutate state outside a saved plan or need a fresh backup.
ALLOWED = {"init", "validate", "plan", "apply", "show", "output", "providers", "version"}
ALLOWED_STATE = {"list", "show"}
FORBIDDEN_FLAGS = ("-state", "-state-out", "-backup", "-backend-config", "-backend",
                   "-migrate-state",
                   "-force-copy", "-chdir", "-destroy", "-from-module", "-plugin-dir",
                   "-upgrade", "-lockfile")
SEPARATE_VALUE_FLAGS = ("-var-file", "-out")  # must use the -flag=value form
# M6S: partial or drift-hiding plans. -target/-replace narrow or force changes outside the
# reviewed configuration; -refresh-only adopts out-of-band changes into state. Refused in every
# form (=value, separate value, bare, -- prefix, any case). -refresh is allowed only as true.
PLAN_SCOPE_FLAGS = ("-target", "-replace", "-refresh-only")
# Name prefixes refused too, so future variants (e.g. -target-file, -exclude in newer Terraform)
# are refused before review rather than silently accepted.
PLAN_SCOPE_PREFIXES = ("-target", "-replace", "-exclude", "-refresh-only")
GO_TRUE = ("", "1", "t", "true")  # bare flag, then Go strconv.ParseBool true (lower-cased)
FORBIDDEN_ENV_PREFIXES = ("TF_CLI_ARGS", "TF_CLI_CONFIG_FILE", "TF_STATE", "TF_PLUGIN_CACHE_DIR")

# ---- M6P (P1): credential and provider-identity environment policy -------------------------
# The only approved credential is a short-lived operator token in GOOGLE_OAUTH_ACCESS_TOKEN, and
# only for commands that contact Google APIs. Everything that could choose a different identity,
# impersonate, load a key file, redirect quota, spoof the metadata server, proxy or re-root TLS
# is refused (never silently dropped), so a stray variable is visible to the operator.
TOKEN_VAR = "GOOGLE_OAUTH_ACCESS_TOKEN"
TOKEN_RE = re.compile(r"^ya29\.[A-Za-z0-9_\-.]{20,4096}$")  # gcloud user access token shape
NETWORK_COMMANDS = {"plan", "apply"}  # every other allowed command is offline
CREDENTIAL_ENV_PREFIXES = ("GOOGLE_", "CLOUDSDK_", "GCLOUD_", "GCE_METADATA_")
CREDENTIAL_ENV_EXACT = {"USER_PROJECT_OVERRIDE", "NO_GCE_CHECK", "HTTP_PROXY", "HTTPS_PROXY",
                        "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"}
CHILD_BASE_ENV = ("HOME", "PATH", "TERM", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER",
                  "LOGNAME")
CHILD_TF_ENV = ("TF_LOG", "TF_LOG_PATH", "TF_IN_AUTOMATION", "TF_INPUT")
# Terraform CLI configuration that could replace the provider binary or its source.
CLI_CONFIG_PATHS = (".terraformrc", "terraform.rc", ".terraform.d/plugins",
                    "Library/Application Support/io.terraform/plugins")
ADC_PATH = Path(".config/gcloud/application_default_credentials.json")
PROVIDER_IDENTITY_ATTRS = ("impersonate_service_account", "impersonate_service_account_delegates",
                           "credentials", "access_token", "billing_project",
                           "user_project_override")
PILOT_IMPERSONATION_RE = re.compile(
    r"^\s*impersonate_service_account\s*=\s*var\.provisioner_service_account\s*$", re.M)


class CustodyError(Exception):
    """A refusal. Messages never contain state, plan or credential contents."""


# ---------------------------------------------------------------- paths and policy

def custody_root(home: Path) -> Path:
    return home / custody.CUSTODY_ROOT


def required_dirs() -> list[str]:
    """Custody directories relative to the custody root, parents first ("" is the root)."""
    wanted = {""}
    for rel in [*custody.LAYOUT, *(f"state/{r}/workspaces" for r in custody.ROOTS)]:
        parts = Path(rel).parts
        wanted.update(Path(*parts[:i]).as_posix() for i in range(1, len(parts) + 1))
    return sorted(wanted, key=lambda rel: (rel.count("/") + bool(rel), rel))


def _inside_git_worktree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in [path, *path.parents])


def location_problems(home: Path, repo: Path) -> list[str]:
    """Custody path, its ancestry and the repository location (no filesystem writes)."""
    root = custody_root(home)
    problems = custody.path_violations(root, home, repo)
    for candidate in (home / ".secure", root):  # a symlink could redirect into iCloud or Git
        if candidate.is_symlink():
            problems.append(f"{candidate.name} is a symlink")
    if root.exists() and root.resolve() != home.resolve() / custody.CUSTODY_ROOT:
        problems.append("custody path resolves through a symlink")
    if _inside_git_worktree(root.resolve()):
        problems.append("custody root is inside a Git working tree")
    try:
        relative = repo.resolve().relative_to(home.resolve()).as_posix()
        if any(relative == p or relative.startswith(p + "/")
               for p in custody.FORBIDDEN_HOME_PREFIXES):
            problems.append("repository is inside a synchronised location (iCloud/Desktop)")
    except ValueError:
        pass
    return problems


def tree_problems(home: Path) -> list[str]:
    root = custody_root(home)
    if not root.is_dir():
        return ["custody tree not initialised (run init-custody)"]
    problems = []
    for rel in required_dirs():
        directory = root / rel
        if directory.is_symlink():
            problems.append(f"{rel or '.'} is a symlink")
        elif not directory.is_dir():
            problems.append(f"missing custody directory {rel or '.'}")
        else:
            problems += [f"{rel or '.'}: {p}" for p in custody.mode_problems(directory, True)]
            for child in directory.iterdir():
                if child.is_dir() and not child.is_symlink():
                    continue  # directories are checked in their own right or below
                problems += [f"{rel or '.'}/{p}" for p in
                             custody.mode_problems(child, child.is_dir())]
    if not (root / SPOTLIGHT_MARKER).is_file():
        problems.append("Spotlight marker missing")
    for name in custody.ROOTS:
        extra = {p.name for p in (root / "state" / name).iterdir()} - STATE_DIR_ALLOWED \
            if (root / "state" / name).is_dir() else set()
        if extra:
            problems.append(f"unexpected entries in state/{name}: {len(extra)}")
    return problems


def environment_problems(env: dict[str, str], home: Path, root: str) -> list[str]:
    problems = custody.environment_problems({**env, "CHECKPOINT_DISABLE": "1"})  # set by us
    for key in env:
        if key.startswith(FORBIDDEN_ENV_PREFIXES):
            problems.append(f"{key} is set (could redirect state or inject flags)")
    if env.get("TF_LOG"):
        log_path = env.get("TF_LOG_PATH")
        logs = custody_root(home) / "logs"
        if not log_path or logs.resolve() not in Path(log_path).resolve().parents:
            problems.append("TF_LOG is set without TF_LOG_PATH inside custody logs/")
    want = custody.expected_backend(home, root)["tf_data_dir"]
    if env.get("TF_DATA_DIR") and Path(env["TF_DATA_DIR"]).resolve() != want.resolve():
        problems.append("TF_DATA_DIR is set to another location (wrong root or repository)")
    return problems


def credential_problems(env: dict[str, str], home: Path, command: str | None) -> list[str]:
    """Credential/provider-identity policy for ``command`` (None: generic checks only).
    Messages name variables or paths only; a token value is never included."""
    problems = []
    for key in sorted(env):
        if key == TOKEN_VAR:
            continue
        if key.startswith(CREDENTIAL_ENV_PREFIXES) or key.upper() in CREDENTIAL_ENV_EXACT:
            problems.append(f"{key} is set (credential, identity, quota or network override; "
                            "unset it)")
    for rel in CLI_CONFIG_PATHS:
        if (home / rel).exists():
            problems.append(f"~/{rel} exists (Terraform CLI config could replace providers)")
    if command in NETWORK_COMMANDS:
        token = env.get(TOKEN_VAR)
        if not token:
            problems.append(f"{TOKEN_VAR} is required for {command} (operator token workflow)")
        elif not TOKEN_RE.match(token):
            problems.append(f"{TOKEN_VAR} is malformed (value not shown)")
        if env.get("TF_LOG"):
            problems.append("TF_LOG is not allowed with credentials (logs can capture headers)")
        if (home / ADC_PATH).exists():
            problems.append("an ADC file exists (~/.config/gcloud/application_default_credentials"
                            ".json); remove it, it is an unapproved fallback identity")
    elif command is not None and TOKEN_VAR in env:
        problems.append(f"{TOKEN_VAR} is set for offline command '{command}' (unset it)")
    return problems


PROVIDER_RE = re.compile(r'^provider\s+"google"\s*\{\n(.*?)^\}', re.M | re.S)
OVERRIDE_RE = re.compile(r"(^override\.tf$|_override\.tf$|\.tf\.json$)")


def provider_problems(root: str, repo: Path) -> list[str]:
    """The effective google provider configuration must match the root's identity design
    (ADR-003). Terraform merges every root *.tf file and applies override files last, so all of
    them are inspected; override and JSON configuration files are refused outright."""
    root_dir = repo / custody.ROOTS[root]
    files = sorted(root_dir.glob("*.tf")) + sorted(root_dir.glob("*.tf.json"))
    problems = [f"override/JSON configuration file present: {f.name}"
                for f in files if OVERRIDE_RE.search(f.name)]
    blocks = [b for f in files if f.suffix == ".tf" for b in PROVIDER_RE.findall(f.read_text())]
    modules = root_dir / "modules"
    nested = [f for f in modules.rglob("*.tf") if ".terraform" not in f.parts] \
        if modules.is_dir() else []
    if any(PROVIDER_RE.search(f.read_text()) for f in nested):
        problems.append("a module declares a google provider block")
    if len(blocks) != 1:
        return problems + [f"expected exactly one google provider block, found {len(blocks)}"]
    body = blocks[0]
    present = {a for a in PROVIDER_IDENTITY_ATTRS if re.search(rf"^\s*{a}\s*=", body, re.M)}
    if root == "bootstrap":
        return problems + [f"bootstrap provider sets {a} (must run as the operator only)"
                           for a in sorted(present)]
    problems += [f"pilot provider sets {a}" for a in sorted(present - {
        "impersonate_service_account"})]
    if not PILOT_IMPERSONATION_RE.search(body):
        problems.append("pilot provider must impersonate var.provisioner_service_account")
    return problems


def child_environment(env: dict[str, str], overrides: dict[str, str],
                      command: str) -> dict[str, str]:
    """Explicit allow-list for the Terraform process; the token only for network commands."""
    child = {k: env[k] for k in (*CHILD_BASE_ENV, *CHILD_TF_ENV) if k in env}
    child.update(overrides)
    if command in NETWORK_COMMANDS and env.get(TOKEN_VAR):
        child[TOKEN_VAR] = env[TOKEN_VAR]
    return child


def isolation_problems(home: Path, root: str) -> list[str]:
    """Each root's backend cache must point at that root's own custody paths only."""
    problems = []
    for name in custody.ROOTS:
        want = custody.expected_backend(home, name)
        cache = want["tf_data_dir"] / "terraform.tfstate"
        if cache.is_file():
            problems += [f"{name}: {p}" for p in
                         custody.backend_cache_problems(want["tf_data_dir"], home, name)]
    paths = [custody.expected_backend(home, n)["path"] for n in custody.ROOTS]
    if len(set(paths)) != len(paths):
        problems.append("roots share a state path")
    return problems


def preflight(home: Path, root: str, env: dict[str, str], *, phase: str,
              repo: Path = custody.REPO, require_filevault: bool = True,
              command: str | None = None) -> list[str]:
    """Every problem that must stop a Terraform command for ``root``. Empty means proceed."""
    if root not in custody.ROOTS:
        return [f"unknown root {root}"]
    problems = location_problems(home, repo) + tree_problems(home)
    problems += environment_problems(env, home, root) + isolation_problems(home, root)
    problems += credential_problems(env, home, command) + provider_problems(root, repo)
    stray = custody.stray_state_paths(repo)
    if stray:
        problems.append(f"state/plan/lock artefacts under the repository: {len(stray)}")
    main_tf = repo / custody.ROOTS[root] / "main.tf"
    if not main_tf.is_file() or BACKEND_BLOCK not in main_tf.read_text():
        problems.append("root does not declare the sentinel local backend")
    if require_filevault and custody.filevault_for(home) is not True:
        problems.append("FileVault not confirmed for the home volume")
    if phase == "run":
        want = custody.expected_backend(home, root)
        problems += custody.backend_cache_problems(want["tf_data_dir"], home, root)
    return problems


# ---------------------------------------------------------------- custody initialisation

def create_custody_tree(home: Path, repo: Path = custody.REPO) -> list[str]:
    """Create missing custody directories at 0700 and the Spotlight marker at 0600. Existing
    entries are verified, never loosened, re-permissioned or replaced. Creates no state."""
    if problems := location_problems(home, repo):
        raise CustodyError("; ".join(problems))
    old = os.umask(0o077)
    created = []
    try:
        secure = home / ".secure"
        for directory in [secure] + [custody_root(home) / rel for rel in required_dirs()]:
            if directory.is_symlink():
                raise CustodyError(f"{directory.name} is a symlink")
            if directory.exists():
                if not directory.is_dir():
                    raise CustodyError(f"{directory.name} exists and is not a directory")
                if problems := custody.mode_problems(directory, True):
                    raise CustodyError("; ".join(problems) + " (fix manually; not loosened)")
                continue
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
            created.append(directory.relative_to(home).as_posix())
        marker = custody_root(home) / SPOTLIGHT_MARKER
        if not marker.exists():
            custody.write_private(marker, b"")
            created.append(marker.relative_to(home).as_posix())
    finally:
        os.umask(old)
    return created


# ---------------------------------------------------------------- command policy

def _inside(path: str, directory: Path) -> bool:
    resolved = Path(path).expanduser().resolve()
    return directory.resolve() in resolved.parents


def build_command(root: str, args: list[str], home: Path, *,
                  repo: Path = custody.REPO) -> tuple[list[str], dict[str, str]]:
    """Validate ``args`` against the allow-list and return (argv, environment overrides)."""
    if not args or args[0].startswith("-"):
        raise CustodyError("give a Terraform subcommand first (global options are not allowed)")
    command, rest = args[0], args[1:]
    if command == "state":
        if not rest or rest[0] not in ALLOWED_STATE:
            raise CustodyError("only 'state list' and 'state show' are allowed")
    elif command not in ALLOWED:
        raise CustodyError(f"'{command}' is not allowed through the custody wrapper")
    # Go's flag parser accepts -flag and --flag, and bool values such as FALSE, f and 0.
    rest = [("-" + a.lstrip("-")) if a.startswith("--") else a for a in rest]
    for arg in rest:
        flag, sep, value = arg.partition("=")
        if flag == "-lock" and value.lower() not in ("", "true", "t", "1"):
            raise CustodyError("disabling state locking is not allowed")
        name = flag.lower()
        if name in PLAN_SCOPE_FLAGS or name.startswith(PLAN_SCOPE_PREFIXES):
            raise CustodyError(f"flag {name} is not allowed (plans must cover the whole "
                               "configuration with a normal refresh)")
        if name == "-refresh" and sep and value.lower() not in GO_TRUE[1:]:
            raise CustodyError("disabling refresh is not allowed (it hides out-of-band drift)")
        if flag in FORBIDDEN_FLAGS:
            raise CustodyError(f"flag {flag} is not allowed (custody controls state and backend)")
        if arg in SEPARATE_VALUE_FLAGS:
            raise CustodyError(f"use {arg}=<value> (separate-value form is not accepted)")
    custody_dir = custody_root(home)
    plans = custody_dir / "plans" / root
    for arg in rest:
        if arg.startswith("-var-file") and not _inside(arg.split("=", 1)[-1],
                                                       custody_dir / "inputs"):
            raise CustodyError("-var-file must be inside custody inputs/")
    positional = [a for a in rest if not a.startswith("-")]
    if command == "plan":
        outs = [a.split("=", 1)[1] for a in rest if a.startswith("-out=")]
        if len(outs) != 1 or not _inside(outs[0], plans):
            raise CustodyError(f"plan requires -out=<file> inside custody plans/{root}/")
    if command == "apply":
        if len(positional) != 1 or not _inside(positional[0], plans):
            raise CustodyError(f"apply requires one saved plan file from custody plans/{root}/")
    if command == "show" and positional and not _inside(positional[0], plans):
        raise CustodyError(f"show accepts only a saved plan from custody plans/{root}/")
    want = custody.expected_backend(home, root)
    argv = ["terraform", f"-chdir={repo / custody.ROOTS[root]}", command, *rest]
    if command == "init":
        argv += ["-input=false", "-lockfile=readonly", f"-backend-config=path={want['path']}",
                 f"-backend-config=workspace_dir={want['workspace_dir']}"]
    env = {"TF_DATA_DIR": str(want["tf_data_dir"]), "CHECKPOINT_DISABLE": "1"}
    return argv, env


def run(root: str, args: list[str], home: Path, env: dict[str, str], *,
        repo: Path = custody.REPO, require_filevault: bool = True, dry_run: bool = False,
        terraform: str = "terraform") -> dict:
    argv, overrides = build_command(root, args, home, repo=repo)
    phase = "init" if args[0] == "init" else "run"
    problems = preflight(home, root, env, phase=phase, repo=repo,
                         require_filevault=require_filevault, command=args[0])
    child_env = child_environment(env, overrides, args[0])
    report = {"root": root, "phase": phase, "argv": argv, "env": overrides,
              "credential_env": [f"{TOKEN_VAR}=<redacted>"] if TOKEN_VAR in child_env else [],
              "child_env_keys": sorted(child_env), "preflight_problems": problems}
    _assert_no_secret(report, env.get(TOKEN_VAR))
    if problems:
        raise CustodyError("preflight refused: " + "; ".join(problems))
    if dry_run:
        return report
    old = os.umask(0o077)
    try:
        result = subprocess.run([terraform, *argv[1:]], env=child_env)
    finally:
        os.umask(old)
    report["returncode"] = result.returncode
    post = custody.stray_state_paths(repo)
    if phase == "init" and result.returncode == 0:
        want = custody.expected_backend(home, root)
        post += custody.backend_cache_problems(want["tf_data_dir"], home, root)
        cache = want["tf_data_dir"] / "terraform.tfstate"
        if cache.exists() and stat.S_IMODE(cache.stat().st_mode) & 0o077:
            post.append("backend cache is group/other accessible")
    report["post_problems"] = post
    _assert_no_secret(report, env.get(TOKEN_VAR))
    return report


def _assert_no_secret(report: dict, token: str | None) -> None:
    """Defence in depth: a report or refusal must never carry the token value."""
    if token and token in json.dumps(report):
        raise CustodyError("internal error: report would expose a credential (suppressed)")


# ---------------------------------------------------------------- CLI

def _operator_only() -> None:
    if os.environ.get("CLAUDECODE"):
        raise CustodyError("running inside a Claude Code session: custody initialisation and "
                           "Terraform runs are operator-only (use Terminal.app)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-custody")
    init.add_argument("--confirm", action="store_true", help="Actually create directories")
    pre = sub.add_parser("preflight")
    pre.add_argument("--root", choices=sorted(custody.ROOTS), required=True)
    pre.add_argument("--phase", choices=("init", "run"), default="run")
    tf = sub.add_parser("run")
    tf.add_argument("--root", choices=sorted(custody.ROOTS), required=True)
    tf.add_argument("--dry-run", action="store_true")
    tf.add_argument("terraform_args", nargs=argparse.REMAINDER,
                    help="Terraform subcommand and flags, after --")
    args = parser.parse_args(argv)
    home = Path.home()
    try:
        if args.command == "preflight":
            problems = preflight(home, args.root, dict(os.environ), phase=args.phase)
            print(json.dumps({"root": args.root, "phase": args.phase,
                              "problems": problems}, indent=2))
            return 0 if not problems else 1
        if args.command == "init-custody":
            if not args.confirm:
                planned = [f"~/{custody.CUSTODY_ROOT.as_posix()}/{rel}".rstrip("/")
                           for rel in required_dirs()]
                print(json.dumps({"would_create_0700": planned,
                                  "location_problems": location_problems(home, custody.REPO)},
                                 indent=2))
                return 0
            _operator_only()
            print(json.dumps({"created": create_custody_tree(home)}, indent=2))
            return 0
        tf_args = args.terraform_args
        if tf_args[:1] == ["--"]:
            tf_args = tf_args[1:]
        if not args.dry_run:
            _operator_only()
        report = run(args.root, tf_args, home, dict(os.environ), dry_run=args.dry_run)
        print(json.dumps(report, indent=2), file=sys.stderr)
        ok = report.get("returncode", 0) == 0 and not report.get("post_problems")
        return 0 if ok else 1
    except CustodyError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
