"""M6L offline state-custody regression checks. No real state, cloud or credential access."""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("state_custody", ROOT / "scripts/state_custody.py")
custody = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(custody)


def test_proposed_layout_is_outside_repo_and_sync_locations(tmp_path):
    home = tmp_path / "home"
    assert custody.CUSTODY_ROOT.parts == (".secure", "ebank-pilot-dev-261031-k7m4")
    assert not custody.CUSTODY_ROOT.is_absolute()  # no username committed
    assert custody.path_violations(home / custody.CUSTODY_ROOT, home, ROOT) == []
    states = [rel for rel, name in custody.LAYOUT.items() if name == "terraform.tfstate"]
    assert states == ["state/bootstrap", "state/pilot-dev"]  # isolated per root
    assert {"tfdata/bootstrap", "tfdata/pilot-dev"} <= set(custody.LAYOUT)  # TF_DATA_DIR checked


@pytest.mark.parametrize("relative", ["Desktop/state", "Documents/x", "Downloads/x",
                                      "Library/Mobile Documents/com~apple~CloudDocs/x",
                                      "Library/CloudStorage/Drive/x"])
def test_synchronised_locations_are_rejected(tmp_path, relative):
    home = tmp_path / "home"
    assert any("synchronised" in p for p in custody.path_violations(home / relative, home, ROOT))


def test_repository_and_non_home_locations_are_rejected(tmp_path):
    home = tmp_path / "home"
    inside = ROOT / "infrastructure/terraform/state/dev"
    assert "inside the Git repository" in custody.path_violations(inside, home, ROOT)
    assert "outside the operator home directory" in custody.path_violations(
        tmp_path / "elsewhere", home, ROOT)


def test_mode_policy_requires_0700_dirs_0600_files_and_no_symlinks(tmp_path):
    directory, state = tmp_path / "d", tmp_path / "s.tfstate"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    state.write_text("{}")
    os.chmod(state, 0o644)
    assert custody.mode_problems(directory, True) and custody.mode_problems(state, False)
    os.chmod(directory, 0o700)
    os.chmod(state, 0o600)
    assert custody.mode_problems(directory, True) == custody.mode_problems(state, False) == []
    link = tmp_path / "link"
    link.symlink_to(state)
    assert custody.mode_problems(link, False) == ["link is a symlink"]


def test_guard_blocks_state_plans_inputs_and_credentials_by_name():
    for name in ("infrastructure/terraform/terraform.tfstate", "a/terraform.tfstate.backup",
                 "p.tfplan", "p.tfplan.json", "bootstrap/dev.tfvars", "x.tfvars.json",
                 "crash.log", "b.sparsebundle/Info.plist", "b.dmg", "s.age",
                 "application_default_credentials.json", ".terraformrc"):
        assert "blocked file type" in custody.blob_findings(name, b""), name
    assert custody.blob_findings("environments/dev.tfvars.example", b"") == []


def test_guard_detects_secret_and_state_content_without_false_positives():
    token = ("ya29." + "A1b2" * 8).encode()
    key = b"-----BEGIN " + b"PRIVATE KEY-----"
    state = json.dumps({
        "terraform_" + "version": "1.16.5",
        "line" + "age": "abc",
    }).encode()
    assert "oauth_access_token" in custody.blob_findings("notes.md", token)
    assert "private_key_block" in custody.blob_findings("x.txt", key)
    assert "terraform_state_content" in custody.blob_findings("renamed.json", state)
    # Prose or test assertions that merely mention key words are not secrets.
    assert custody.blob_findings("doc.md", b'forbid "private_key" and lineage words') == []


def test_no_tracked_file_contains_state_plans_inputs_or_credentials():
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                           check=True).stdout.split(b"\0")
    flagged = {}
    for raw in filter(None, files):
        path = ROOT / raw.decode()
        if path.is_file() and (findings := custody.blob_findings(raw.decode(),
                                                                 path.read_bytes())):
            flagged[raw.decode()] = findings
    assert flagged == {}


@pytest.mark.parametrize("path", [
    "infrastructure/terraform/terraform.tfstate",
    "infrastructure/terraform/bootstrap/terraform.tfstate.backup",
    "infrastructure/terraform/.terraform.tfstate.lock.info",
    "infrastructure/terraform/bootstrap/dev.tfvars", "plan.tfplan", "plan.tfplan.json",
    "infrastructure/terraform/override.tf", "infrastructure/terraform/main_override.tf",
    ".terraformrc", "backup.sparsebundle/Info.plist", "backup.dmg", "state.age", "state.gpg",
    "key.p12", "infrastructure/terraform/terraform.tfstate.d/dev/terraform.tfstate",
    "application_default_credentials.json", "crash.log", "crash.20261031.log",
    "infrastructure/terraform/bootstrap/.terraform/providers/x",
])
def test_gitignore_covers_sensitive_terraform_artifacts(path):
    assert subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0


def test_examples_and_lockfiles_remain_trackable():
    for path in ("infrastructure/terraform/environments/dev.tfvars.example",
                 "infrastructure/terraform/bootstrap/environments/dev.tfvars.example",
                 "infrastructure/terraform/.terraform.lock.hcl",
                 "infrastructure/terraform/bootstrap/.terraform.lock.hcl"):
        assert subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 1


def test_snapshot_manifest_round_trip_and_tamper_detection(tmp_path):
    old = os.umask(0o077)
    try:
        live = tmp_path / "live.tfstate"
        custody.write_private(live, json.dumps(custody.fixture_state(3)).encode())
        backups = tmp_path / "backups"
        backups.mkdir(mode=0o700)
        result = custody.snapshot(live, backups)
        snap = backups / result["snapshot"]
        assert custody.verify_snapshot(snap) == {"hash_ok": True, "lineage_ok": True,
                                                 "serial_ok": True}
        for name in ("terraform.tfstate", "MANIFEST.json"):
            assert stat.S_IMODE((snap / name).stat().st_mode) == 0o600
        (snap / "terraform.tfstate").write_text(json.dumps(custody.fixture_state(4)))
        assert not all(custody.verify_snapshot(snap).values())
        with pytest.raises(FileExistsError):  # snapshots are never overwritten
            custody.snapshot(live, backups)
    finally:
        os.umask(old)


def test_fixture_is_obviously_synthetic():
    state = custody.fixture_state(1)
    assert state["resources"] == [] and state["lineage"].startswith("m6l-fixture-")
    assert "not real infrastructure" in state["outputs"]["fixture_marker"]["value"]


def test_pre_commit_hook_is_present_but_not_installed():
    hook = ROOT / "scripts/hooks/pre-commit"
    assert os.access(hook, os.X_OK) and "state_custody.py\" staged" in hook.read_text()
    configured = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=ROOT,
                                capture_output=True, text=True).stdout.strip()
    assert configured in ("", "scripts/hooks")  # installation is an operator decision


@pytest.mark.skipif(os.environ.get("M6L_RUN_DRILL") != "1" or not shutil.which("hdiutil"),
                    reason="Encrypted-image drill mounts a disposable volume; opt in explicitly")
def test_encrypted_restore_drill(tmp_path):
    assert custody.main(["drill", "--workdir", str(tmp_path)]) == 0


# ---------------------------------------------------------------- M6L closeout: backend fail-safe
# Disposable configurations only (no providers, no resources) in tmp_path; only `init`,
# `state list` and `workspace` are run. Never plan/apply, never the real roots.

TERRAFORM = shutil.which("terraform")
needs_terraform = pytest.mark.skipif(TERRAFORM is None, reason="terraform binary unavailable")


@pytest.fixture
def sandbox(tmp_path):
    """A fake repository holding one root with the proposed partial `backend "local" {}`."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    root = repo / "root"
    root.mkdir(parents=True)
    (root / "main.tf").write_text('terraform {\n  backend "local" {}\n}\n')
    want = custody.expected_backend(home, "bootstrap")
    old = os.umask(0o077)
    try:
        for key in ("tf_data_dir", "workspace_dir"):
            want[key].mkdir(parents=True)
        yield repo, root, home, want
    finally:
        os.umask(old)


def _tf(root, *args, data_dir=None, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TF_")}
    env.update(CHECKPOINT_DISABLE="1", TF_IN_AUTOMATION="1", TF_INPUT="0", **extra)
    if data_dir is not None:
        env["TF_DATA_DIR"] = str(data_dir)
    return subprocess.run([TERRAFORM, f"-chdir={root}", *args, "-no-color"],
                          capture_output=True, text=True, env=env, timeout=120)


def _custody_init(root, want, *, workspace_dir=True):
    args = ["init", f"-backend-config=path={want['path']}"]
    if workspace_dir:
        args.append(f"-backend-config=workspace_dir={want['workspace_dir']}")
    result = _tf(root, *args, data_dir=want["tf_data_dir"])
    assert result.returncode == 0, result.stderr
    return result


@needs_terraform
def test_explicit_backend_fails_closed_when_init_or_data_dir_is_omitted(sandbox, tmp_path):
    repo, root, home, want = sandbox
    for data_dir in (None, tmp_path / "fresh-data"):  # repo .terraform, or a new TF_DATA_DIR
        result = _tf(root, "state", "list", data_dir=data_dir)
        assert result.returncode != 0 and "Backend initialization required" in result.stderr
    _custody_init(root, want)
    forgotten = _tf(root, "state", "list")  # TF_DATA_DIR forgotten after a correct init
    assert forgotten.returncode != 0 and "Backend initialization required" in forgotten.stderr
    assert custody.stray_state_paths(repo) == []


@needs_terraform
def test_correct_custody_init_passes_preflight_and_contains_workspaces(sandbox):
    repo, root, home, want = sandbox
    _custody_init(root, want)
    assert custody.backend_cache_problems(want["tf_data_dir"], home, "bootstrap") == []
    assert not want["path"].exists()  # init alone never creates state
    _tf(root, "state", "list", data_dir=want["tf_data_dir"], TF_WORKSPACE="dev")
    assert custody.stray_state_paths(repo) == []  # accidental workspace lands in custody
    assert (want["workspace_dir"] / "dev").is_dir()
    assert custody.environment_problems({"TF_WORKSPACE": "dev"})  # and preflight refuses it


@needs_terraform
def test_init_without_backend_config_fails_open_and_preflight_catches_it(sandbox):
    repo, root, home, want = sandbox
    result = _tf(root, "init", data_dir=want["tf_data_dir"])
    assert result.returncode == 0  # Terraform accepts it: state would default to the repository
    problems = custody.backend_cache_problems(want["tf_data_dir"], home, "bootstrap")
    assert any("backend path" in p for p in problems)
    assert any("workspace_dir" in p for p in problems)


@needs_terraform
def test_workspace_without_workspace_dir_escapes_into_working_directory(sandbox):
    repo, root, home, want = sandbox
    _custody_init(root, want, workspace_dir=False)
    _tf(root, "state", "list", data_dir=want["tf_data_dir"], TF_WORKSPACE="dev")
    assert custody.stray_state_paths(repo) == ["root/terraform.tfstate.d"]  # fail-open proven
    assert any("workspace_dir" in p for p in
               custody.backend_cache_problems(want["tf_data_dir"], home, "bootstrap"))


def test_preflight_rejects_repository_data_dir_and_checkpoint(tmp_path):
    home = tmp_path / "home"
    problems = custody.backend_cache_problems(ROOT / "infrastructure/terraform/.terraform",
                                              home, "pilot-dev")
    assert "TF_DATA_DIR is not the custody tfdata directory for this root" in problems
    assert custody.environment_problems({}) == [
        "CHECKPOINT_DISABLE is not 1 (Terraform would contact HashiCorp)"]
    assert custody.environment_problems({"CHECKPOINT_DISABLE": "1"}) == []


def test_repository_holds_no_state_plan_lock_or_workspace_artifacts_on_disk():
    assert custody.stray_state_paths(ROOT) == []  # includes git-ignored files


def test_backend_blocks_use_fail_closed_sentinel():
    # M6O (gate 8): explicit local backend with a /dev/null sentinel; custody paths are supplied
    # only via -backend-config by scripts/terraform_custody.py (tests/test_terraform_custody.py).
    for root in custody.ROOTS.values():
        text = (ROOT / root / "main.tf").read_text()
        assert text.count('backend "') == 1 and 'backend "local"' in text
        assert 'path          = "/dev/null/' in text and 'workspace_dir = "/dev/null/' in text
