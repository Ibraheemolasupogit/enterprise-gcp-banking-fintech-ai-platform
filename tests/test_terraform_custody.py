"""M6O secure local backend regression checks.

Offline only: fake home directories and fake repositories under tmp_path. Sandbox Terraform
runs use configurations with no providers and no resources and only `init` and `state list`;
never plan/apply/destroy, never the real roots, never real custody.
"""

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("terraform_custody",
                                              ROOT / "scripts/terraform_custody.py")
tc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tc)
custody = tc.custody

TERRAFORM = shutil.which("terraform")
needs_terraform = pytest.mark.skipif(TERRAFORM is None, reason="terraform binary unavailable")
BACKEND_RE = re.compile(r'backend\s+"(\w+)"\s*\{(.*?)\}', re.S)


@pytest.fixture
def world(tmp_path):
    """A fake home with an initialised custody tree and a fake repository with both roots."""
    old = os.umask(0o077)
    try:
        home, repo = tmp_path / "home", tmp_path / "repo"
        home.mkdir(mode=0o700)
        for rel in custody.ROOTS.values():
            (repo / rel).mkdir(parents=True, exist_ok=True)
            (repo / rel / "main.tf").write_text("terraform {\n" + tc.BACKEND_BLOCK + "}\n")
        created = tc.create_custody_tree(home, repo)
        yield home, repo, created
    finally:
        os.umask(old)


def _pre(home, repo, root="bootstrap", env=None, phase="init"):
    return tc.preflight(home, root, env or {}, phase=phase, repo=repo, require_filevault=False)


# ---------------------------------------------------------------- real roots

@pytest.mark.parametrize("root", sorted(custody.ROOTS))
def test_real_roots_declare_only_the_sentinel_local_backend(root):
    text = (ROOT / custody.ROOTS[root] / "main.tf").read_text()
    blocks = BACKEND_RE.findall(text)
    assert len(blocks) == 1 and blocks[0][0] == "local"
    assert tc.BACKEND_BLOCK in text
    assert tc.SENTINEL["path"].startswith("/dev/null/") and \
        tc.SENTINEL["workspace_dir"].startswith("/dev/null/")
    assert "/Users/" not in text and "/home/" not in text  # no username committed
    assert not Path("/dev/null").is_dir()  # sentinel parent can never be created


def test_roots_have_isolated_custody_paths(tmp_path):
    a, b = (custody.expected_backend(tmp_path, r) for r in ("bootstrap", "pilot-dev"))
    for key in a:
        assert a[key] != b[key]
        assert tmp_path / custody.CUSTODY_ROOT in a[key].parents
    assert {"state/bootstrap/workspaces", "state/pilot-dev/workspaces"} <= set(
        tc.required_dirs())


# ---------------------------------------------------------------- custody initialisation

def test_init_creates_private_tree_without_state_and_is_idempotent(world):
    home, repo, created = world
    root = home / custody.CUSTODY_ROOT
    assert ".secure" in created and len(created) == len(tc.required_dirs()) + 2
    for rel in tc.required_dirs():
        assert stat.S_IMODE((root / rel).stat().st_mode) == 0o700, rel
    assert stat.S_IMODE((root / tc.SPOTLIGHT_MARKER).stat().st_mode) == 0o600
    assert not [p for p in root.rglob("*") if p.is_file() and p.name != tc.SPOTLIGHT_MARKER]
    assert tc.create_custody_tree(home, repo) == []  # idempotent
    assert _pre(home, repo) == []


def test_init_never_loosens_or_repairs_existing_entries(world):
    home, repo, _ = world
    os.chmod(home / custody.CUSTODY_ROOT / "state", 0o755)
    with pytest.raises(tc.CustodyError, match="exceeds"):
        tc.create_custody_tree(home, repo)
    assert stat.S_IMODE((home / custody.CUSTODY_ROOT / "state").stat().st_mode) == 0o755


@pytest.mark.parametrize("target", ["Library/Mobile Documents/com~apple~CloudDocs/s",
                                    "Desktop/s", "outside"])
def test_init_refuses_symlinked_secure_dir(tmp_path, target):
    home = tmp_path / "home"
    home.mkdir()
    real = tmp_path / "elsewhere" if target == "outside" else home / target
    real.mkdir(parents=True)
    (home / ".secure").symlink_to(real)
    with pytest.raises(tc.CustodyError):
        tc.create_custody_tree(home, tmp_path / "repo")
    assert not any(real.iterdir())


def test_init_refuses_custody_inside_git_worktree_or_repository(tmp_path):
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)  # e.g. a dotfiles repository in $HOME
    with pytest.raises(tc.CustodyError, match="Git working tree"):
        tc.create_custody_tree(home, tmp_path / "repo")
    assert any("inside the Git repository" in p
               for p in tc.location_problems(ROOT, ROOT))  # home == repository


# ---------------------------------------------------------------- adversarial preflight

def _mutate(world, case):
    home, repo, _ = world
    root = home / custody.CUSTODY_ROOT
    env = {}
    if case == "loose_dir":
        os.chmod(root / "state/bootstrap", 0o750)
    elif case == "loose_file":
        (root / "state/bootstrap/terraform.tfstate").write_text("{}")
        os.chmod(root / "state/bootstrap/terraform.tfstate", 0o644)
    elif case == "symlinked_state_dir":
        shutil.rmtree(root / "state/pilot-dev")
        (root / "state/pilot-dev").symlink_to(root / "state/bootstrap")
    elif case == "unexpected_state_entry":
        custody.write_private(root / "state/bootstrap/copy-of-state.json", b"{}")
    elif case == "missing_dir":
        shutil.rmtree(root / "tfdata/pilot-dev")
    elif case == "missing_marker":
        (root / tc.SPOTLIGHT_MARKER).unlink()
    elif case == "tf_workspace":
        env = {"TF_WORKSPACE": "dev"}
    elif case == "tf_cli_args":
        env = {"TF_CLI_ARGS_apply": "-lock=false"}
    elif case == "tf_cli_config":
        env = {"TF_CLI_CONFIG_FILE": str(repo / "x.tfrc")}
    elif case == "tf_data_dir_other_root":
        env = {"TF_DATA_DIR": str(root / "tfdata/pilot-dev")}
    elif case == "tf_data_dir_repository":
        env = {"TF_DATA_DIR": str(repo / custody.ROOTS["bootstrap"] / ".terraform")}
    elif case == "tf_log_unrouted":
        env = {"TF_LOG": "DEBUG"}
    elif case == "stray_state_in_repo":
        (repo / custody.ROOTS["bootstrap"] / "terraform.tfstate").write_text("{}")
    elif case == "stray_workspace_in_repo":
        (repo / custody.ROOTS["pilot-dev"] / "terraform.tfstate.d").mkdir()
    elif case == "backend_block_removed":
        (repo / custody.ROOTS["bootstrap"] / "main.tf").write_text("terraform {}\n")
    elif case == "repo_in_icloud_desktop":
        moved = home / "Desktop" / "repo"
        moved.parent.mkdir()
        shutil.copytree(repo, moved)
        repo = moved
    elif case == "tree_missing":
        shutil.rmtree(root)
    return home, repo, env


@pytest.mark.parametrize("case", [
    "loose_dir", "loose_file", "symlinked_state_dir", "unexpected_state_entry", "missing_dir",
    "missing_marker", "tf_workspace", "tf_cli_args", "tf_cli_config", "tf_data_dir_other_root",
    "tf_data_dir_repository", "tf_log_unrouted", "stray_state_in_repo",
    "stray_workspace_in_repo", "backend_block_removed", "repo_in_icloud_desktop",
    "tree_missing"])
def test_preflight_fails_closed(world, case):
    home, repo, env = _mutate(world, case)
    assert _pre(home, repo, env=env), case


def test_tf_log_allowed_only_into_custody_logs(world):
    home, repo, _ = world
    logs = home / custody.CUSTODY_ROOT / "logs"
    assert _pre(home, repo, env={"TF_LOG": "DEBUG", "TF_LOG_PATH": str(logs / "tf.log")}) == []
    assert _pre(home, repo, env={"TF_LOG": "DEBUG", "TF_LOG_PATH": str(repo / "tf.log")})


def test_run_phase_requires_custody_backend_cache(world):
    home, repo, _ = world
    problems = _pre(home, repo, phase="run")
    assert "backend not initialised in TF_DATA_DIR" in problems


# ---------------------------------------------------------------- command policy

def test_init_command_injects_per_root_custody_backend(world):
    home, repo, _ = world
    for root in custody.ROOTS:
        argv, env = tc.build_command(root, ["init"], home, repo=repo)
        want = custody.expected_backend(home, root)
        assert f"-backend-config=path={want['path']}" in argv
        assert f"-backend-config=workspace_dir={want['workspace_dir']}" in argv
        assert "-lockfile=readonly" in argv and "-input=false" in argv
        assert argv[1] == f"-chdir={repo / custody.ROOTS[root]}"
        assert env == {"TF_DATA_DIR": str(want["tf_data_dir"]), "CHECKPOINT_DISABLE": "1"}


def test_plan_and_apply_must_use_saved_plans_in_own_root(world):
    home, repo, _ = world
    plans = home / custody.CUSTODY_ROOT / "plans"
    good = f"-out={plans / 'bootstrap' / 'p.tfplan'}"
    assert tc.build_command("bootstrap", ["plan", good], home, repo=repo)
    tc.build_command("bootstrap", ["apply", str(plans / "bootstrap" / "p.tfplan")], home,
                     repo=repo)
    inputs = home / custody.CUSTODY_ROOT / "inputs" / "bootstrap-dev.tfvars"
    tc.build_command("bootstrap", ["plan", good, f"-var-file={inputs}"], home, repo=repo)


@pytest.mark.parametrize("args", [
    ["destroy"], ["import", "a", "b"], ["state", "rm", "x"], ["state", "push", "f"],
    ["state", "mv", "a", "b"], ["workspace", "new", "dev"], ["force-unlock", "id"],
    ["console"], ["refresh"], ["taint", "x"], ["-chdir=/tmp", "plan"], [],
    ["plan", "-lock=false"], ["plan", "--lock=FALSE"], ["plan", "-lock=0"],
    ["plan", "-state=/tmp/s"], ["init", "-backend-config=path=./terraform.tfstate"],
    ["init", "-backend=false"], ["init", "--backend=0"], ["init", "-backend"],
    ["init", "-migrate-state"], ["init", "-upgrade"],
    ["init", "-lockfile=readonly"], ["plan", "-destroy", "-out=x"], ["apply", "-auto-approve"],
    ["plan"], ["plan", "-out=plan.tfplan"], ["plan", "-out", "x"],
    ["plan", "-out=PLANS_OTHER"], ["apply", "PLANS_OTHER"], ["apply", "REPO_PLAN"],
    ["plan", "-out=PLANS_OWN", "-var-file=REPO_VARS"], ["plan", "-out=PLANS_OWN", "-var-file",
                                                        "x"],
    ["show", "REPO_PLAN"], ["state", "list", "-state=x"],
])
def test_command_policy_refuses_unsafe_invocations(world, args):
    home, repo, _ = world
    plans = home / custody.CUSTODY_ROOT / "plans"
    subst = {"PLANS_OTHER": str(plans / "pilot-dev" / "p.tfplan"),
             "PLANS_OWN": str(plans / "bootstrap" / "p.tfplan"),
             "REPO_PLAN": str(repo / "p.tfplan"), "REPO_VARS": str(repo / "dev.tfvars")}
    args = [a.replace(k, v) for a in args for k, v in [next(
        ((k, v) for k, v in subst.items() if k in a), ("", ""))]]
    with pytest.raises(tc.CustodyError):
        tc.build_command("bootstrap", args, home, repo=repo)


def test_cli_refuses_inside_claude_and_creates_nothing(tmp_path):
    script = str(ROOT / "scripts/terraform_custody.py")
    env = {**os.environ, "HOME": str(tmp_path), "CLAUDECODE": "1"}
    for args in (["init-custody", "--confirm"], ["run", "--root", "bootstrap", "--", "init"]):
        result = subprocess.run([sys.executable, script, *args], capture_output=True, text=True,
                                env=env, stdin=subprocess.DEVNULL)
        assert result.returncode == 2 and "operator-only" in result.stderr, result.stderr
    dry = subprocess.run([sys.executable, script, "init-custody"], capture_output=True,
                         text=True, env=env)
    assert dry.returncode == 0 and "would_create_0700" in dry.stdout
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- sandbox Terraform probes

def _tf(root, *args, data_dir=None, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TF_")}
    env.update(CHECKPOINT_DISABLE="1", TF_IN_AUTOMATION="1", TF_INPUT="0", **extra)
    if data_dir is not None:
        env["TF_DATA_DIR"] = str(data_dir)
    return subprocess.run([TERRAFORM, f"-chdir={root}", *args, "-no-color"],
                          capture_output=True, text=True, env=env, timeout=120)


@needs_terraform
@pytest.mark.parametrize("variant", ["plain", "path_only", "workspace_dir_only",
                                     "backend_false", "tf_workspace"])
def test_real_backend_block_fails_closed_without_custody_config(tmp_path, variant):
    # The exact backend block from the real bootstrap root, without its providers/resources.
    block = BACKEND_RE.search((ROOT / custody.ROOTS["bootstrap"] / "main.tf").read_text())
    repo = tmp_path / "repo"
    (repo / "root").mkdir(parents=True)
    (repo / "root" / "main.tf").write_text("terraform {\n  " + block.group(0) + "\n}\n")
    data = tmp_path / "data"
    args = {"plain": [], "path_only": [f"-backend-config=path={tmp_path / 's.tfstate'}"],
            "workspace_dir_only": [f"-backend-config=workspace_dir={tmp_path / 'ws'}"],
            "backend_false": ["-backend=false"], "tf_workspace": []}[variant]
    (tmp_path / "ws").mkdir()
    extra = {"TF_WORKSPACE": "dev"} if variant == "tf_workspace" else {}
    init = _tf(repo / "root", "init", *args, data_dir=data, **extra)
    listed = _tf(repo / "root", "state", "list", data_dir=data, **extra)
    if variant == "backend_false":
        assert init.returncode == 0  # init skips the backend, but every state command stops
    else:
        assert init.returncode != 0 and "not a directory" in init.stderr
    # Fails closed either before the backend is usable or at the unreachable sentinel state.
    assert listed.returncode != 0 and ("Backend initialization required" in listed.stderr
                                       or "/dev/null/" in listed.stderr), listed.stderr
    assert custody.stray_state_paths(repo) == []
    assert not (tmp_path / "s.tfstate").exists()


@needs_terraform
def test_wrapper_end_to_end_init_is_isolated_and_creates_no_state(world):
    home, repo, _ = world
    for root in custody.ROOTS:
        report = tc.run(root, ["init"], home, {}, repo=repo, require_filevault=False,
                        terraform=TERRAFORM)
        assert report["returncode"] == 0 and report["post_problems"] == [], report
        want = custody.expected_backend(home, root)
        cache = want["tf_data_dir"] / "terraform.tfstate"
        assert stat.S_IMODE(cache.stat().st_mode) == 0o600
        assert json.loads(cache.read_text())["backend"]["config"] == {
            "path": str(want["path"]), "workspace_dir": str(want["workspace_dir"])}
        assert not want["path"].exists()  # init never creates state
        assert _pre(home, repo, root, phase="run") == []
        listed = tc.run(root, ["state", "list"], home, {}, repo=repo, require_filevault=False,
                        terraform=TERRAFORM)
        assert listed["post_problems"] == [] and not want["path"].exists()
    assert custody.stray_state_paths(repo) == []
    # Cross-root contamination: point the bootstrap cache at the pilot state file.
    cache = custody.expected_backend(home, "bootstrap")["tf_data_dir"] / "terraform.tfstate"
    doc = json.loads(cache.read_text())
    doc["backend"]["config"]["path"] = str(custody.expected_backend(home, "pilot-dev")["path"])
    cache.write_text(json.dumps(doc))
    for root in custody.ROOTS:  # both roots refuse until the contamination is resolved
        assert any("bootstrap" in p for p in _pre(home, repo, root, phase="run"))
