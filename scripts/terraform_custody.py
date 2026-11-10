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
FORBIDDEN_ENV_PREFIXES = ("TF_CLI_ARGS", "TF_CLI_CONFIG_FILE", "TF_STATE", "TF_PLUGIN_CACHE_DIR")


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
              repo: Path = custody.REPO, require_filevault: bool = True) -> list[str]:
    """Every problem that must stop a Terraform command for ``root``. Empty means proceed."""
    if root not in custody.ROOTS:
        return [f"unknown root {root}"]
    problems = location_problems(home, repo) + tree_problems(home)
    problems += environment_problems(env, home, root) + isolation_problems(home, root)
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
        flag, _, value = arg.partition("=")
        if flag == "-lock" and value.lower() not in ("", "true", "t", "1"):
            raise CustodyError("disabling state locking is not allowed")
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
                         require_filevault=require_filevault)
    report = {"root": root, "phase": phase, "argv": argv, "env": overrides,
              "preflight_problems": problems}
    if problems:
        raise CustodyError("preflight refused: " + "; ".join(problems))
    if dry_run:
        return report
    child_env = {k: v for k, v in env.items() if not k.startswith("TF_") or k in
                 ("TF_LOG", "TF_LOG_PATH", "TF_IN_AUTOMATION", "TF_INPUT")}
    child_env.update(overrides)
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
    return report


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
