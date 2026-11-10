"""M6S: offline checks for the access-window, revocation and first pilot saved plans.

Synthetic ``terraform show -json`` documents only; no Terraform, gcloud or network access.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap_plan_check.py"
TF = ROOT / "infrastructure" / "terraform"
_spec = importlib.util.spec_from_file_location("bootstrap_plan_check_m6s", SCRIPT)
bpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bpc)

P = bpc.PROJECT
PLANNED = "2026-11-11T09:00:00Z"
EXPIRY = "2026-11-11T16:00:00Z"  # 7 h window
OPERATOR = "user:named-operator@example.invalid"
RUNNER = f"serviceAccount:{bpc.RUNNER_EMAIL}"


def _rc(address, kind, name, values, actions, mode="managed"):
    before = None if actions == ["create"] else values
    after = None if actions == ["delete"] else values
    return {"address": address, "mode": mode, "type": kind, "name": name,
            "change": {"actions": actions, "before": before, "after": after}}


def _existing() -> list[dict]:
    roles = bpc.expected_roles()
    out = [_rc(f"google_service_account.{n}", "google_service_account", n,
               {"project": P, "account_id": f"bankpilot-dev-{n}"}, ["no-op"])
           for n in ("runner", "provisioner")]
    for addr in sorted(a for a in bpc.durable_addresses() if "custom_role" in a):
        name = addr.split(".")[1].split("[")[0]
        key = re.search(r'\["(\w+)"\]', addr)
        rid = f"bankpilot_dev_{key.group(1) if key else name}"
        out.append(_rc(addr, "google_project_iam_custom_role", name,
                       {"project": P, "role_id": rid, "permissions": sorted(roles[rid])},
                       ["no-op"]))
    out.append(_rc("google_project_iam_member.runner_jobs", "google_project_iam_member",
                   "runner_jobs", {"project": P, "condition": [],
                                   "role": f"{bpc.ROLE_PREFIX}jobs", "member": RUNNER},
                   ["no-op"]))
    out.append(_rc(bpc.AUDIT_ADDRESS, "google_project_iam_audit_config", "iamcredentials",
                   {"project": P, "service": "iam.googleapis.com",
                    "audit_log_config": [{"log_type": t, "exempted_members": []}
                                         for t in ("ADMIN_READ", "DATA_READ")]},
                   ["no-op"]))
    return out


def _grants(actions, expiry=EXPIRY, operator=OPERATOR) -> list[dict]:
    cond = [bpc.expiry_condition(expiry)]
    return [
        _rc(bpc.PROVISIONER_GRANT, "google_project_iam_member", "provisioner",
            {"project": P, "role": f"{bpc.ROLE_PREFIX}provision",
             "member": f"serviceAccount:{bpc.PROVISIONER_EMAIL}", "condition": cond}, actions),
        _rc(bpc.IMPERSONATION_GRANT, "google_service_account_iam_member",
            "operator_impersonation",
            {"service_account_id": f"projects/{P}/serviceAccounts/{bpc.PROVISIONER_EMAIL}",
             "role": f"{bpc.ROLE_PREFIX}impersonate", "member": operator, "condition": cond},
            actions),
    ]


def _variables(access: bool, expiry) -> dict:
    v = {k: {"value": val} for k, val in bpc.EXPECTED_VARIABLES.items()}
    v["provisioner_access_enabled"] = {"value": access}
    v["access_expiry"] = {"value": expiry}
    v["operator_member"] = {"value": OPERATOR}
    return v


def access_plan() -> dict:
    return {"format_version": "1.2", "timestamp": PLANNED, "errored": False,
            "variables": _variables(True, EXPIRY),
            "resource_changes": [*_existing(), *_grants(["create"])]}


def revoke_plan() -> dict:
    return {"format_version": "1.2", "timestamp": PLANNED, "errored": False,
            "variables": _variables(False, None),
            "resource_changes": [*_existing(), *_grants(["delete"])]}


def _by(p, addr):
    return next(rc for rc in p["resource_changes"] if rc["address"] == addr)


def _cond(p, addr):
    return _by(p, addr)["change"]["after"]["condition"][0]


# ---------------------------------------------------------------- access window

def test_access_plan_creates_exactly_the_two_conditional_grants():
    r = bpc.check_plan(access_plan(), "access")
    assert r["ok"], r["problems"]
    assert r["creates"] == 2 and r["unchanged"] == 11 and r["deletes"] == 0
    assert r["create_addresses"] == sorted(bpc.ACCESS_ADDRESSES)
    assert r["counts"] == {"google_project_iam_member": 1,
                           "google_service_account_iam_member": 1}
    assert r["access_expiry"] == EXPIRY and r["window_hours"] == 7.0
    assert "named-operator" not in json.dumps(r)


def test_eight_hours_exactly_is_allowed():
    p = access_plan()
    expiry = "2026-11-11T17:00:00Z"
    p["variables"]["access_expiry"]["value"] = expiry
    p["resource_changes"][-2:] = _grants(["create"], expiry)
    assert bpc.check_plan(p, "access")["ok"]


def _with_expiry(expiry):
    def f(p):
        p["variables"]["access_expiry"]["value"] = expiry
        p["resource_changes"][-2:] = _grants(["create"], expiry)
    return f


def _set(path_fn, key, value):
    def f(p):
        path_fn(p)[key] = value
    return f


ACCESS_BAD = {
    "window over 8 h": _with_expiry("2026-11-11T17:00:01Z"),
    "expiry before plan": _with_expiry("2026-11-11T08:59:59Z"),
    "expiry not UTC Z": _with_expiry("2026-11-11T16:00:00+01:00"),
    "plan timestamp missing": lambda p: p.pop("timestamp"),
    "condition differs from variable": _set(
        lambda p: _cond(p, bpc.PROVISIONER_GRANT), "expression",
        'request.time < timestamp("2026-11-12T16:00:00Z")'),
    "impersonation condition removed": _set(
        lambda p: _by(p, bpc.IMPERSONATION_GRANT)["change"]["after"], "condition", []),
    "project grant unconditional": _set(
        lambda p: _by(p, bpc.PROVISIONER_GRANT)["change"]["after"], "condition", []),
    "group member": _set(lambda p: _by(p, bpc.IMPERSONATION_GRANT)["change"]["after"],
                         "member", "group:ops@example.invalid"),
    "member differs from variable": _set(
        lambda p: _by(p, bpc.IMPERSONATION_GRANT)["change"]["after"],
        "member", "user:someone-else@example.invalid"),
    "operator variable is a domain": _set(
        lambda p: p["variables"]["operator_member"], "value", "domain:example.invalid"),
    "grant on runner account": _set(
        lambda p: _by(p, bpc.IMPERSONATION_GRANT)["change"]["after"], "service_account_id",
        f"projects/{P}/serviceAccounts/{bpc.RUNNER_EMAIL}"),
    "token creator role": _set(lambda p: _by(p, bpc.IMPERSONATION_GRANT)["change"]["after"],
                               "role", "roles/iam.serviceAccountTokenCreator"),
    "provisioner gets jobs role": _set(lambda p: _by(p, bpc.PROVISIONER_GRANT)["change"]["after"],
                                       "role", f"{bpc.ROLE_PREFIX}jobs"),
    "runner member gets grant": _set(lambda p: _by(p, bpc.PROVISIONER_GRANT)["change"]["after"],
                                     "member", RUNNER),
    "access variable false": _set(lambda p: p["variables"]["provisioner_access_enabled"],
                                  "value", False),
    "audit config deleted": lambda p: _by(p, bpc.AUDIT_ADDRESS)["change"].update(
        actions=["delete"]),
    "role widened": lambda p: _by(p, "google_project_iam_custom_role.provision")[
        "change"].update(actions=["update"]),
    "only one grant": lambda p: p["resource_changes"].pop(),
    "drift": lambda p: p.update(resource_drift=[{"address": "google_service_account.runner",
                                                 "change": {"actions": ["update"]}}]),
    "extra owner member": lambda p: p["resource_changes"].append(_rc(
        "google_project_iam_member.extra", "google_project_iam_member", "extra",
        {"project": P, "role": "roles/owner", "member": OPERATOR}, ["create"])),
}


@pytest.mark.parametrize("name", sorted(ACCESS_BAD))
def test_adversarial_access_plans_are_rejected(name):
    p = access_plan()
    ACCESS_BAD[name](p)
    assert not bpc.check_plan(p, "access")["ok"], name


def test_access_plan_is_not_accepted_by_other_modes():
    for mode in ("initial", "recovery", "revoke", "pilot"):
        assert not bpc.check_plan(access_plan(), mode)["ok"], mode


# ---------------------------------------------------------------- revocation

def test_revoke_plan_deletes_exactly_the_two_grants():
    r = bpc.check_plan(revoke_plan(), "revoke")
    assert r["ok"], r["problems"]
    assert r["deletes"] == 2 and r["unchanged"] == 11 and r["creates"] == 0
    assert r["delete_addresses"] == sorted(bpc.ACCESS_ADDRESSES)
    assert r["already_removed"] == []


def test_revoke_after_emergency_manual_removal():
    p = revoke_plan()
    p["resource_changes"] = p["resource_changes"][:-2]
    p["resource_drift"] = [{"address": a, "change": {"actions": ["delete"]}}
                           for a in sorted(bpc.ACCESS_ADDRESSES)]
    r = bpc.check_plan(p, "revoke")
    assert r["ok"], r["problems"]
    assert r["already_removed"] == sorted(bpc.ACCESS_ADDRESSES) and r["deletes"] == 0


REVOKE_BAD = {
    "access still enabled": _set(lambda p: p["variables"]["provisioner_access_enabled"],
                                 "value", True),
    "expiry still set": _set(lambda p: p["variables"]["access_expiry"], "value", EXPIRY),
    "durable role deleted": lambda p: _by(p, "google_project_iam_custom_role.impersonate")[
        "change"].update(actions=["delete"]),
    "audit config deleted": lambda p: _by(p, bpc.AUDIT_ADDRESS)["change"].update(
        actions=["delete"]),
    "one grant kept": lambda p: p["resource_changes"].pop(),
    "other drift": lambda p: p.update(resource_drift=[{
        "address": "google_project_iam_member.runner_jobs", "change": {"actions": ["delete"]}}]),
    "grant drift is an update": lambda p: p.update(resource_drift=[{
        "address": bpc.PROVISIONER_GRANT, "change": {"actions": ["update"]}}]),
    "new grant created": lambda p: p["resource_changes"].append(_grants(["create"])[0]),
}


@pytest.mark.parametrize("name", sorted(REVOKE_BAD))
def test_adversarial_revoke_plans_are_rejected(name):
    p = revoke_plan()
    REVOKE_BAD[name](p)
    assert not bpc.check_plan(p, "revoke")["ok"], name


def test_disabled_modes_require_null_expiry():
    from_m6q = revoke_plan()
    from_m6q["resource_changes"] = from_m6q["resource_changes"][:-2]
    assert bpc.check_plan(from_m6q, "recovery")["ok"] is False  # audit is no-op, not create
    from_m6q["variables"]["access_expiry"]["value"] = EXPIRY
    assert ("variable access_expiry must be null while access is disabled"
            in bpc.check_plan(from_m6q, "recovery")["problems"])


# ---------------------------------------------------------------- pilot deployment

def _policy(role):
    return json.dumps({"bindings": [{"role": role, "members": [RUNNER]}]})


def pilot_plan() -> dict:
    tables = bpc.pilot_tables()
    lbl = {"environment": "dev"}
    rcs = [
        _rc("module.storage.google_storage_bucket.artifacts", "google_storage_bucket",
            "artifacts", {"project": P, "location": "europe-west2", "name": "b",
                          "public_access_prevention": "enforced",
                          "uniform_bucket_level_access": True, "force_destroy": False,
                          "versioning": [{"enabled": True}], "labels": lbl}, ["create"]),
        _rc("module.storage.google_storage_bucket_iam_policy.runner",
            "google_storage_bucket_iam_policy", "runner",
            {"policy_data": _policy(f"{bpc.ROLE_PREFIX}objects")}, ["create"]),
        _rc("module.bigquery.google_bigquery_dataset.foundation", "google_bigquery_dataset",
            "foundation", {"project": P, "dataset_id": "bankpilot_dev",
                           "location": "europe-west2", "delete_contents_on_destroy": False},
            ["create"]),
        _rc("module.bigquery.google_bigquery_dataset_iam_policy.metadata",
            "google_bigquery_dataset_iam_policy", "metadata",
            {"project": P, "policy_data": _policy(f"{bpc.ROLE_PREFIX}metadata")}, ["create"]),
    ]
    for t in tables:
        rcs.append(_rc(f'module.bigquery.google_bigquery_table.products["{t}"]',
                       "google_bigquery_table", "products",
                       {"project": P, "dataset_id": "bankpilot_dev", "table_id": t,
                        "deletion_protection": True}, ["create"]))
        role = "readers" if t in bpc.READER_TABLES else "tables"
        rcs.append(_rc(f'module.bigquery.google_bigquery_table_iam_member.runner["{t}"]',
                       "google_bigquery_table_iam_member", "runner",
                       {"project": P, "dataset_id": "bankpilot_dev", "table_id": t,
                        "member": RUNNER, "role": f"{bpc.ROLE_PREFIX}{role}"}, ["create"]))
    variables = {k: {"value": v} for k, v in bpc.PILOT_VARIABLES.items()}
    variables["bucket_name"] = {"value": "private-synthetic-bucket-name"}
    # The identity module's data source legitimately records the Owner binding in prior_state.
    prior = {"values": {"root_module": {"child_modules": [{"resources": [{
        "address": "module.identity.data.google_project_iam_policy.project", "mode": "data",
        "values": {"policy_data": json.dumps({"bindings": [
            {"role": "roles/owner", "members": [OPERATOR]}]})}}]}]}}}
    return {"format_version": "1.2", "timestamp": PLANNED, "errored": False,
            "variables": variables, "resource_changes": rcs, "prior_state": prior}


def test_pilot_plan_is_18_runner_only_creates():
    r = bpc.check_plan(pilot_plan(), "pilot")
    assert r["ok"], r["problems"]
    assert r["creates"] == 18 and r["unchanged"] == 0 and r["deletes"] == 0
    assert r["counts"] == {"google_storage_bucket": 1, "google_storage_bucket_iam_policy": 1,
                           "google_bigquery_dataset": 1,
                           "google_bigquery_dataset_iam_policy": 1,
                           "google_bigquery_table": 7, "google_bigquery_table_iam_member": 7}
    out = json.dumps(r)
    assert "private-synthetic-bucket-name" not in out and "named-operator" not in out


def _pa(addr):
    return lambda p: _by(p, addr)["change"]["after"]


BUCKET = _pa("module.storage.google_storage_bucket.artifacts")
DATASET = _pa("module.bigquery.google_bigquery_dataset.foundation")
PILOT_BAD = {
    "bucket public access inherited": _set(BUCKET, "public_access_prevention", "inherited"),
    "bucket in US": _set(BUCKET, "location", "US"),
    "bucket force_destroy": _set(BUCKET, "force_destroy", True),
    "bucket versioning off": _set(BUCKET, "versioning", [{"enabled": False}]),
    "bucket policy allUsers": _set(_pa("module.storage.google_storage_bucket_iam_policy.runner"),
                                   "policy_data", json.dumps({"bindings": [{
                                       "role": "roles/storage.objectViewer",
                                       "members": ["allUsers"]}]})),
    "bucket policy extra member": _set(
        _pa("module.storage.google_storage_bucket_iam_policy.runner"), "policy_data",
        json.dumps({"bindings": [{"role": f"{bpc.ROLE_PREFIX}objects",
                                  "members": [RUNNER, OPERATOR]}]})),
    "dataset policy wrong role": _set(
        _pa("module.bigquery.google_bigquery_dataset_iam_policy.metadata"), "policy_data",
        _policy(f"{bpc.ROLE_PREFIX}tables")),
    "dataset delete contents": _set(DATASET, "delete_contents_on_destroy", True),
    "dataset other location": _set(DATASET, "location", "EU"),
    "table unprotected": _set(_pa('module.bigquery.google_bigquery_table.products["customers"]'),
                              "deletion_protection", False),
    "reader table gets write role": _set(
        _pa('module.bigquery.google_bigquery_table_iam_member.runner["customers"]'),
        "role", f"{bpc.ROLE_PREFIX}tables"),
    "table member is provisioner": _set(
        _pa('module.bigquery.google_bigquery_table_iam_member.runner["account_daily"]'),
        "member", f"serviceAccount:{bpc.PROVISIONER_EMAIL}"),
    "project member added": lambda p: p["resource_changes"].append(_rc(
        "google_project_iam_member.x", "google_project_iam_member", "x",
        {"project": P, "role": f"{bpc.ROLE_PREFIX}jobs", "member": RUNNER}, ["create"])),
    "table missing": lambda p: p["resource_changes"].pop(),
    "replace dataset": lambda p: _by(p, "module.bigquery.google_bigquery_dataset.foundation")[
        "change"].update(actions=["delete", "create"]),
    "provisioner variable differs": _set(
        lambda p: p["variables"]["provisioner_service_account"], "value",
        f"other-provisioner@{P}.iam.gserviceaccount.com"),
    "prefix is default fincrime-m5": _set(lambda p: p["variables"]["prefix"], "value",
                                          "fincrime-m5"),
    "deferred data source read": lambda p: p["resource_changes"].append(_rc(
        "module.identity.data.google_project_iam_policy.project", "google_project_iam_policy",
        "project", {}, ["read"], mode="data")),
    "drift": lambda p: p.update(resource_drift=[{"address": "x"}]),
    "errored": lambda p: p.update(errored=True),
}


@pytest.mark.parametrize("name", sorted(PILOT_BAD))
def test_adversarial_pilot_plans_are_rejected(name):
    p = pilot_plan()
    PILOT_BAD[name](p)
    assert not bpc.check_plan(p, "pilot")["ok"], name


# ---------------------------------------------------------------- consistency with Terraform

def test_expiry_condition_matches_bootstrap_terraform():
    text = (TF / "bootstrap" / "main.tf").read_text()
    assert 'title       = "expires-${replace(coalesce(var.access_expiry, "unset"), ":", "-")}"' \
        in text
    assert 'description = "M6K time-bound access; remove explicitly after expiry"' in text
    assert ('expression  = "request.time < timestamp(\\"${coalesce(var.access_expiry, '
            '"1970-01-01T00:00:00Z")}\\")"') in text
    assert bpc.expiry_condition(EXPIRY) == {
        "title": "expires-2026-11-11T16-00-00Z",
        "description": "M6K time-bound access; remove explicitly after expiry",
        "expression": 'request.time < timestamp("2026-11-11T16:00:00Z")'}
    for addr, kind, name in ((bpc.PROVISIONER_GRANT, "google_project_iam_member", "provisioner"),
                             (bpc.IMPERSONATION_GRANT, "google_service_account_iam_member",
                              "operator_impersonation")):
        body = text[text.index(f'resource "{kind}" "{name}"'):].split("\n}\n", 1)[0]
        assert "count              = local.access_count" in body or \
            "count   = local.access_count" in body
        assert "condition {" in body and addr.endswith("[0]")
    variables = (TF / "bootstrap" / "variables.tf").read_text()
    assert 'timeadd(plantimestamp(), "8h")' in variables


def test_pilot_addresses_match_modules():
    seen = set()
    for module in ("storage", "bigquery"):
        data = json.loads((TF / "modules" / module / "main.tf.json").read_text())
        for kind, blocks in data["resource"].items():
            for name, body in blocks.items():
                if "for_each" in body:
                    seen |= {f'module.{module}.{kind}.{name}["{t}"]' for t in bpc.pilot_tables()}
                else:
                    seen.add(f"module.{module}.{kind}.{name}")
    assert seen == bpc.pilot_addresses() and len(seen) == 18


def test_cli_accepts_new_modes():
    out = subprocess.run([sys.executable, "-I", str(SCRIPT), "--mode", "access"],
                         input=json.dumps(access_plan()), capture_output=True, text=True,
                         check=False)
    assert out.returncode == 0 and json.loads(out.stdout)["ok"]
    bad = subprocess.run([sys.executable, "-I", str(SCRIPT), "--mode", "pilot"],
                         input=json.dumps(access_plan()), capture_output=True, text=True,
                         check=False)
    assert bad.returncode == 1


# ---------------------------------------------------------------- M6S runner etag drift

RUNNER_ID = f"{P}/{bpc.ROLE_PREFIX}jobs/{RUNNER}"


def _runner_side(etag):
    return {"condition": [], "etag": etag, "id": RUNNER_ID, "member": RUNNER, "project": P,
            "role": f"{bpc.ROLE_PREFIX}jobs"}


def _etag_drift():
    return {"address": bpc.RUNNER_GRANT, "mode": "managed",
            "type": "google_project_iam_member", "name": "runner_jobs",
            "provider_name": "registry.terraform.io/hashicorp/google",
            "change": {"actions": ["update"], "before": _runner_side("BwYOldEtag0="),
                       "after": _runner_side("BwYNewEtag1="), "after_unknown": {},
                       "before_sensitive": {"condition": []},
                       "after_sensitive": {"condition": []}}}


def access_plan_with_etag_drift():
    p = access_plan()
    p["resource_drift"] = [_etag_drift()]
    return p


def test_access_accepts_exact_runner_etag_only_drift():
    r = bpc.check_plan(access_plan_with_etag_drift(), "access")
    assert r["ok"], r["problems"]
    assert r["accepted_etag_drift"] == [bpc.RUNNER_GRANT]
    assert r["creates"] == 2 and r["unchanged"] == 11
    assert bpc.check_plan(access_plan(), "access")["accepted_etag_drift"] == []


def _drift(fn):
    def apply(p):
        fn(p["resource_drift"][0])
    return apply


def _both(key, value):
    def f(d):
        d["change"]["before"][key] = value
        d["change"]["after"][key] = value
    return f


ETAG_BAD = {
    "role changed after": _drift(lambda d: d["change"]["after"].update(
        role=f"{bpc.ROLE_PREFIX}provision")),
    "member changed after": _drift(lambda d: d["change"]["after"].update(
        member=f"serviceAccount:{bpc.PROVISIONER_EMAIL}")),
    "project changed after": _drift(lambda d: d["change"]["after"].update(project="other-p")),
    "condition added after": _drift(lambda d: d["change"]["after"].update(
        condition=[bpc.expiry_condition(EXPIRY)])),
    "id changed after": _drift(lambda d: d["change"]["after"].update(id="x")),
    "both sides wrong role": _drift(_both("role", f"{bpc.ROLE_PREFIX}objects")),
    "both sides wrong member": _drift(_both("member", "user:someone@example.invalid")),
    "both sides conditional": _drift(_both("condition", [bpc.expiry_condition(EXPIRY)])),
    "both sides empty id": _drift(_both("id", "")),
    "etag unchanged": _drift(lambda d: d["change"]["after"].update(etag="BwYOldEtag0=")),
    "etag missing after": _drift(lambda d: d["change"]["after"].pop("etag")),
    "etag empty before": _drift(lambda d: d["change"]["before"].update(etag="")),
    "member missing both": _drift(lambda d: [d["change"][s].pop("member")
                                             for s in ("before", "after")]),
    "extra attribute": _drift(_both("extra", "x")),
    "after null": _drift(lambda d: d["change"].update(after=None)),
    "unknown after values": _drift(lambda d: d["change"].update(after_unknown={"etag": True})),
    "delete drift": _drift(lambda d: d["change"].update(actions=["delete"])),
    "replace drift": _drift(lambda d: d["change"].update(actions=["delete", "create"])),
    "no-op labelled drift": _drift(lambda d: d["change"].update(actions=["no-op"])),
    "indexed address": _drift(lambda d: d.update(index=0)),
    "data mode": _drift(lambda d: d.update(mode="data")),
    "wrong type": _drift(lambda d: d.update(type="google_project_iam_binding")),
    "different resource etag drift": _drift(lambda d: d.update(
        address="google_project_iam_audit_config.iamcredentials[0]")),
    "second identical entry": lambda p: p["resource_drift"].append(_etag_drift()),
    "extra drift on role": lambda p: p["resource_drift"].append({
        "address": "google_project_iam_custom_role.provision",
        "change": {"actions": ["update"]}}),
    "runner grant not no-op in changes": lambda p: _by(p, bpc.RUNNER_GRANT)["change"].update(
        actions=["update"]),
    "role permissions changed": lambda p: _by(p, "google_project_iam_custom_role.provision")[
        "change"]["after"].update(permissions=["resourcemanager.projects.setIamPolicy"]),
}


@pytest.mark.parametrize("name", sorted(ETAG_BAD))
def test_adversarial_runner_drift_is_rejected(name):
    p = access_plan_with_etag_drift()
    ETAG_BAD[name](p)
    r = bpc.check_plan(p, "access")
    assert not r["ok"], name
    assert r["accepted_etag_drift"] in ([], [bpc.RUNNER_GRANT])


@pytest.mark.parametrize("mode", ["initial", "recovery"])
def test_etag_exception_is_not_available_in_initial_or_recovery(mode):
    p = revoke_plan()
    p["resource_changes"] = p["resource_changes"][:-2]
    p["resource_drift"] = [_etag_drift()]
    assert "drift detected outside Terraform: google_project_iam_member.runner_jobs" in \
        bpc.check_plan(p, mode)["problems"]


def test_access_adversarial_cases_still_fail_with_etag_drift_present():
    for name, mutate in ACCESS_BAD.items():
        p = access_plan_with_etag_drift()
        mutate(p)
        assert not bpc.check_plan(p, "access")["ok"], name


def test_etag_drift_helper_layers_directly():
    assert bpc.runner_etag_drift_problems(_etag_drift()) == []
    other = _etag_drift()
    other["address"] = "google_project_iam_member.provisioner[0]"
    assert bpc.runner_etag_drift_problems(other) == [
        "drift detected outside Terraform: google_project_iam_member.provisioner[0]"]
    for key, value in (("member", "user:someone@example.invalid"),
                       ("role", f"{bpc.ROLE_PREFIX}objects")):
        moved = _etag_drift()
        moved["change"]["after"][key] = value
        assert (f"{bpc.RUNNER_GRANT}: drift changes more than etag: ['{key}']"
                in bpc.runner_etag_drift_problems(moved))


# ---------------------------------------------------------------- M6S revoke etag drift

AUDIT_ID = f"projects/{P}/audit_config/iam.googleapis.com"


def _audit_side(etag):
    return {"audit_log_config": [{"exempted_members": [], "log_type": "ADMIN_READ"},
                                 {"exempted_members": [], "log_type": "DATA_READ"}],
            "etag": etag, "id": AUDIT_ID, "project": P, "service": "iam.googleapis.com"}


def _audit_drift():
    return {"address": bpc.AUDIT_ADDRESS, "mode": "managed",
            "type": "google_project_iam_audit_config", "name": "iamcredentials", "index": 0,
            "provider_name": "registry.terraform.io/hashicorp/google",
            "change": {"actions": ["update"], "before": _audit_side("BwYAccessEtag="),
                       "after": _audit_side("BwYRevokeEtag="),
                       "after_unknown": {"audit_log_config": [{"exempted_members": []},
                                                              {"exempted_members": []}]}}}


def revoke_plan_with_drift(*entries):
    p = revoke_plan()
    p["resource_drift"] = [e() for e in entries]
    return p


@pytest.mark.parametrize("entries", [
    (), (_etag_drift,), (_audit_drift,), (_etag_drift, _audit_drift),
    (_audit_drift, _etag_drift)])
def test_revoke_accepts_exact_etag_only_drift(entries):
    r = bpc.check_plan(revoke_plan_with_drift(*entries), "revoke")
    assert r["ok"], r["problems"]
    assert r["deletes"] == 2 and r["unchanged"] == 11 and r["creates"] == 0
    assert r["accepted_etag_drift"] == sorted(e()["address"] for e in entries)


def test_revoke_after_emergency_removal_with_policy_etag_drift():
    p = revoke_plan_with_drift(_etag_drift, _audit_drift)
    p["resource_changes"] = p["resource_changes"][:-2]
    p["resource_drift"] += [{"address": a, "change": {"actions": ["delete"]}}
                            for a in sorted(bpc.ACCESS_ADDRESSES)]
    r = bpc.check_plan(p, "revoke")
    assert r["ok"], r["problems"]
    assert r["already_removed"] == sorted(bpc.ACCESS_ADDRESSES) and r["deletes"] == 0


def _a(fn):
    def apply(p):
        fn(next(d for d in p["resource_drift"] if d["address"] == bpc.AUDIT_ADDRESS))
    return apply


def _a_both(fn):
    def f(d):
        fn(d["change"]["before"])
        fn(d["change"]["after"])
    return _a(f)


AUDIT_BAD = {
    "service changed after": _a(lambda d: d["change"]["after"].update(service="allServices")),
    "service other on both": _a_both(lambda s: s.update(service="storage.googleapis.com")),
    "DATA_WRITE added after": _a(lambda d: d["change"]["after"]["audit_log_config"].append(
        {"exempted_members": [], "log_type": "DATA_WRITE"})),
    "DATA_READ removed on both": _a_both(lambda s: s["audit_log_config"].pop()),
    "exemption added after": _a(lambda d: d["change"]["after"]["audit_log_config"][1].update(
        exempted_members=["user:x@example.invalid"])),
    "exemption on both": _a_both(lambda s: s["audit_log_config"][0].update(
        exempted_members=["serviceAccount:x@example.invalid"])),
    "exempted_members missing": _a_both(lambda s: s["audit_log_config"][0].pop(
        "exempted_members")),
    "extra key in log config": _a_both(lambda s: s["audit_log_config"][0].update(x=1)),
    "log config not a list": _a_both(lambda s: s.update(audit_log_config={"a": 1})),
    "project changed after": _a(lambda d: d["change"]["after"].update(project="other-p")),
    "id changed after": _a(lambda d: d["change"]["after"].update(id="x")),
    "id empty on both": _a_both(lambda s: s.update(id="")),
    "extra attribute": _a_both(lambda s: s.update(extra="x")),
    "service missing": _a_both(lambda s: s.pop("service")),
    "etag unchanged": _a(lambda d: d["change"]["after"].update(etag="BwYAccessEtag=")),
    "etag missing before": _a(lambda d: d["change"]["before"].pop("etag")),
    "etag not a string": _a(lambda d: d["change"]["after"].update(etag=7)),
    "before null": _a(lambda d: d["change"].update(before=None)),
    "nested unknown": _a(lambda d: d["change"].update(
        after_unknown={"audit_log_config": [{"exempted_members": True}]})),
    "delete action": _a(lambda d: d["change"].update(actions=["delete"])),
    "replace action": _a(lambda d: d["change"].update(actions=["delete", "create"])),
    "create action": _a(lambda d: d["change"].update(actions=["create"])),
    "index missing": _a(lambda d: d.pop("index")),
    "index 1": _a(lambda d: d.update(index=1)),
    "wrong name": _a(lambda d: d.update(name="other")),
    "wrong type": _a(lambda d: d.update(type="google_project_iam_policy")),
    "data mode": _a(lambda d: d.update(mode="data")),
    "duplicate audit drift": lambda p: p["resource_drift"].append(_audit_drift()),
    "duplicate runner drift": lambda p: p["resource_drift"].append(_etag_drift()),
    "runner member changed": lambda p: p["resource_drift"][0]["change"]["after"].update(
        member=f"serviceAccount:{bpc.PROVISIONER_EMAIL}"),
    "runner conditional on both": lambda p: [p["resource_drift"][0]["change"][s].update(
        condition=[bpc.expiry_condition(EXPIRY)]) for s in ("before", "after")],
    "provisioner grant etag drift": lambda p: p["resource_drift"].append(
        {**_etag_drift(), "address": bpc.PROVISIONER_GRANT, "name": "provisioner", "index": 0}),
    "impersonation grant update drift": lambda p: p["resource_drift"].append(
        {"address": bpc.IMPERSONATION_GRANT, "change": {"actions": ["update"]}}),
    "role drift": lambda p: p["resource_drift"].append(
        {"address": "google_project_iam_custom_role.provision",
         "change": {"actions": ["update"]}}),
    "audit config deleted in changes": lambda p: _by(p, bpc.AUDIT_ADDRESS)["change"].update(
        actions=["delete"]),
    "runner deleted in changes": lambda p: _by(p, bpc.RUNNER_GRANT)["change"].update(
        actions=["delete"]),
    "access re-enabled": lambda p: p["variables"]["provisioner_access_enabled"].update(
        value=True),
}


@pytest.mark.parametrize("name", sorted(AUDIT_BAD))
def test_adversarial_revoke_drift_is_rejected(name):
    p = revoke_plan_with_drift(_etag_drift, _audit_drift)
    AUDIT_BAD[name](p)
    r = bpc.check_plan(p, "revoke")
    assert not r["ok"], name


def test_revoke_adversarial_cases_still_fail_with_etag_drift_present():
    for name, mutate in REVOKE_BAD.items():
        p = revoke_plan_with_drift(_etag_drift, _audit_drift)
        mutate(p)
        assert not bpc.check_plan(p, "revoke")["ok"], name


def test_access_mode_still_rejects_audit_etag_drift():
    p = access_plan_with_etag_drift()
    p["resource_drift"].append(_audit_drift())
    r = bpc.check_plan(p, "access")
    assert f"drift detected outside Terraform: {bpc.AUDIT_ADDRESS}" in r["problems"]
    assert r["accepted_etag_drift"] == [bpc.RUNNER_GRANT]


def test_audit_drift_helper_layers_directly():
    assert bpc.etag_drift_problems(_audit_drift(), bpc.AUDIT_ADDRESS) == []
    d = _audit_drift()
    d["change"]["after"]["service"] = "allServices"
    assert (f"{bpc.AUDIT_ADDRESS}: drift changes more than etag: ['service']"
            in bpc.etag_drift_problems(d, bpc.AUDIT_ADDRESS))
    d = _audit_drift()
    for side in ("before", "after"):
        d["change"][side]["audit_log_config"][0]["exempted_members"] = ["user:x@e.invalid"]
    assert bpc.etag_drift_problems(d, bpc.AUDIT_ADDRESS) == [
        f"{bpc.AUDIT_ADDRESS}: drift values are not the approved iam.googleapis.com audit config"]
    assert bpc.etag_drift_problems(_etag_drift(), bpc.AUDIT_ADDRESS) == [
        f"drift detected outside Terraform: {bpc.RUNNER_GRANT}"]
    assert not bpc._has_unknown({"a": [{"b": []}, {"c": False}]})
    assert bpc._has_unknown({"a": [{"b": [False, True]}]})


def test_both_sides_are_checked_against_approved_values():
    for build, addr, key, bad in ((_audit_drift, bpc.AUDIT_ADDRESS, "service", "allServices"),
                                  (_etag_drift, bpc.RUNNER_GRANT, "member",
                                   "user:x@example.invalid")):
        d = build()
        d["change"]["before"][key] = bad  # only the stored side is wrong
        problems = bpc.etag_drift_problems(d, addr)
        assert f"{addr}: drift changes more than etag: ['{key}']" in problems
        assert any(p.startswith(f"{addr}: drift values are not") for p in problems)
