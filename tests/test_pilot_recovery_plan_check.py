"""M6T: BigQuery dataset IAM partial-apply recovery (offline).

Reproduces the design defect behind HTTP 400 "No owners specified" and checks the bounded
``pilot-recovery`` saved-plan mode. Synthetic ``terraform show -json`` documents only.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap_plan_check.py"
BIGQUERY = ROOT / "infrastructure" / "terraform" / "modules" / "bigquery" / "main.tf.json"
_spec = importlib.util.spec_from_file_location("bootstrap_plan_check_m6t", SCRIPT)
bpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bpc)

P = bpc.PROJECT
RUNNER = f"serviceAccount:{bpc.RUNNER_EMAIL}"
PROVISIONER = f"serviceAccount:{bpc.PROVISIONER_EMAIL}"
OWNER = "user:approved-dataset-owner@example.invalid"
METADATA = f"{bpc.ROLE_PREFIX}metadata"
DS = bpc.DATASET_POLICY


def _policy(*bindings) -> str:
    return json.dumps({"bindings": [{"role": r, "members": list(m)} for r, m in bindings]})


GOOD_POLICY = _policy(("roles/bigquery.dataOwner", [OWNER]), (METADATA, [RUNNER]))


def _rc(address, kind, name, values, actions, mode="managed"):
    return {"address": address, "mode": mode, "type": kind, "name": name,
            "change": {"actions": actions,
                       "before": None if actions == ["create"] else values,
                       "after": None if actions == ["delete"] else values}}


def recovery_plan() -> dict:
    """17 created resources unchanged (as refreshed after the partial apply) + 1 create."""
    rcs = [
        _rc("module.storage.google_storage_bucket.artifacts", "google_storage_bucket",
            "artifacts", {"project": P, "location": "EUROPE-WEST2", "name": "b",
                          "public_access_prevention": "enforced",
                          "uniform_bucket_level_access": True, "force_destroy": False,
                          "versioning": [{"enabled": True}]}, ["no-op"]),
        _rc("module.storage.google_storage_bucket_iam_policy.runner",
            "google_storage_bucket_iam_policy", "runner",
            {"policy_data": _policy((f"{bpc.ROLE_PREFIX}objects", [RUNNER]))}, ["no-op"]),
        # The live dataset still carries BigQuery's creation defaults, including the creator
        # (provisioner) OWNER entry. The authoritative dataset policy below replaces them.
        _rc("module.bigquery.google_bigquery_dataset.foundation", "google_bigquery_dataset",
            "foundation", {"project": P, "dataset_id": "bankpilot_dev",
                           "location": "europe-west2", "delete_contents_on_destroy": False,
                           "access": [{"role": "OWNER", "user_by_email": bpc.PROVISIONER_EMAIL},
                                      {"role": "OWNER", "special_group": "projectOwners"},
                                      {"role": "WRITER", "special_group": "projectWriters"},
                                      {"role": "READER", "special_group": "projectReaders"}]},
            ["no-op"]),
        _rc(DS, "google_bigquery_dataset_iam_policy", "metadata",
            {"project": P, "dataset_id": "bankpilot_dev", "policy_data": GOOD_POLICY},
            ["create"]),
    ]
    for t in bpc.pilot_tables():
        rcs.append(_rc(f'module.bigquery.google_bigquery_table.products["{t}"]',
                       "google_bigquery_table", "products",
                       {"project": P, "dataset_id": "bankpilot_dev", "table_id": t,
                        "deletion_protection": True}, ["no-op"]))
        role = "readers" if t in bpc.READER_TABLES else "tables"
        rcs.append(_rc(f'module.bigquery.google_bigquery_table_iam_member.runner["{t}"]',
                       "google_bigquery_table_iam_member", "runner",
                       {"project": P, "dataset_id": "bankpilot_dev", "table_id": t,
                        "member": RUNNER, "role": f"{bpc.ROLE_PREFIX}{role}", "condition": [],
                        "etag": "BwYx"}, ["no-op"]))
    variables = {k: {"value": v} for k, v in bpc.PILOT_VARIABLES.items()}
    variables["bucket_name"] = {"value": "private-synthetic-bucket-name"}
    variables["dataset_owner_member"] = {"value": OWNER}
    return {"format_version": "1.2", "timestamp": "2026-11-12T09:00:00Z", "errored": False,
            "variables": variables, "resource_changes": rcs}


def check(p, owner=OWNER, mode="pilot-recovery"):
    return bpc.check_plan(p, mode, dataset_owner=owner)


def _by(p, addr):
    return next(rc for rc in p["resource_changes"] if rc["address"] == addr)


# ---------------------------------------------------------------- D1 design in Terraform

PILOT_ROOT = ROOT / "infrastructure" / "terraform"


def _hcl_owner_regex() -> str:
    text = (PILOT_ROOT / "variables.tf").read_text()
    block = text[text.index('variable "dataset_owner_member"'):]
    raw = re.search(r'regex\("((?:[^"\\]|\\.)*)", var\.dataset_owner_member\)', block).group(1)
    return raw.replace("\\\\", "\\")  # HCL string escapes -> regex text


def _tf_accepts(owner: str) -> bool:
    """Python model of the dataset_owner_member validation (RE2 subset used is identical)."""
    return bool(re.match(_hcl_owner_regex(), owner)) and not owner.lower().endswith(
        ".gserviceaccount.com")


def _render(template: str, owner: str | None) -> str:
    """Render the module policy_data as Terraform's jsonencode would for these inputs."""
    bindings = []
    if '"roles/bigquery.dataOwner", members = [var.dataset_owner]' in template:
        bindings.append({"members": [owner], "role": "roles/bigquery.dataOwner"})
    if 'role = var.metadata_role, members = ["serviceAccount:${var.runner_email}"]' in template:
        bindings.append({"members": [RUNNER], "role": METADATA})
    return json.dumps({"bindings": bindings})


def test_module_dataset_policy_binds_exactly_the_owner_and_the_runner():
    module = json.loads(BIGQUERY.read_text())
    resources = module["resource"]
    assert set(resources["google_bigquery_dataset_iam_policy"]) == {"metadata"}
    assert "google_bigquery_dataset_iam_member" not in resources
    assert "google_bigquery_dataset_iam_binding" not in resources
    assert "google_bigquery_dataset_access" not in resources
    assert "access" not in resources["google_bigquery_dataset"]["foundation"]
    template = resources["google_bigquery_dataset_iam_policy"]["metadata"]["policy_data"]
    assert template == (
        '${jsonencode({bindings = [{role = "roles/bigquery.dataOwner", members = '
        '[var.dataset_owner]}, {role = var.metadata_role, members = '
        '["serviceAccount:${var.runner_email}"]}]})}')
    assert "condition" not in template
    assert "default" not in module["variable"]["dataset_owner"]
    rendered = _render(template, OWNER)
    p = recovery_plan()
    _by(p, DS)["change"]["after"]["policy_data"] = rendered
    assert check(p)["ok"]  # what Terraform will render passes the bounded recovery check


def test_runner_only_policy_reproduces_the_http_400_defect():
    """The M6S design (runner-only authoritative policy) left no direct owner."""
    old = '${jsonencode({bindings = [{role = var.metadata_role, members = ' \
          '["serviceAccount:${var.runner_email}"]}]})}'
    p = recovery_plan()
    _by(p, DS)["change"]["after"]["policy_data"] = _render(old, OWNER)
    problems = check(p)["problems"]
    assert any(pr.startswith(f"{DS}: dataset policy must be exactly the approved owner")
               for pr in problems)


def test_root_requires_and_wires_the_dataset_owner():
    variables = (PILOT_ROOT / "variables.tf").read_text()
    block = variables[variables.index('variable "dataset_owner_member"'):]
    block = block[:block.index("\n}\n") + 3]
    assert "default" not in block and "validation {" in block
    assert "!endswith(lower(var.dataset_owner_member), \".gserviceaccount.com\")" in block
    main = (PILOT_ROOT / "main.tf").read_text()
    assert "dataset_owner = var.dataset_owner_member" in main
    assert _hcl_owner_regex() == bpc.DATASET_OWNER_RE.pattern  # checker mirrors Terraform
    for env in ("dev", "test"):
        example = (PILOT_ROOT / f"environments/{env}.tfvars.example").read_text()
        assert re.search(r'^dataset_owner_member\s+= "user:[^"]+@example\.com"$', example, re.M)
        assert "yahoo" not in example


@pytest.mark.parametrize("owner,ok", [
    (OWNER, True), ("user:first.last+tag@example.co.uk", True),
    ("user:Someone@Example.com", True),
    (RUNNER, False), (PROVISIONER, False), (f"user:{bpc.RUNNER_EMAIL}", False),
    (f"user:{bpc.PROVISIONER_EMAIL.upper()}", False), ("group:g@example.com", False),
    ("domain:example.com", False), ("allUsers", False), ("user:a@example.com,user:b@x.com", False),
    ("user:a@example.com user:b@x.com", False), ("user:@example.com", False),
    ("user:a@example", False), ("projectOwners", False), ("", False),
])
def test_terraform_and_checker_agree_on_owner_validation(owner, ok):
    assert _tf_accepts(owner) is ok
    assert (bpc.dataset_owner_problems(owner) == []) is ok


# ---------------------------------------------------------------- the recovery plan

def test_recovery_plan_is_one_dataset_policy_create_and_17_unchanged():
    r = check(recovery_plan())
    assert r["ok"], r["problems"]
    assert r["mode"] == "pilot-recovery" and r["dataset_owner_approved"] is True
    assert r["creates"] == 1 and r["unchanged"] == 17 and r["deletes"] == 0
    assert r["create_addresses"] == [DS]
    assert r["counts"] == {"google_bigquery_dataset_iam_policy": 1}
    out = json.dumps(r)
    assert "approved-dataset-owner" not in out and "private-synthetic" not in out


def test_binding_order_does_not_matter():
    p = recovery_plan()
    _by(p, DS)["change"]["after"]["policy_data"] = _policy(
        (METADATA, [RUNNER]), ("roles/bigquery.dataOwner", [OWNER]))
    assert check(p)["ok"]


@pytest.mark.parametrize("owner", [
    None, "", "group:data-owners@example.invalid", "domain:example.invalid", "allUsers",
    "allAuthenticatedUsers", RUNNER, PROVISIONER,
    f"user:{bpc.RUNNER_EMAIL}", f"user:{bpc.PROVISIONER_EMAIL}",
    "projectOwners", "specialGroup:projectOwners", "user:a@example.invalid,user:b@x.invalid",
    "user:no-at-sign", " user:approved-dataset-owner@example.invalid",
])
def test_owner_must_be_an_approved_single_user(owner):
    p = recovery_plan()
    _by(p, DS)["change"]["after"]["policy_data"] = _policy(
        ("roles/bigquery.dataOwner", [owner]), (METADATA, [RUNNER]))
    r = check(p, owner=owner)
    assert not r["ok"] and r["dataset_owner_approved"] is False


POLICY_BAD = {
    "runner only (the defect)": _policy((METADATA, [RUNNER])),
    "owner only": _policy(("roles/bigquery.dataOwner", [OWNER])),
    "runner is dataset owner": _policy(("roles/bigquery.dataOwner", [RUNNER]),
                                       (METADATA, [RUNNER])),
    "provisioner is dataset owner": _policy(("roles/bigquery.dataOwner", [PROVISIONER]),
                                            (METADATA, [RUNNER])),
    "creator kept beside owner": _policy(("roles/bigquery.dataOwner", [OWNER, PROVISIONER]),
                                         (METADATA, [RUNNER])),
    "different user owner": _policy(("roles/bigquery.dataOwner", ["user:x@example.invalid"]),
                                    (METADATA, [RUNNER])),
    "primitive OWNER role": _policy(("OWNER", [OWNER]), (METADATA, [RUNNER])),
    "bigquery admin owner": _policy(("roles/bigquery.admin", [OWNER]), (METADATA, [RUNNER])),
    "runner widened to tables": _policy(("roles/bigquery.dataOwner", [OWNER]),
                                        (f"{bpc.ROLE_PREFIX}tables", [RUNNER])),
    "runner editor": _policy(("roles/bigquery.dataOwner", [OWNER]),
                             ("roles/bigquery.dataEditor", [RUNNER])),
    "extra reader binding": _policy(("roles/bigquery.dataOwner", [OWNER]),
                                    (METADATA, [RUNNER]),
                                    ("roles/bigquery.dataViewer", ["allAuthenticatedUsers"])),
    "two user owners": _policy(("roles/bigquery.dataOwner",
                                [OWNER, "user:second@example.invalid"]), (METADATA, [RUNNER])),
    "owner also gets metadata": _policy(("roles/bigquery.dataOwner", [OWNER]),
                                        (METADATA, [RUNNER, OWNER])),
    "duplicate owner binding": _policy(("roles/bigquery.dataOwner", [OWNER]),
                                       ("roles/bigquery.dataOwner", [OWNER]),
                                       (METADATA, [RUNNER])),
    "conditional owner": json.dumps({"bindings": [
        {"role": "roles/bigquery.dataOwner", "members": [OWNER],
         "condition": {"expression": "request.time < timestamp(\"2026-11-12T00:00:00Z\")"}},
        {"role": METADATA, "members": [RUNNER]}]}),
    "extra top-level key": json.dumps({"bindings": json.loads(GOOD_POLICY)["bindings"],
                                       "auditConfigs": []}),
    "not JSON": "not-json",
    "missing": None,
}


@pytest.mark.parametrize("name", sorted(POLICY_BAD))
def test_noncompliant_dataset_policies_are_rejected(name):
    p = recovery_plan()
    _by(p, DS)["change"]["after"]["policy_data"] = POLICY_BAD[name]
    assert not check(p)["ok"], name


def _set(addr, key, value):
    return lambda p: _by(p, addr)["change"]["after"].__setitem__(key, value)


def _action(addr, actions):
    return lambda p: _by(p, addr)["change"].update(actions=actions)


BUCKET = "module.storage.google_storage_bucket.artifacts"
BUCKET_POLICY = "module.storage.google_storage_bucket_iam_policy.runner"
DATASET = "module.bigquery.google_bigquery_dataset.foundation"
TABLE = 'module.bigquery.google_bigquery_table.products["customers"]'
TABLE_MEMBER = 'module.bigquery.google_bigquery_table_iam_member.runner["customers"]'

PLAN_BAD = {
    "dataset policy replace (tainted)": _action(DS, ["delete", "create"]),
    "dataset policy update": _action(DS, ["update"]),
    "dataset policy no-op (already applied)": _action(DS, ["no-op"]),
    "dataset policy missing": lambda p: p["resource_changes"].remove(_by(p, DS)),
    "bucket update": _action(BUCKET, ["update"]),
    "bucket policy update": _action(BUCKET_POLICY, ["update"]),
    "dataset replace": _action(DATASET, ["delete", "create"]),
    "dataset update": _action(DATASET, ["update"]),
    "table delete": _action(TABLE, ["delete"]),
    "table member update": _action(TABLE_MEMBER, ["update"]),
    "table member widened": _set(TABLE_MEMBER, "role", f"{bpc.ROLE_PREFIX}tables"),
    "bucket policy widened": _set(BUCKET_POLICY, "policy_data", _policy(
        (f"{bpc.ROLE_PREFIX}objects", [RUNNER, PROVISIONER]))),
    "dataset delete_contents": _set(DATASET, "delete_contents_on_destroy", True),
    "table unprotected": _set(TABLE, "deletion_protection", False),
    "no-op table missing": lambda p: p["resource_changes"].remove(_by(p, TABLE)),
    "extra dataset iam member": lambda p: p["resource_changes"].append(_rc(
        "module.bigquery.google_bigquery_dataset_iam_member.owner",
        "google_bigquery_dataset_iam_member", "owner",
        {"project": P, "role": "roles/bigquery.dataOwner", "member": OWNER}, ["create"])),
    "dataset access resource": lambda p: p["resource_changes"].append(_rc(
        "module.bigquery.google_bigquery_dataset_access.owner",
        "google_bigquery_dataset_access", "owner", {"project": P}, ["create"])),
    "project member": lambda p: p["resource_changes"].append(_rc(
        "google_project_iam_member.x", "google_project_iam_member", "x",
        {"project": P, "role": METADATA, "member": RUNNER}, ["create"])),
    "any drift": lambda p: p.update(resource_drift=[{"address": DATASET,
                                                     "change": {"actions": ["update"]}}]),
    "data source read": lambda p: p["resource_changes"].append(_rc(
        "module.identity.data.google_project_iam_policy.project",
        "google_project_iam_policy", "project", {}, ["read"], mode="data")),
    "errored": lambda p: p.update(errored=True),
    "prefix variable": lambda p: p["variables"]["prefix"].update(value="fincrime-m5"),
    "provisioner variable": lambda p: p["variables"]["provisioner_service_account"].update(
        value=f"other@{P}.iam.gserviceaccount.com"),
    "bucket name missing": lambda p: p["variables"].pop("bucket_name"),
    "owner variable differs": lambda p: p["variables"]["dataset_owner_member"].update(
        value="user:someone-else@example.invalid"),
    "owner variable missing": lambda p: p["variables"].pop("dataset_owner_member"),
}


@pytest.mark.parametrize("name", sorted(PLAN_BAD))
def test_adversarial_recovery_plans_are_rejected(name):
    p = recovery_plan()
    PLAN_BAD[name](p)
    assert not check(p)["ok"], name


def test_modes_are_isolated():
    assert not check(recovery_plan(), mode="pilot")["ok"]  # 17 no-ops are not 18 creates
    for mode in bpc.BOOTSTRAP_MODES:
        assert not bpc.check_plan(recovery_plan(), mode)["ok"], mode
    full = copy.deepcopy(recovery_plan())
    for rc in full["resource_changes"]:
        rc["change"]["actions"] = ["create"]
    assert check(full, mode="pilot")["ok"]  # same content as a fresh compliant deployment
    assert not check(full)["ok"]


def test_recovery_expected_addresses():
    assert bpc.pilot_addresses() - {DS} == {rc["address"] for rc in recovery_plan()[
        "resource_changes"] if rc["change"]["actions"] == ["no-op"]}
    assert len(bpc.pilot_addresses() - {DS}) == 17


def test_cli_requires_owner_and_never_prints_it():
    def cli(*args):
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *args],
                              input=json.dumps(recovery_plan()), capture_output=True,
                              text=True, check=False)
    ok = cli("--mode", "pilot-recovery", f"--dataset-owner={OWNER}")
    assert ok.returncode == 0 and json.loads(ok.stdout)["ok"]
    assert "approved-dataset-owner" not in ok.stdout + ok.stderr
    missing = cli("--mode", "pilot-recovery")
    assert missing.returncode == 1
    assert "dataset owner not supplied" in missing.stdout
    other = cli("--mode", "pilot-recovery", "--dataset-owner=user:someone@example.invalid")
    assert other.returncode == 1
