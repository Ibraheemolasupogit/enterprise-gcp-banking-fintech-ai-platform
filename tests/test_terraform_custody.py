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
REAL_PROVIDER = {name: re.search(r'^provider "google" \{\n.*?^\}\n',
                                 (ROOT / rel / "main.tf").read_text(), re.M | re.S).group(0)
                 for name, rel in custody.ROOTS.items()}


@pytest.fixture
def world(tmp_path):
    """A fake home with an initialised custody tree and a fake repository with both roots."""
    old = os.umask(0o077)
    try:
        home, repo = tmp_path / "home", tmp_path / "repo"
        home.mkdir(mode=0o700)
        for name, rel in custody.ROOTS.items():
            (repo / rel).mkdir(parents=True, exist_ok=True)
            (repo / rel / "main.tf").write_text("terraform {\n" + tc.BACKEND_BLOCK + "}\n\n"
                                                + REAL_PROVIDER[name])
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
def test_wrapper_end_to_end_init_is_isolated_and_creates_no_state(world, monkeypatch):
    home, repo, _ = world
    # No provider blocks here, so init downloads nothing; provider checks are tested separately.
    for rel in custody.ROOTS.values():
        (repo / rel / "main.tf").write_text("terraform {\n" + tc.BACKEND_BLOCK + "}\n")
    monkeypatch.setattr(tc, "provider_problems", lambda root, repo: [])
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


# ---------------------------------------------------------------- M6P P1: credential inheritance
# Synthetic token assembled at runtime so no token-shaped literal exists in this file.
FAKE_TOKEN = "ya29" + "." + "SyntheticTestTokenNotReal_" + "x" * 24
BASE_ENV = {"HOME": "/nonexistent-home", "PATH": "/usr/bin:/bin"}


def _plan_args(home, root="bootstrap"):
    c = home / custody.CUSTODY_ROOT
    return ["plan", "-input=false", f"-var-file={c / 'inputs' / f'{root}-dev.tfvars'}",
            f"-out={c / 'plans' / root / 'p.tfplan'}"]


def _write_backend_caches(home):
    for root in custody.ROOTS:
        want = custody.expected_backend(home, root)
        cache = {"version": 3, "backend": {"type": "local", "config": {
            "path": str(want["path"]), "workspace_dir": str(want["workspace_dir"])}}}
        custody.write_private(want["tf_data_dir"] / "terraform.tfstate",
                              json.dumps(cache).encode())


def _cred(home, env, command):
    return tc.credential_problems(env, home, command)


@pytest.mark.parametrize("var", [
    "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT", "GOOGLE_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_KEYFILE_JSON", "GCLOUD_KEYFILE_JSON", "GOOGLE_BILLING_PROJECT",
    "USER_PROJECT_OVERRIDE", "GOOGLE_CLOUD_QUOTA_PROJECT", "GOOGLE_PROJECT",
    "GOOGLE_USE_DEFAULT_CREDENTIALS", "CLOUDSDK_CONFIG", "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "CLOUDSDK_CORE_PROJECT", "GCE_METADATA_HOST", "GCE_METADATA_IP", "HTTPS_PROXY",
    "https_proxy", "ALL_PROXY", "SSL_CERT_FILE"])
@pytest.mark.parametrize("command", ["plan", "apply", "init", "validate", None])
def test_credential_and_override_variables_are_refused(tmp_path, var, command):
    env = {**BASE_ENV, var: "attacker-controlled"}
    if command in tc.NETWORK_COMMANDS:
        env[tc.TOKEN_VAR] = FAKE_TOKEN
    problems = _cred(tmp_path, env, command)
    assert any(p.startswith(var + " is set") for p in problems), problems
    assert all(FAKE_TOKEN not in p and "attacker-controlled" not in p for p in problems)


@pytest.mark.parametrize("command", ["plan", "apply"])
def test_network_commands_require_a_wellformed_token(tmp_path, command):
    assert _cred(tmp_path, {**BASE_ENV, tc.TOKEN_VAR: FAKE_TOKEN}, command) == []
    assert any("is required" in p for p in _cred(tmp_path, BASE_ENV, command))
    bad = "not-a-token " + FAKE_TOKEN  # whitespace/prefix: malformed
    problems = _cred(tmp_path, {**BASE_ENV, tc.TOKEN_VAR: bad}, command)
    assert problems == [f"{tc.TOKEN_VAR} is malformed (value not shown)"]


@pytest.mark.parametrize("command", ["init", "validate", "show", "output", "state", "version"])
def test_offline_commands_refuse_a_token(tmp_path, command):
    assert _cred(tmp_path, BASE_ENV, command) == []
    assert any("offline command" in p for p in
               _cred(tmp_path, {**BASE_ENV, tc.TOKEN_VAR: FAKE_TOKEN}, command))


def test_token_runs_refuse_tf_log_adc_and_cli_config(tmp_path):
    env = {**BASE_ENV, tc.TOKEN_VAR: FAKE_TOKEN}
    assert any("TF_LOG" in p for p in _cred(tmp_path, {**env, "TF_LOG": "DEBUG"}, "plan"))
    adc = tmp_path / tc.ADC_PATH
    adc.parent.mkdir(parents=True)
    adc.write_text("{}")
    assert any("ADC file" in p for p in _cred(tmp_path, env, "plan"))
    assert not any("ADC file" in p for p in _cred(tmp_path, BASE_ENV, "validate"))
    for rel in tc.CLI_CONFIG_PATHS:
        home = tmp_path / ("h-" + rel.replace("/", "_"))
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).mkdir() if rel.endswith("plugins") else (home / rel).write_text("")
        assert any(rel in p for p in _cred(home, BASE_ENV, "validate")), rel


@pytest.mark.parametrize("change", [
    "bootstrap:impersonate_service_account", "bootstrap:credentials", "bootstrap:access_token",
    "bootstrap:billing_project", "bootstrap:user_project_override",
    "bootstrap:impersonate_service_account_delegates", "bootstrap:second_block_other_file",
    "bootstrap:override_file", "bootstrap:json_file", "pilot-dev:no_impersonation",
    "pilot-dev:other_impersonation", "pilot-dev:credentials", "pilot-dev:module_provider"])
def test_provider_identity_tampering_is_refused(world, change):
    home, repo, _ = world
    root, what = change.split(":")
    root_dir = repo / custody.ROOTS[root]
    main = root_dir / "main.tf"
    text = main.read_text()
    assert tc.provider_problems(root, repo) == []
    if what == "second_block_other_file":
        (root_dir / "providers.tf").write_text(
            'provider "google" {\n  alias   = "x"\n  project = "p"\n}\n')
    elif what == "override_file":
        (root_dir / "bootstrap_override.tf").write_text(
            'provider "google" {\n  impersonate_service_account = "sa@p.iam"\n}\n')
    elif what == "json_file":
        (root_dir / "extra.tf.json").write_text("{}")
    elif what == "no_impersonation":
        main.write_text(re.sub(r"^\s*impersonate_service_account.*\n", "", text, flags=re.M))
    elif what == "other_impersonation":
        main.write_text(text.replace("var.provisioner_service_account", "var.runner_account"))
    elif what == "module_provider":
        (root_dir / "modules" / "m").mkdir(parents=True)
        (root_dir / "modules" / "m" / "main.tf").write_text('provider "google" {\n}\n')
    else:
        attr = what
        main.write_text(text.replace('provider "google" {\n',
                                     f'provider "google" {{\n  {attr} = "x"\n', 1))
    assert tc.provider_problems(root, repo), change
    assert _pre(home, repo, root), change  # and the preflight refuses


def test_child_environment_is_an_explicit_allow_list():
    env = {**BASE_ENV, tc.TOKEN_VAR: FAKE_TOKEN, "EDITOR": "vi", "AWS_SECRET_ACCESS_KEY": "s",
           "TF_LOG": "INFO", "TF_DATA_DIR": "/elsewhere", "TF_WORKSPACE": "dev"}
    overrides = {"TF_DATA_DIR": "/custody/tfdata/bootstrap", "CHECKPOINT_DISABLE": "1"}
    plan = tc.child_environment(env, overrides, "plan")
    assert plan[tc.TOKEN_VAR] == FAKE_TOKEN and plan["TF_DATA_DIR"] == overrides["TF_DATA_DIR"]
    assert set(plan) <= {*tc.CHILD_BASE_ENV, *tc.CHILD_TF_ENV, *overrides, tc.TOKEN_VAR}
    assert "EDITOR" not in plan and "AWS_SECRET_ACCESS_KEY" not in plan
    assert "TF_WORKSPACE" not in plan
    assert tc.TOKEN_VAR not in tc.child_environment(env, overrides, "validate")


def test_dry_run_report_redacts_the_token(world):
    home, repo, _ = world
    _write_backend_caches(home)
    env = {**BASE_ENV, "HOME": str(home), tc.TOKEN_VAR: FAKE_TOKEN}
    report = tc.run("bootstrap", _plan_args(home), home, env, repo=repo,
                    require_filevault=False, dry_run=True)
    dumped = json.dumps(report)
    assert FAKE_TOKEN not in dumped and report["preflight_problems"] == []
    assert report["credential_env"] == [f"{tc.TOKEN_VAR}=<redacted>"]
    with pytest.raises(tc.CustodyError) as refused:  # refusal text never carries the value
        tc.run("bootstrap", _plan_args(home), home,
               {**env, "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT": "sa@p.iam.gserviceaccount.com"},
               repo=repo, require_filevault=False, dry_run=True)
    assert "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT" in str(refused.value)
    assert FAKE_TOKEN not in str(refused.value)


def test_cli_refusal_never_prints_token(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], tc.TOKEN_VAR: FAKE_TOKEN,
           "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT": "sa@p.iam.gserviceaccount.com"}
    result = subprocess.run([sys.executable, str(ROOT / "scripts/terraform_custody.py"), "run",
                             "--root", "bootstrap", "--dry-run", "--", *_plan_args(tmp_path)],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 2 and "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT" in result.stderr
    assert FAKE_TOKEN not in result.stderr + result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("root", sorted(custody.ROOTS))
def test_approved_token_flow_executes_with_sanitised_environment(world, tmp_path, root):
    """Approved operator flow, offline: a fake terraform records the environment it receives.
    No real Terraform, plan or Google API call is involved."""
    home, repo, _ = world
    _write_backend_caches(home)
    dump = tmp_path / f"env-{root}.txt"
    fake = tmp_path / "fake-terraform"
    fake.write_text(f'#!/bin/sh\nenv > "{dump}"\nexit 0\n')
    fake.chmod(0o700)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "TERM": "xterm",
           tc.TOKEN_VAR: FAKE_TOKEN, "EDITOR": "vi"}
    report = tc.run(root, _plan_args(home, root), home, env, repo=repo,
                    require_filevault=False, terraform=str(fake))
    assert report["returncode"] == 0 and report["post_problems"] == []
    assert FAKE_TOKEN not in json.dumps(report)
    child = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    assert child[tc.TOKEN_VAR] == FAKE_TOKEN  # the approved credential, and only it
    assert not [k for k in child if k.startswith(tc.CREDENTIAL_ENV_PREFIXES)
                and k != tc.TOKEN_VAR]
    assert child["TF_DATA_DIR"] == str(custody.expected_backend(home, root)["tf_data_dir"])
    assert child["CHECKPOINT_DISABLE"] == "1" and "EDITOR" not in child
    # Offline command in the same world: no token passed, and none required.
    dump.unlink()
    offline = tc.run(root, ["validate"], home, {k: v for k, v in env.items()
                                                 if k != tc.TOKEN_VAR},
                     repo=repo, require_filevault=False, terraform=str(fake))
    assert offline["returncode"] == 0 and tc.TOKEN_VAR not in dump.read_text()


def test_bootstrap_instance_count_is_11_with_audit_and_without_access():
    text = (ROOT / custody.ROOTS["bootstrap"] / "main.tf").read_text()
    runtime = json.loads((ROOT / "infrastructure/iam/runtime_roles.json").read_text())
    loader_roles = json.loads((ROOT / "infrastructure/iam/loader_roles.json").read_text())
    always = gated_access = gated_audit = gated_loader_identity = gated_loader_access = 0
    for kind, name in re.findall(r'^resource "(\w+)" "(\w+)"', text, re.M):
        body = text[text.index(f'resource "{kind}" "{name}"'):].split("\n}\n", 1)[0]
        # M7C1 loader resources: every one is gated off by default.
        if "for_each    = var.loader_identity_enabled ? local.loader_roles : {}" in body:
            gated_loader_identity += len(loader_roles)
        elif re.search(r"count\s+= local\.loader_identity_count", body):
            gated_loader_identity += 1
        elif "for_each   = local.loader_tables" in body:
            gated_loader_access += 3  # the three input tables, only while access is enabled
        elif re.search(r"count\s+= local\.loader_access_count", body):
            gated_loader_access += 1
        elif "for_each" in body:
            always += len(runtime)
        elif re.search(r"count\s+= local\.access_count", body):
            gated_access += 1
        elif "token_audit_logging_enabled" in body:
            gated_audit += 1
        else:
            always += 1
    assert (always, gated_access, gated_audit) == (10, 2, 1)
    assert always + gated_audit == 11  # first bootstrap: audit on, provisioner access off
    assert always + gated_audit + gated_access == 13  # later access window
    # M7C1: the disabled baseline stays 11; loader identity adds 4, a loader window 6 more.
    assert (gated_loader_identity, gated_loader_access) == (4, 6)
    assert "loader_tables         = var.loader_access_enabled ? toset([" in text


# ---------------------------------------------------------------- M6S plan-scope flags

PLAN_SCOPE_BYPASSES = [
    # -refresh=false in every Go bool spelling, dash form and case
    "-refresh=false", "-refresh=FALSE", "-refresh=False", "-refresh=f", "-refresh=F",
    "-refresh=0", "--refresh=false", "---refresh=false", "-REFRESH=false", "-Refresh=0",
    "-refresh=", "-refresh=no", "-refresh=off",
    # -target / -replace, =value, bare (separate value follows), double dash, case
    "-target=google_service_account.runner", "-target", "--target=module.storage",
    "-Target=x", "-TARGET=x", "-replace=google_service_account.runner", "-replace",
    "--replace=x", "-REPLACE=x",
    # refresh-only mode in any value, and future scoping variants
    "-refresh-only", "--refresh-only", "-refresh-only=true", "-refresh-only=false",
    "-target-file=targets.txt", "-exclude=module.bigquery", "--exclude=x",
]


@pytest.mark.parametrize("flag", PLAN_SCOPE_BYPASSES)
@pytest.mark.parametrize("command", ["plan", "apply"])
@pytest.mark.parametrize("position", ["first", "last", "after_separator"])
def test_plan_scope_and_refresh_bypasses_are_refused(world, command, flag, position):
    home, repo, _ = world
    saved = str(home / custody.CUSTODY_ROOT / "plans" / "bootstrap" / "p.tfplan")
    base = _plan_args(home)[1:] if command == "plan" else [saved]
    extra = [flag, "x"] if flag in ("-target", "-replace") else [flag]
    args = {"first": [*extra, *base], "last": [*base, *extra],
            "after_separator": [*base, "--", *extra]}[position]
    with pytest.raises(tc.CustodyError) as refused:
        tc.build_command("bootstrap", [command, *args], home, repo=repo)
    assert "not allowed" in str(refused.value)


@pytest.mark.parametrize("command", ["init", "validate", "show", "state"])
def test_scope_flags_are_refused_for_every_command(world, command):
    home, repo, _ = world
    head = [command, "list"] if command == "state" else [command]
    for flag in ("-target=x", "-replace=x", "-refresh=false", "-refresh-only"):
        with pytest.raises(tc.CustodyError):
            tc.build_command("bootstrap", [*head, flag], home, repo=repo)


@pytest.mark.parametrize("extra", [
    [], ["-refresh"], ["-refresh=true"], ["-refresh=TRUE"], ["-refresh=True"], ["-refresh=t"],
    ["-refresh=1"], ["--refresh=true"],
    ["-var=note=-target=x"],  # a variable value, not a flag: Go assigns it to -var
    ["-lock-timeout=60s"], ["-parallelism=5"], ["-lock=true"],
])
def test_normal_refresh_and_approved_plan_forms_still_work(world, extra):
    home, repo, _ = world
    c = home / custody.CUSTODY_ROOT
    args = [*_plan_args(home), *extra,
            f"-var-file={c / 'inputs' / 'bootstrap-dev-window.tfvars'}"]
    argv, _ = tc.build_command("bootstrap", args, home, repo=repo)
    # passed through unchanged (existing --flag -> -flag normalisation), nothing injected
    assert argv[2:] == [("-" + a.lstrip("-")) if a.startswith("--") else a for a in args]
    saved = str(c / "plans" / "bootstrap" / "p.tfplan")
    assert tc.build_command("bootstrap", ["apply", saved], home, repo=repo)[0][2:] == [
        "apply", saved]


@pytest.mark.parametrize("var,value", [
    ("TF_CLI_ARGS", "-refresh=false"), ("TF_CLI_ARGS_plan", "-target=x"),
    ("TF_CLI_ARGS_apply", "-replace=x"), ("TF_CLI_ARGS_plan", "-refresh-only"),
    ("TF_CLI_CONFIG_FILE", "/tmp/evil.tfrc"),
])
def test_injected_cli_environment_is_refused_and_never_reaches_terraform(
        world, tmp_path, var, value):
    home, repo, _ = world
    _write_backend_caches(home)
    fake = tmp_path / "fake-terraform"
    ran = tmp_path / "ran"
    fake.write_text(f'#!/bin/sh\ntouch "{ran}"\nexit 0\n')
    fake.chmod(0o700)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", tc.TOKEN_VAR: FAKE_TOKEN, var: value}
    assert any(p.startswith(var) for p in tc.environment_problems(env, home, "bootstrap"))
    with pytest.raises(tc.CustodyError) as refused:
        tc.run("bootstrap", _plan_args(home), home, env, repo=repo,
               require_filevault=False, terraform=str(fake))
    assert var in str(refused.value) and value not in str(refused.value)
    assert not ran.exists()
    assert var not in tc.child_environment(env, {}, "plan")  # second layer: allow-list


def test_refused_flag_never_executes_terraform(world, tmp_path):
    home, repo, _ = world
    _write_backend_caches(home)
    fake = tmp_path / "fake-terraform"
    ran = tmp_path / "ran"
    fake.write_text(f'#!/bin/sh\ntouch "{ran}"\nexit 0\n')
    fake.chmod(0o700)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", tc.TOKEN_VAR: FAKE_TOKEN}
    for flag in ("-refresh=false", "-target=x", "-replace=x", "-refresh-only"):
        with pytest.raises(tc.CustodyError):
            tc.run("bootstrap", [*_plan_args(home), flag], home, env, repo=repo,
                   require_filevault=False, terraform=str(fake))
    assert not ran.exists()
    report = tc.run("bootstrap", [*_plan_args(home), "-refresh=true"], home, env, repo=repo,
                    require_filevault=False, terraform=str(fake))
    assert report["returncode"] == 0 and ran.exists()  # control: normal refresh executes
