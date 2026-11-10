"""M6Q: offline saved-plan checker for the bootstrap root (initial and recovery modes).

Synthetic ``terraform show -json`` documents only; no Terraform, gcloud or network access.
"""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap_plan_check.py"
BOOTSTRAP_MAIN = ROOT / "infrastructure" / "terraform" / "bootstrap" / "main.tf"
_spec = importlib.util.spec_from_file_location("bootstrap_plan_check", SCRIPT)
bpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bpc)

PROJECT = bpc.PROJECT
RUNNER = f"bankpilot-dev-runner@{PROJECT}.iam.gserviceaccount.com"


def _rc(address: str, kind: str, name: str, after: dict, actions: list[str]) -> dict:
    return {"address": address, "mode": "managed", "type": kind, "name": name,
            "change": {"actions": actions, "before": None if actions == ["create"] else after,
                       "after": after, "after_unknown": {}}}


def _durable_changes(actions: list[str]) -> list[dict]:
    roles = bpc.expected_roles()
    out = [_rc(f"google_service_account.{n}", "google_service_account", n,
               {"project": PROJECT, "account_id": f"bankpilot-dev-{n}"}, actions)
           for n in ("runner", "provisioner")]
    runtime = json.loads((bpc.IAM / "runtime_roles.json").read_text())
    for key in runtime:
        rid = f"bankpilot_dev_{key}"
        out.append(_rc(f'google_project_iam_custom_role.runtime["{key}"]',
                       "google_project_iam_custom_role", "runtime",
                       {"project": PROJECT, "role_id": rid, "permissions": sorted(roles[rid])},
                       actions))
    for name, rid in (("provision", "bankpilot_dev_provision"),
                      ("impersonate", "bankpilot_dev_impersonate")):
        out.append(_rc(f"google_project_iam_custom_role.{name}",
                       "google_project_iam_custom_role", name,
                       {"project": PROJECT, "role_id": rid, "permissions": sorted(roles[rid])},
                       actions))
    member = {"project": PROJECT, "condition": [],
              "role": f"projects/{PROJECT}/roles/bankpilot_dev_jobs",
              "member": f"serviceAccount:{RUNNER}"}
    if actions == ["create"]:  # role and member are unknown until the role and SA exist
        member = {"project": PROJECT, "condition": []}
    out.append(_rc("google_project_iam_member.runner_jobs", "google_project_iam_member",
                   "runner_jobs", member, actions))
    return out


def _audit(service: str = "iam.googleapis.com", types=("ADMIN_READ", "DATA_READ"),
           exempt=()) -> dict:
    after = {"project": PROJECT, "service": service,
             "audit_log_config": [{"log_type": t, "exempted_members": list(exempt)}
                                  for t in types]}
    return _rc(bpc.AUDIT_ADDRESS, "google_project_iam_audit_config", "iamcredentials",
               after, ["create"])


def plan(mode: str = "recovery") -> dict:
    durable = _durable_changes(["no-op"] if mode == "recovery" else ["create"])
    variables = {k: {"value": v} for k, v in bpc.EXPECTED_VARIABLES.items()}
    variables["operator_member"] = {"value": "user:operator@example.invalid"}
    variables["access_expiry"] = {"value": None}
    return {"format_version": "1.2", "errored": False, "variables": variables,
            "resource_changes": [*durable, _audit()]}


def by_addr(p: dict, address: str) -> dict:
    return next(rc for rc in p["resource_changes"] if rc["address"] == address)


# ---------------------------------------------------------------- good plans

def test_recovery_plan_adds_only_the_iam_audit_config():
    report = bpc.check_plan(plan("recovery"), "recovery")
    assert report["ok"], report["problems"]
    assert report["counts"] == {"google_project_iam_audit_config": 1}
    assert report["creates"] == 1 and report["unchanged"] == 10
    assert report["create_addresses"] == [bpc.AUDIT_ADDRESS]


def test_initial_plan_still_creates_eleven():
    report = bpc.check_plan(plan("initial"), "initial")
    assert report["ok"], report["problems"]
    assert report["creates"] == 11 and report["unchanged"] == 0
    assert report["counts"] == {"google_service_account": 2,
                                "google_project_iam_custom_role": 7,
                                "google_project_iam_member": 1,
                                "google_project_iam_audit_config": 1}


def test_modes_do_not_accept_each_other():
    assert not bpc.check_plan(plan("initial"), "recovery")["ok"]
    assert not bpc.check_plan(plan("recovery"), "initial")["ok"]
    with pytest.raises(ValueError):
        bpc.check_plan(plan(), "apply")


# ---------------------------------------------------------------- adversarial recovery plans

def _mutate(fn):
    p = plan("recovery")
    fn(p)
    return p


def _set_audit(p, rc):
    p["resource_changes"][-1] = rc


def _widen(p):
    rc = by_addr(p, "google_project_iam_custom_role.provision")
    rc["change"]["actions"] = ["update"]
    rc["change"]["after"]["permissions"] = [*rc["change"]["after"]["permissions"],
                                            "resourcemanager.projects.setIamPolicy"]


def _replace_sa(p):
    by_addr(p, "google_service_account.runner")["change"]["actions"] = ["delete", "create"]


def _delete_role(p):
    by_addr(p, "google_project_iam_custom_role.impersonate")["change"]["actions"] = ["delete"]


def _access_on(p):
    p["variables"]["provisioner_access_enabled"]["value"] = True
    p["resource_changes"].append(_rc(
        "google_project_iam_member.provisioner[0]", "google_project_iam_member", "provisioner",
        {"project": PROJECT, "role": f"projects/{PROJECT}/roles/bankpilot_dev_provision",
         "condition": [{"expression": "request.time < timestamp(\"x\")"}]}, ["create"]))


def _audit_off(p):
    p["variables"]["token_audit_logging_enabled"]["value"] = False
    p["resource_changes"].pop()


def _drift(p):
    p["resource_drift"] = [{"address": "google_project_iam_member.runner_jobs"}]


def _missing_existing(p):
    p["resource_changes"] = [rc for rc in p["resource_changes"]
                             if rc["address"] != "google_service_account.provisioner"]


def _other_project(p):
    p["resource_changes"][-1]["change"]["after"]["project"] = "some-other-project"


def _owner_member(p):
    p["resource_changes"].append(_rc(
        "google_project_iam_member.extra", "google_project_iam_member", "extra",
        {"project": PROJECT, "role": "roles/owner", "member": "user:x@example.invalid"},
        ["create"]))


def _data_source(p):
    rc = _rc("data.google_project.this", "google_project", "this", {}, ["read"])
    rc["mode"] = "data"
    p["resource_changes"].append(rc)


def _errored(p):
    p["errored"] = True


def _member_rebound(p):
    rc = by_addr(p, "google_project_iam_member.runner_jobs")
    rc["change"]["after"]["member"] = "user:someone@example.invalid"


ADVERSARIAL = {
    "old iamcredentials service": lambda p: _set_audit(p, _audit("iamcredentials.googleapis.com")),
    "allServices audit config": lambda p: _set_audit(p, _audit("allServices")),
    "missing DATA_READ": lambda p: _set_audit(p, _audit(types=("ADMIN_READ",))),
    "extra DATA_WRITE": lambda p: _set_audit(
        p, _audit(types=("ADMIN_READ", "DATA_READ", "DATA_WRITE"))),
    "exempted member": lambda p: _set_audit(p, _audit(exempt=("user:x@example.invalid",))),
    "widened role update": _widen,
    "service account replace": _replace_sa,
    "role delete": _delete_role,
    "provisioner access enabled": _access_on,
    "audit flag off": _audit_off,
    "drift": _drift,
    "existing resource missing": _missing_existing,
    "other project": _other_project,
    "owner member": _owner_member,
    "data source": _data_source,
    "errored plan": _errored,
    "runner member rebound": _member_rebound,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_adversarial_recovery_plans_are_rejected(name):
    report = bpc.check_plan(_mutate(ADVERSARIAL[name]), "recovery")
    assert not report["ok"] and report["problems"], name


def test_audit_replace_is_rejected_even_with_correct_service():
    p = plan()
    p["resource_changes"][-1]["change"]["actions"] = ["delete", "create"]
    assert not bpc.check_plan(p, "recovery")["ok"]


# ---------------------------------------------------------------- consistency with Terraform

def test_checker_matches_bootstrap_terraform():
    text = BOOTSTRAP_MAIN.read_text()
    block = text[text.index('resource "google_project_iam_audit_config" "iamcredentials"'):]
    block = block.split("\n}\n", 1)[0]
    assert re.search(r'^\s*service\s*=\s*"iam\.googleapis\.com"\s*$', block, re.M)
    assert "count   = var.token_audit_logging_enabled ? 1 : 0" in block
    assert sorted(re.findall(r'log_type = "(\w+)"', block)) == ["ADMIN_READ", "DATA_READ"]
    assert "exempted_members" not in block
    assert "iamcredentials.googleapis.com" not in text and "allServices" not in text
    assert len(bpc.durable_addresses()) == 10


def test_report_contains_no_plan_values_beyond_addresses():
    p = plan()
    p["variables"]["operator_member"]["value"] = "user:private-operator@example.invalid"
    out = json.dumps(bpc.check_plan(p, "recovery"))
    assert "private-operator" not in out


# ---------------------------------------------------------------- CLI

def _cli(stdin: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], input=stdin,
                          capture_output=True, text=True, check=False)


def test_cli_exit_codes():
    good = _cli(json.dumps(plan()), "--mode", "recovery")
    assert good.returncode == 0 and json.loads(good.stdout)["ok"] is True
    bad = _cli(json.dumps(_mutate(ADVERSARIAL["old iamcredentials service"])),
               "--mode", "recovery")
    assert bad.returncode == 1 and json.loads(bad.stdout)["ok"] is False
    assert _cli("not json", "--mode", "recovery").returncode == 2
    assert _cli("[]", "--mode", "recovery").returncode == 2
    assert _cli("{}").returncode == 2  # --mode is required


def test_main_reads_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(copy.deepcopy(plan()))))
    assert bpc.main(["--mode", "recovery"]) == 0
    assert json.loads(capsys.readouterr().out)["unchanged"] == 10


# ---------------------------------------------------------------- each layer on its own

def test_non_create_actions_are_named():
    p = plan()
    by_addr(p, "google_service_account.runner")["change"]["actions"] = ["delete", "create"]
    problems = bpc.check_plan(p, "recovery")["problems"]
    assert "google_service_account.runner: ['delete', 'create']" in problems


@pytest.mark.parametrize("name,value", [("provisioner_access_enabled", True),
                                        ("token_audit_logging_enabled", False),
                                        ("project_id", "some-other-project"),
                                        ("prefix", "other")])
def test_variable_values_are_checked_alone(name, value):
    p = plan()
    p["variables"][name]["value"] = value
    assert bpc.check_plan(p, "recovery")["problems"] == [
        f"variable {name} is not the approved value"]


def test_missing_variable_is_rejected():
    p = plan()
    del p["variables"]["provisioner_access_enabled"]
    assert not bpc.check_plan(p, "recovery")["ok"]


@pytest.mark.parametrize("bad", ["allServices", "iamcredentials.googleapis.com"])
def test_forbidden_audit_services_anywhere_in_plan(bad):
    p = plan()
    p["output_changes"] = {"bootstrap_inventory": {"after": {"note": bad}}}
    assert bpc.check_plan(p, "recovery")["problems"] == [f"forbidden token present: {bad}"]
