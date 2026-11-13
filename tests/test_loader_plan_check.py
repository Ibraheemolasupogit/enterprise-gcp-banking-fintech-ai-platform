"""M7C1: offline checks for the loader identity, access-window and revocation plans.

Synthetic ``terraform show -json`` documents only; no Terraform, gcloud or network access.
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
BOOTSTRAP = ROOT / "infrastructure" / "terraform" / "bootstrap"
_spec = importlib.util.spec_from_file_location("bootstrap_plan_check_m7c1", SCRIPT)
bpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bpc)

P = bpc.PROJECT
PLANNED = "2026-11-20T09:00:00Z"
EXPIRY = "2026-11-20T13:00:00Z"  # 4 h window
OPERATOR = "user:named-operator@example.invalid"
BUCKET = "approved-pilot-bucket-x1"
PACKET = "m7p-a89332976c69d583137f9323"
PREFIX = f"m7/ingestion/{PACKET}/"
LOADER = bpc.LOADER_MEMBER
RUNNER = f"serviceAccount:{bpc.RUNNER_EMAIL}"
PROVISIONER = f"serviceAccount:{bpc.PROVISIONER_EMAIL}"
R = bpc.ROLE_PREFIX


def _rc(address, kind, name, values, actions, index=None):
    before = None if actions == ["create"] else values
    after = None if actions == ["delete"] else values
    rc = {"address": address, "mode": "managed", "type": kind, "name": name,
          "change": {"actions": actions, "before": before, "after": after,
                     "after_unknown": {}}}
    if index is not None:
        rc["index"] = index
    return rc


def existing() -> list[dict]:
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
                   "runner_jobs", {"project": P, "condition": [], "role": f"{R}jobs",
                                   "member": RUNNER}, ["no-op"]))
    out.append(_rc(bpc.AUDIT_ADDRESS, "google_project_iam_audit_config", "iamcredentials",
                   {"project": P, "service": "iam.googleapis.com",
                    "audit_log_config": [{"log_type": t, "exempted_members": []}
                                         for t in ("ADMIN_READ", "DATA_READ")]},
                   ["no-op"], index=0))
    return out


def identity(actions) -> list[dict]:
    roles = bpc.loader_roles()
    out = [_rc(bpc.LOADER_ACCOUNT, "google_service_account", "loader",
               {"project": P, "account_id": "bankpilot-dev-loader"}, actions, index=0)]
    for key, spec in roles.items():
        out.append(_rc(f'google_project_iam_custom_role.loader["{key}"]',
                       "google_project_iam_custom_role", "loader",
                       {"project": P, "role_id": f"bankpilot_dev_{key}",
                        "permissions": sorted(spec["permissions"]), "stage": "GA"},
                       actions, index=key))
    return out


def grants(actions, expiry=EXPIRY, bucket=BUCKET, prefix=PREFIX, operator=OPERATOR,
           with_ids=False) -> list[dict]:
    cond = [bpc.loader_expiry_condition(expiry)]
    objects = [bpc.loader_object_condition(expiry, bucket, prefix)]
    ids = {"id": "x", "etag": "BwX1"} if with_ids else {}
    out = [
        _rc(bpc.LOADER_JOBS_GRANT, "google_project_iam_member", "loader_jobs",
            {"project": P, "role": f"{R}loader_jobs", "member": LOADER, "condition": cond,
             **ids}, actions, index=0),
        _rc(bpc.LOADER_OBJECTS_GRANT, "google_project_iam_member", "loader_objects",
            {"project": P, "role": f"{R}loader_objects", "member": LOADER,
             "condition": objects, **ids}, actions, index=0),
        _rc(bpc.LOADER_IMPERSONATION, "google_service_account_iam_member",
            "operator_loader_impersonation",
            {"service_account_id": f"projects/{P}/serviceAccounts/{bpc.LOADER_EMAIL}",
             "role": f"{R}impersonate", "member": operator, "condition": cond}, actions,
            index=0),
    ]
    for table in bpc.LOADER_TABLES:
        out.append(_rc(f'google_bigquery_table_iam_member.loader["{table}"]',
                       "google_bigquery_table_iam_member", "loader",
                       {"project": P, "dataset_id": "bankpilot_dev", "table_id": table,
                        "role": f"{R}loader_tables", "member": LOADER, "condition": [],
                        **ids}, actions, index=table))
    return out


def variables(mode) -> dict:
    v = {k: {"value": val} for k, val in bpc.EXPECTED_VARIABLES.items()}
    v.update(access_expiry={"value": None}, operator_member={"value": OPERATOR},
             loader_identity_enabled={"value": True},
             **{k: {"value": val} for k, val in bpc.LOADER_OFF.items()})
    if mode == "loader-access":
        v.update(loader_access_enabled={"value": True}, loader_access_expiry={"value": EXPIRY},
                 loader_bucket_name={"value": BUCKET}, loader_object_prefix={"value": PREFIX})
    return v


def plan(mode) -> dict:
    changes = {"loader-identity": [*existing(), *identity(["create"])],
               "loader-access": [*existing(), *identity(["no-op"]), *grants(["create"])],
               "loader-revoke": [*existing(), *identity(["no-op"]),
                                 *grants(["delete"], with_ids=True)]}[mode]
    return {"format_version": "1.2", "timestamp": PLANNED, "errored": False,
            "variables": variables(mode), "resource_changes": changes}


def check(p, mode, packet_id=PACKET, bucket=BUCKET):
    return bpc.check_plan(p, mode, packet_id=packet_id, bucket=bucket)


def by(p, addr):
    return next(rc for rc in p["resource_changes"] if rc["address"] == addr)


def after(p, addr):
    return by(p, addr)["change"]["after"]


def before(p, addr):
    return by(p, addr)["change"]["before"]


TABLE = 'google_bigquery_table_iam_member.loader["customers"]'


# ---------------------------------------------------------------- baseline

def test_disabled_bootstrap_inventory_is_still_11_and_identity_adds_4_grants_6():
    assert len(bpc.durable_addresses()) == 10
    assert len(bpc.durable_addresses() | {bpc.AUDIT_ADDRESS}) == 11
    assert len(bpc.loader_identity_addresses()) == 4
    assert len(bpc.LOADER_GRANTS) == 6
    assert not bpc.loader_identity_addresses() & bpc.durable_addresses()
    assert not bpc.LOADER_GRANTS & bpc.ACCESS_ADDRESSES


@pytest.mark.parametrize("mode,creates,unchanged,deletes", [
    ("loader-identity", 4, 11, 0), ("loader-access", 6, 15, 0), ("loader-revoke", 0, 15, 6)])
def test_valid_loader_plans_pass(mode, creates, unchanged, deletes):
    report = check(plan(mode), mode)
    assert report["ok"], report["problems"]
    assert (report["creates"], report["unchanged"], report["deletes"]) == (
        creates, unchanged, deletes)


def test_access_report_never_prints_the_bucket_or_operator():
    report = check(plan("loader-access"), "loader-access")
    assert report["packet_id"] == PACKET and report["bucket_approved"] is True
    assert report["loader_access_expiry"] == EXPIRY and report["window_hours"] == 4.0
    text = json.dumps(report)
    assert BUCKET not in text and "named-operator" not in text


def test_conditions_match_the_terraform_rendering():
    text = (BOOTSTRAP / "main.tf").read_text()
    assert 'title       = "loader-expires-${replace(coalesce(var.loader_access_expiry, ' \
        '"unset"), ":", "-")}"' in text
    assert 'description = "M7C1 time-bound loader access; revoke explicitly after use"' in text
    assert 'title       = "loader-objects-expires-${replace(coalesce(' \
        'var.loader_access_expiry, "unset"), ":", "-")}"' in text
    assert ('description = "M7C1 time-bound packet-prefix object access; revoke explicitly '
            'after use"') in text
    # Render the Terraform templates with the approved inputs and compare.
    expiry_tf = re.search(r'loader_expiry_condition = \{.*?expression  = "(.*?)"\n', text,
                          re.S).group(1)
    objects_tf = re.search(r'loader_object_condition = \{.*?expression  = "(.*?)"\n', text,
                           re.S).group(1)

    def render(template):
        out = template.replace("${local.loader_expiry}", EXPIRY)
        out = out.replace('${coalesce(var.loader_bucket_name, "unset")}', BUCKET)
        out = out.replace('${coalesce(var.loader_object_prefix, "unset/")}', PREFIX)
        return out.replace('\\"', '"')

    assert render(expiry_tf) == bpc.loader_expiry_condition(EXPIRY)["expression"]
    assert render(objects_tf) == bpc.loader_object_condition(EXPIRY, BUCKET, PREFIX)["expression"]
    assert bpc.loader_object_condition(EXPIRY, BUCKET, PREFIX)["expression"] == (
        'request.time < timestamp("2026-11-20T13:00:00Z") && resource.type == '
        '"storage.googleapis.com/Object" && resource.name.startsWith('
        f'"projects/_/buckets/{BUCKET}/objects/m7/ingestion/{PACKET}/")')


def test_loader_roles_in_checker_match_repository_json():
    roles = bpc.expected_roles()
    for key, spec in bpc.loader_roles().items():
        assert roles[f"bankpilot_dev_{key}"] == set(spec["permissions"])


# ---------------------------------------------------------------- loader-identity

def _role(p, key):
    return after(p, f'google_project_iam_custom_role.loader["{key}"]')


IDENTITY_BAD = {
    "jobs role widened": lambda p: _role(p, "loader_jobs")["permissions"].append(
        "bigquery.jobs.update"),
    "objects role gains delete": lambda p: _role(p, "loader_objects")["permissions"].append(
        "storage.objects.delete"),
    "tables role gains update": lambda p: _role(p, "loader_tables")["permissions"].append(
        "bigquery.tables.update"),
    "tables role gains setIamPolicy": lambda p: _role(p, "loader_tables")["permissions"].append(
        "bigquery.tables.setIamPolicy"),
    "role renamed": lambda p: _role(p, "loader_jobs").update(role_id="bankpilot_dev_jobs2"),
    "account renamed": lambda p: after(p, bpc.LOADER_ACCOUNT).update(account_id="other-sa"),
    "account in another project": lambda p: after(p, bpc.LOADER_ACCOUNT).update(project="x"),
    "identity disabled": lambda p: p["variables"]["loader_identity_enabled"].update(value=False),
    "access enabled too": lambda p: p["variables"]["loader_access_enabled"].update(value=True),
    "expiry set": lambda p: p["variables"]["loader_access_expiry"].update(value=EXPIRY),
    "bucket set": lambda p: p["variables"]["loader_bucket_name"].update(value=BUCKET),
    "provisioner access on": lambda p: p["variables"]["provisioner_access_enabled"].update(
        value=True),
    "grants bundled in": lambda p: p["resource_changes"].extend(grants(["create"])),
    "only three creates": lambda p: p["resource_changes"].pop(),
    "key resource": lambda p: p["resource_changes"].append(_rc(
        "google_service_account_key.loader[0]", "google_service_account_key", "loader",
        {"service_account_id": bpc.LOADER_EMAIL}, ["create"], index=0)),
    "existing runner changes": lambda p: by(p, "google_project_iam_member.runner_jobs")[
        "change"].update(actions=["update"]),
    "errored": lambda p: p.update(errored=True),
}


IDENTITY_BAD["removed-grant drift outside revoke"] = lambda p: p.update(resource_drift=[
    _rc(bpc.LOADER_IMPERSONATION, "x", "y", {}, ["delete"])])


@pytest.mark.parametrize("name", IDENTITY_BAD)
def test_adversarial_identity_plans_are_rejected(name):
    p = plan("loader-identity")
    IDENTITY_BAD[name](p)
    assert not check(p, "loader-identity")["ok"], name


# ---------------------------------------------------------------- loader-access

def _cond(p, addr):
    return after(p, addr)["condition"][0]


ACCESS_BAD = {
    # wrong identities
    "jobs to runner": lambda p: after(p, bpc.LOADER_JOBS_GRANT).update(member=RUNNER),
    "objects to provisioner": lambda p: after(p, bpc.LOADER_OBJECTS_GRANT).update(
        member=PROVISIONER),
    "table to a user": lambda p: after(p, TABLE).update(member="user:x@example.invalid"),
    "table to allUsers": lambda p: after(p, TABLE).update(member="allUsers"),
    "impersonation on provisioner": lambda p: after(p, bpc.LOADER_IMPERSONATION).update(
        service_account_id=f"projects/{P}/serviceAccounts/{bpc.PROVISIONER_EMAIL}"),
    "impersonation on runner": lambda p: after(p, bpc.LOADER_IMPERSONATION).update(
        service_account_id=f"projects/{P}/serviceAccounts/{bpc.RUNNER_EMAIL}"),
    "impersonation for a group": lambda p: after(p, bpc.LOADER_IMPERSONATION).update(
        member="group:ops@example.invalid"),
    "impersonation for another user": lambda p: after(p, bpc.LOADER_IMPERSONATION).update(
        member="user:someone-else@example.invalid"),
    "impersonation for the loader itself": lambda p: after(p, bpc.LOADER_IMPERSONATION).update(
        member=LOADER),
    # role widening / privilege escalation
    "jobs grant gets provision role": lambda p: after(p, bpc.LOADER_JOBS_GRANT).update(
        role=f"{R}provision"),
    "jobs grant predefined admin": lambda p: after(p, bpc.LOADER_JOBS_GRANT).update(
        role="roles/bigquery.admin"),
    "objects grant gets runtime objects": lambda p: after(p, bpc.LOADER_OBJECTS_GRANT).update(
        role=f"{R}objects"),
    "objects grant storage admin": lambda p: after(p, bpc.LOADER_OBJECTS_GRANT).update(
        role="roles/storage.admin"),
    "table grant dataEditor": lambda p: after(p, TABLE).update(role="roles/bigquery.dataEditor"),
    "table grant runtime tables": lambda p: after(p, TABLE).update(role=f"{R}tables"),
    "impersonation token creator": lambda p: after(p, bpc.LOADER_IMPERSONATION).update(
        role="roles/iam.serviceAccountTokenCreator"),
    "impersonation gets jobs role": lambda p: after(p, bpc.LOADER_IMPERSONATION).update(
        role=f"{R}loader_jobs"),
    "table grant gets loader jobs role": lambda p: after(p, TABLE).update(role=f"{R}loader_jobs"),
    "jobs grant gets loader tables role": lambda p: after(p, bpc.LOADER_JOBS_GRANT).update(
        role=f"{R}loader_tables"),
    "objects grant gets loader jobs role": lambda p: after(p, bpc.LOADER_OBJECTS_GRANT).update(
        role=f"{R}loader_jobs"),
    # scope
    "fourth table (mart)": lambda p: p["resource_changes"].append(_rc(
        'google_bigquery_table_iam_member.loader["transaction_daily"]',
        "google_bigquery_table_iam_member", "loader",
        {**after(p, TABLE), "table_id": "transaction_daily"}, ["create"],
        index="transaction_daily")),
    "table key and id disagree": lambda p: after(p, TABLE).update(table_id="accounts"),
    "other dataset": lambda p: after(p, TABLE).update(dataset_id="other_dataset"),
    "other project": lambda p: after(p, bpc.LOADER_JOBS_GRANT).update(project="other-project"),
    "one table grant missing": lambda p: p["resource_changes"].remove(by(p, TABLE)),
    # expiry and conditions
    "jobs grant without condition": lambda p: after(p, bpc.LOADER_JOBS_GRANT).update(
        condition=[]),
    # M7D correction: a table grant must carry no condition at all.
    "table grant with the approved expiry": lambda p: after(p, TABLE).update(
        condition=[bpc.loader_expiry_condition(EXPIRY)]),
    "table grant with an object condition": lambda p: after(p, TABLE).update(
        condition=[bpc.loader_object_condition(EXPIRY, BUCKET, PREFIX)]),
    "table grant with an empty condition object": lambda p: after(p, TABLE).update(
        condition=[{}]),
    "table grant condition key missing": lambda p: after(p, TABLE).pop("condition"),
    "impersonation without condition": lambda p: after(p, bpc.LOADER_IMPERSONATION).update(
        condition=[]),
    "two conditions": lambda p: after(p, bpc.LOADER_JOBS_GRANT)["condition"].append(
        bpc.loader_expiry_condition(EXPIRY)),
    "condition widened with or-true": lambda p: _cond(p, bpc.LOADER_JOBS_GRANT).update(
        expression=_cond(p, bpc.LOADER_JOBS_GRANT)["expression"] + " || true"),
    "later expiry on one grant": lambda p: after(p, bpc.LOADER_IMPERSONATION).update(
        condition=[bpc.loader_expiry_condition("2026-11-20T16:00:00Z")]),
    "M6 provisioner condition reused": lambda p: after(p, bpc.LOADER_JOBS_GRANT).update(
        condition=[bpc.expiry_condition(EXPIRY)]),
    "objects without prefix": lambda p: _cond(p, bpc.LOADER_OBJECTS_GRANT).update(
        expression=f'request.time < timestamp("{EXPIRY}")'),
    "objects whole bucket": lambda p: after(p, bpc.LOADER_OBJECTS_GRANT).update(
        condition=[bpc.loader_object_condition(EXPIRY, BUCKET, "")]),
    "objects other packet": lambda p: after(p, bpc.LOADER_OBJECTS_GRANT).update(
        condition=[bpc.loader_object_condition(EXPIRY, BUCKET,
                                               "m7/ingestion/m7p-" + "0" * 24 + "/")]),
    "objects other bucket": lambda p: after(p, bpc.LOADER_OBJECTS_GRANT).update(
        condition=[bpc.loader_object_condition(EXPIRY, "other-bucket-123", PREFIX)]),
    "objects jobs-style condition": lambda p: after(p, bpc.LOADER_OBJECTS_GRANT).update(
        condition=[bpc.loader_expiry_condition(EXPIRY)]),
    "expiry over 8 h": lambda p: _set_expiry(p, "2026-11-20T17:00:01Z"),
    "expiry before plan": lambda p: _set_expiry(p, "2026-11-20T08:59:59Z"),
    "expiry not UTC Z": lambda p: _set_expiry(p, "2026-11-20T13:00:00+00:00"),
    "expiry missing": lambda p: _set_expiry(p, None),
    "no plan timestamp": lambda p: p.pop("timestamp"),
    # variables
    "prefix variable other packet": lambda p: p["variables"]["loader_object_prefix"].update(
        value="m7/ingestion/m7p-" + "1" * 24 + "/"),
    "prefix variable broad": lambda p: p["variables"]["loader_object_prefix"].update(
        value="m7/"),
    "bucket variable differs": lambda p: p["variables"]["loader_bucket_name"].update(
        value="other-bucket-123"),
    "identity disabled": lambda p: p["variables"]["loader_identity_enabled"].update(value=False),
    "access variable off": lambda p: p["variables"]["loader_access_enabled"].update(value=False),
    "access variable missing": lambda p: p["variables"].pop("loader_access_enabled"),
    "removed-grant drift outside revoke": lambda p: p.update(resource_drift=[
        _rc(TABLE, "x", "y", {}, ["delete"])]),
    "provisioner access overlaps": lambda p: p["variables"]["provisioner_access_enabled"].update(
        value=True),
    "provisioner expiry set": lambda p: p["variables"]["access_expiry"].update(value=EXPIRY),
    "operator variable is a group": lambda p: p["variables"]["operator_member"].update(
        value="group:ops@example.invalid"),
    "audit logging off": lambda p: p["variables"]["token_audit_logging_enabled"].update(
        value=False),
    # unexpected resources and actions
    "provisioner grant bundled": lambda p: p["resource_changes"].append(_rc(
        bpc.PROVISIONER_GRANT, "google_project_iam_member", "provisioner",
        {"project": P, "role": f"{R}provision", "member": PROVISIONER,
         "condition": [bpc.expiry_condition(EXPIRY)]}, ["create"], index=0)),
    "dataset policy touched": lambda p: p["resource_changes"].append(_rc(
        "google_bigquery_dataset_iam_member.loader", "google_bigquery_dataset_iam_member",
        "loader", {"dataset_id": "bankpilot_dev", "role": "roles/bigquery.dataEditor",
                   "member": LOADER}, ["create"])),
    "bucket member": lambda p: p["resource_changes"].append(_rc(
        "google_storage_bucket_iam_member.loader", "google_storage_bucket_iam_member", "loader",
        {"bucket": BUCKET, "role": "roles/storage.objectAdmin", "member": LOADER}, ["create"])),
    "project binding text": lambda p: p.update(configuration={"x": "google_project_iam_binding"}),
    "loader key": lambda p: p["resource_changes"].append(_rc(
        "google_service_account_key.loader[0]", "google_service_account_key", "loader", {},
        ["create"], index=0)),
    "grant as update": lambda p: by(p, bpc.LOADER_JOBS_GRANT)["change"].update(
        actions=["update"]),
    "identity recreated": lambda p: by(p, bpc.LOADER_ACCOUNT)["change"].update(
        actions=["delete", "create"]),
    "loader role changed": lambda p: by(
        p, 'google_project_iam_custom_role.loader["loader_tables"]')["change"].update(
        actions=["update"]),
    "data source": lambda p: p["resource_changes"].append({
        "address": "data.google_iam_policy.x", "mode": "data", "type": "google_iam_policy",
        "name": "x", "change": {"actions": ["read"], "before": None, "after": {}}}),
    # drift
    "non-etag drift on runner": lambda p: p.update(resource_drift=[_runner_drift(
        change_role=True)]),
    "drift on loader grant in access": lambda p: p.update(resource_drift=[_loader_drift()]),
    "drift on loader identity": lambda p: p.update(resource_drift=[{
        **by(p, bpc.LOADER_ACCOUNT), "change": {"actions": ["update"], "before": {},
                                                 "after": {}}}]),
}


def _set_expiry(p, expiry):
    p["variables"]["loader_access_expiry"]["value"] = expiry
    if expiry is not None:
        for rc in p["resource_changes"]:
            if rc["address"] in bpc.LOADER_EXPIRING_GRANTS:
                rc["change"]["after"]["condition"] = [
                    bpc.loader_object_condition(expiry, BUCKET, PREFIX)
                    if rc["address"] == bpc.LOADER_OBJECTS_GRANT
                    else bpc.loader_expiry_condition(expiry)]


def _runner_drift(change_role=False) -> dict:
    side = {"project": P, "role": f"{R}jobs", "member": RUNNER, "condition": [], "id": "i",
            "etag": "BwOld"}
    new = {**side, "etag": "BwNew"}
    if change_role:
        new["role"] = f"{R}provision"
    return {"address": bpc.RUNNER_GRANT, "mode": "managed", "type": "google_project_iam_member",
            "name": "runner_jobs",
            "change": {"actions": ["update"], "before": side, "after": new,
                       "after_unknown": {}}}


def _loader_drift(address=bpc.LOADER_JOBS_GRANT, **changes) -> dict:
    name = "loader_jobs" if address == bpc.LOADER_JOBS_GRANT else "loader_objects"
    cond = ([bpc.loader_expiry_condition(EXPIRY)] if name == "loader_jobs"
            else [bpc.loader_object_condition(EXPIRY, BUCKET, PREFIX)])
    side = {"project": P, "role": f"{R}{name}", "member": LOADER, "condition": cond, "id": "i",
            "etag": "BwOld"}
    new = {**side, "etag": "BwNew", **changes}
    return {"address": address, "mode": "managed", "type": "google_project_iam_member",
            "name": name, "index": 0,
            "change": {"actions": ["update"], "before": side, "after": new,
                       "after_unknown": {}}}


@pytest.mark.parametrize("name", ACCESS_BAD)
def test_adversarial_access_plans_are_rejected(name):
    p = plan("loader-access")
    ACCESS_BAD[name](p)
    assert not check(p, "loader-access")["ok"], name


def test_access_accepts_strict_etag_drift_on_runner_and_audit_only():
    p = plan("loader-access")
    p["resource_drift"] = [_runner_drift()]
    report = check(p, "loader-access")
    assert report["ok"], report["problems"]
    assert report["accepted_etag_drift"] == [bpc.RUNNER_GRANT]


@pytest.mark.parametrize("packet_id,bucket", [
    (None, BUCKET), (PACKET, None), ("m7p-short", BUCKET), (PACKET, "Bad_Bucket"),
    ("m7p-" + "0" * 24, BUCKET), (PACKET, "other-bucket-123")])
def test_access_requires_the_approved_packet_and_bucket(packet_id, bucket):
    report = check(plan("loader-access"), "loader-access", packet_id=packet_id, bucket=bucket)
    assert not report["ok"]
    assert report["bucket_approved"] is (bucket == BUCKET)


def test_eight_hours_exactly_is_allowed():
    p = plan("loader-access")
    _set_expiry(p, "2026-11-20T17:00:00Z")
    assert check(p, "loader-access")["ok"]


# ---------------------------------------------------------------- loader-revoke

REVOKE_BAD = {
    "one table grant left": lambda p: by(p, TABLE)["change"].update(
        actions=["no-op"], after=before(p, TABLE)),
    "impersonation left": lambda p: p["resource_changes"].remove(by(p, bpc.LOADER_IMPERSONATION)),
    "objects grant left": lambda p: by(p, bpc.LOADER_OBJECTS_GRANT)["change"].update(
        actions=["no-op"], after=before(p, bpc.LOADER_OBJECTS_GRANT)),
    "access still enabled": lambda p: p["variables"]["loader_access_enabled"].update(value=True),
    "expiry left set": lambda p: p["variables"]["loader_access_expiry"].update(value=EXPIRY),
    "bucket left set": lambda p: p["variables"]["loader_bucket_name"].update(value=BUCKET),
    "prefix left set": lambda p: p["variables"]["loader_object_prefix"].update(value=PREFIX),
    "identity destroyed": lambda p: by(p, bpc.LOADER_ACCOUNT)["change"].update(
        actions=["delete"], after=None),
    "loader role destroyed": lambda p: by(
        p, 'google_project_iam_custom_role.loader["loader_jobs"]')["change"].update(
        actions=["delete"], after=None),
    "identity disabled": lambda p: p["variables"]["loader_identity_enabled"].update(value=False),
    "provisioner grant recreated": lambda p: p["resource_changes"].append(_rc(
        bpc.PROVISIONER_GRANT, "google_project_iam_member", "provisioner",
        {"project": P, "role": f"{R}provision", "member": PROVISIONER,
         "condition": [bpc.expiry_condition(EXPIRY)]}, ["create"], index=0)),
    "replacement grant created": lambda p: p["resource_changes"].append(_rc(
        "google_project_iam_member.loader_jobs[1]", "google_project_iam_member", "loader_jobs",
        {"project": P, "role": f"{R}loader_jobs", "member": LOADER, "condition": []},
        ["create"], index=1)),
    "deleted grant is a different role": lambda p: before(p, bpc.LOADER_JOBS_GRANT).update(
        role=f"{R}provision"),
    "deleted grant had no condition": lambda p: before(p, bpc.LOADER_JOBS_GRANT).update(
        condition=[]),
    "deleted impersonation had no condition": lambda p: before(
        p, bpc.LOADER_IMPERSONATION).update(condition=[]),
    "deleted table grant had a condition": lambda p: before(p, TABLE).update(
        condition=[bpc.loader_expiry_condition(EXPIRY)]),
    "deleted table grant on a mart": lambda p: before(p, TABLE).update(
        table_id="transaction_daily"),
    "drift removed a condition": lambda p: p.update(resource_drift=[_loader_drift(
        condition=[])]),
    "drift widened the role": lambda p: p.update(resource_drift=[_loader_drift(
        role="roles/bigquery.admin")]),
    "drift on a table grant": lambda p: p.update(resource_drift=[{
        **by(p, TABLE), "change": {"actions": ["update"], "before": before(p, TABLE),
                                   "after": {**before(p, TABLE), "etag": "z"}}}]),
    "duplicate drift": lambda p: p.update(resource_drift=[_loader_drift(), _loader_drift()]),
    "provisioner drift tolerated?": lambda p: p.update(resource_drift=[{
        **_loader_drift(), "address": bpc.PROVISIONER_GRANT, "name": "provisioner"}]),
}


@pytest.mark.parametrize("name", REVOKE_BAD)
def test_adversarial_revoke_plans_are_rejected(name):
    p = plan("loader-revoke")
    REVOKE_BAD[name](p)
    assert not check(p, "loader-revoke")["ok"], name


@pytest.mark.parametrize("drift", [
    [bpc.LOADER_JOBS_GRANT], [bpc.LOADER_OBJECTS_GRANT],
    [bpc.LOADER_JOBS_GRANT, bpc.LOADER_OBJECTS_GRANT]])
def test_revoke_accepts_strict_etag_drift_on_loader_project_grants(drift):
    p = plan("loader-revoke")
    p["resource_drift"] = [_loader_drift(a) for a in drift] + [_runner_drift()]
    report = check(p, "loader-revoke")
    assert report["ok"], report["problems"]
    assert report["accepted_etag_drift"] == sorted([*drift, bpc.RUNNER_GRANT])


def test_revoke_after_emergency_manual_removal():
    p = plan("loader-revoke")
    removed = [TABLE, bpc.LOADER_IMPERSONATION]
    p["resource_changes"] = [rc for rc in p["resource_changes"] if rc["address"] not in removed]
    p["resource_drift"] = [{**_rc(a, "x", "y", {}, ["delete"])} for a in removed]
    report = check(p, "loader-revoke")
    assert report["ok"], report["problems"]
    assert report["already_removed"] == sorted(removed)


def test_revoke_cannot_claim_removal_and_also_skip_the_rest():
    p = plan("loader-revoke")
    p["resource_changes"] = [rc for rc in p["resource_changes"]
                             if rc["address"] not in bpc.LOADER_GRANTS]
    p["resource_drift"] = [_rc(TABLE, "x", "y", {}, ["delete"])]
    problems = check(p, "loader-revoke")["problems"]
    assert sum("expected delete missing" in x for x in problems) == 5


# ---------------------------------------------------------------- mode isolation

@pytest.mark.parametrize("source", ["loader-identity", "loader-access", "loader-revoke"])
def test_loader_plans_pass_only_their_own_mode(source):
    for mode in bpc.BOOTSTRAP_MODES:
        ok = check(plan(source), mode)["ok"]
        assert ok is (mode == source), (source, mode)


def test_m6_drift_table_is_unchanged_and_loader_table_is_separate():
    assert bpc.ETAG_DRIFT_BY_MODE == {
        "access": {bpc.RUNNER_GRANT, bpc.AUDIT_ADDRESS},
        "revoke": {bpc.RUNNER_GRANT, bpc.AUDIT_ADDRESS}}
    assert set(bpc.LOADER_ETAG_DRIFT_BY_MODE) == set(bpc.LOADER_MODES)
    assert bpc.LOADER_ETAG_DRIFT_BY_MODE["loader-access"] == {bpc.RUNNER_GRANT,
                                                              bpc.AUDIT_ADDRESS}
    for mode in ("initial", "recovery"):
        p = plan("loader-revoke")
        p["resource_drift"] = [_loader_drift()]
        assert "drift detected outside Terraform: " + bpc.LOADER_JOBS_GRANT in \
            check(p, mode)["problems"]


def _m6(kind):
    """An M6 access or revoke plan after M7D: loader identity present and unchanged."""
    cond = [bpc.expiry_condition(EXPIRY)]
    m6 = [_rc(bpc.PROVISIONER_GRANT, "google_project_iam_member", "provisioner",
              {"project": P, "role": f"{R}provision", "member": PROVISIONER, "condition": cond},
              [kind], index=0),
          _rc(bpc.IMPERSONATION_GRANT, "google_service_account_iam_member",
              "operator_impersonation",
              {"service_account_id": f"projects/{P}/serviceAccounts/{bpc.PROVISIONER_EMAIL}",
               "role": f"{R}impersonate", "member": OPERATOR, "condition": cond}, [kind],
              index=0)]
    v = variables("loader-revoke")
    if kind == "create":
        v["provisioner_access_enabled"] = {"value": True}
        v["access_expiry"] = {"value": EXPIRY}
    return {"format_version": "1.2", "timestamp": PLANNED, "errored": False, "variables": v,
            "resource_changes": [*existing(), *identity(["no-op"]), *m6]}


@pytest.mark.parametrize("kind,mode", [("create", "access"), ("delete", "revoke")])
def test_m6_modes_still_work_after_the_loader_identity_exists(kind, mode):
    report = bpc.check_plan(_m6(kind), mode)
    assert report["ok"], report["problems"]


M6_WITH_LOADER_BAD = {
    "loader grant in M6 access": lambda p: p["resource_changes"].append(
        grants(["create"])[0]),
    "loader access variable on": lambda p: p["variables"]["loader_access_enabled"].update(
        value=True),
    "loader expiry set": lambda p: p["variables"]["loader_access_expiry"].update(value=EXPIRY),
    "loader bucket set": lambda p: p["variables"]["loader_bucket_name"].update(value=BUCKET),
    "loader identity created in M6": lambda p: by(p, bpc.LOADER_ACCOUNT)["change"].update(
        actions=["create"]),
    "identity on but resources missing": lambda p: p.update(resource_changes=[
        rc for rc in p["resource_changes"] if rc["address"] != bpc.LOADER_ACCOUNT]),
    "identity off but resources present": lambda p: p["variables"][
        "loader_identity_enabled"].update(value=False),
    "loader role widened": lambda p: _role(p, "loader_objects")["permissions"].append(
        "storage.objects.delete"),
}


@pytest.mark.parametrize("name", M6_WITH_LOADER_BAD)
@pytest.mark.parametrize("kind,mode", [("create", "access"), ("delete", "revoke")])
def test_m6_modes_reject_loader_changes(name, kind, mode):
    p = _m6(kind)
    M6_WITH_LOADER_BAD[name](p)
    assert not bpc.check_plan(p, mode)["ok"], (name, mode)


@pytest.mark.parametrize("mode", ["initial", "recovery"])
def test_initial_and_recovery_refuse_the_loader_identity_variable(mode):
    p = {"variables": {"loader_identity_enabled": {"value": True}}, "resource_changes": []}
    assert "variable loader_identity_enabled is not allowed in this mode" in \
        bpc.check_plan(p, mode)["problems"]


def test_pre_m7c1_plans_without_loader_variables_are_still_accepted_by_m6_modes():
    p = _m6("create")
    p["resource_changes"] = [rc for rc in p["resource_changes"]
                             if rc["address"] not in bpc.loader_identity_addresses()]
    for name in ("loader_identity_enabled", *bpc.LOADER_OFF):
        p["variables"].pop(name)
    assert bpc.check_plan(p, "access")["ok"]


# ---------------------------------------------------------------- CLI

def _cli(p, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], input=json.dumps(p),
                          capture_output=True, text=True, check=False)


def test_cli_loader_modes_and_secret_handling():
    result = _cli(plan("loader-access"), "--mode", "loader-access", f"--packet-id={PACKET}",
                  f"--bucket={BUCKET}")
    assert result.returncode == 0, result.stdout
    assert BUCKET not in result.stdout and "named-operator" not in result.stdout
    assert json.loads(result.stdout)["packet_id"] == PACKET
    missing = _cli(plan("loader-access"), "--mode", "loader-access", f"--packet-id={PACKET}")
    assert missing.returncode == 1
    for mode in ("loader-identity", "loader-revoke"):
        assert _cli(plan(mode), "--mode", mode).returncode == 0


def test_cli_wrong_bucket_is_not_echoed():
    p = plan("loader-access")
    result = _cli(p, "--mode", "loader-access", f"--packet-id={PACKET}",
                  "--bucket=other-bucket-123")
    assert result.returncode == 1
    assert "other-bucket-123" not in result.stdout and BUCKET not in result.stdout


def test_fixture_independence():
    first, second = plan("loader-access"), plan("loader-access")
    ACCESS_BAD["jobs to runner"](first)
    assert check(second, "loader-access")["ok"]
    assert copy.deepcopy(second) == plan("loader-access")


# ---------------------------------------------------------------- M7D table IAM correction

def _tf_block(text, kind, name):
    match = re.search(rf'resource "{kind}" "{name}" \{{\n(.*?)\n\}}\n', text, re.S)
    assert match, (kind, name)
    return match.group(1)


def test_terraform_table_grants_are_unconditional_and_the_rest_still_expire():
    text = (BOOTSTRAP / "main.tf").read_text()
    table = _tf_block(text, "google_bigquery_table_iam_member", "loader")
    assert "condition" not in table and "dynamic" not in table
    assert "for_each   = local.loader_tables" in table
    assert 'role       = google_project_iam_custom_role.loader["loader_tables"].name' in table
    assert "member     = google_service_account.loader[0].member" in table
    assert "dataset_id = local.loader_dataset_id" in table
    for kind, name, cond in (
            ("google_project_iam_member", "loader_jobs", "loader_expiry_condition"),
            ("google_project_iam_member", "loader_objects", "loader_object_condition"),
            ("google_service_account_iam_member", "operator_loader_impersonation",
             "loader_expiry_condition")):
        body = _tf_block(text, kind, name)
        assert body.count("condition {") == 1, name
        for key in ("title", "description", "expression"):
            assert f"local.{cond}.{key}" in body, (name, key)
        assert "count" in body and "local.loader_access_count" in body
    # Still exactly three input tables, only while access is enabled; never the dataset.
    assert ('loader_tables         = var.loader_access_enabled ? toset(["accounts", '
            '"curated_transactions", "customers"]) : toset([])') in text
    assert "google_bigquery_dataset_iam" not in text


def test_loader_roles_json_is_unchanged_by_the_correction():
    """The preserved custom roles must not drift: a description edit would plan an update."""
    roles = bpc.loader_roles()
    assert roles["loader_tables"]["permissions"] == [
        "bigquery.tables.get", "bigquery.tables.getData", "bigquery.tables.updateData"]
    assert roles["loader_tables"]["description"].startswith("M7C1: load into and read back")


def test_access_plan_table_grants_carry_no_condition_and_others_exact_expiry():
    p = plan("loader-access")
    for addr in bpc.LOADER_TABLE_GRANTS:
        assert after(p, addr)["condition"] == []
    report = check(p, "loader-access")
    assert report["ok"], report["problems"]
    for addr in bpc.LOADER_EXPIRING_GRANTS:
        assert len(after(p, addr)["condition"]) == 1


@pytest.mark.parametrize("table", bpc.LOADER_TABLES)
def test_any_table_condition_is_reported_per_table(table):
    p = plan("loader-access")
    addr = f'google_bigquery_table_iam_member.loader["{table}"]'
    after(p, addr)["condition"] = [bpc.loader_expiry_condition(EXPIRY)]
    assert f"{addr}: table grant must be unconditional (no condition block)" in \
        check(p, "loader-access")["problems"]


def test_unconditional_is_only_accepted_on_table_grants():
    for addr in bpc.LOADER_EXPIRING_GRANTS:
        p = plan("loader-access")
        after(p, addr)["condition"] = []
        assert not check(p, "loader-access")["ok"], addr


# ---------------------------------------------------------------- loader-recovery (M7D)

def recovery_plan(deleted=(), removed=(), drift=()) -> dict:
    """Base tfvars plan after the partial loader-access apply: tables never created."""
    p = plan("loader-revoke")
    expiring = {rc["address"]: rc for rc in p["resource_changes"]
                if rc["address"] in bpc.LOADER_EXPIRING_GRANTS}
    p["resource_changes"] = [rc for rc in p["resource_changes"]
                             if rc["address"] not in bpc.LOADER_GRANTS]
    p["resource_changes"] += [expiring[a] for a in deleted]
    p["resource_drift"] = ([_rc(a, "x", "y", {}, ["delete"]) for a in removed]
                           + [_loader_drift(a) for a in drift])
    return p


EXPIRING = sorted(bpc.LOADER_EXPIRING_GRANTS)


@pytest.mark.parametrize("deleted,removed", [
    ((), ()),                                   # state already clean
    (EXPIRING, ()),                             # state still records all three; live present
    ((), EXPIRING),                             # revoked outside Terraform; state stale
    ([bpc.LOADER_IMPERSONATION], [bpc.LOADER_JOBS_GRANT, bpc.LOADER_OBJECTS_GRANT]),
    ([bpc.LOADER_JOBS_GRANT], ()),
])
def test_recovery_accepts_every_honest_partial_state(deleted, removed):
    report = check(recovery_plan(deleted, removed), "loader-recovery")
    assert report["ok"], report["problems"]
    assert (report["creates"], report["unchanged"]) == (0, 15)
    assert report["delete_addresses"] == sorted(deleted)
    assert report["already_removed"] == sorted(removed)
    assert report["absent_from_state"] == sorted(
        bpc.LOADER_EXPIRING_GRANTS - set(deleted) - set(removed))


def test_recovery_accepts_strict_etag_drift_on_loader_project_grants():
    p = recovery_plan([bpc.LOADER_JOBS_GRANT, bpc.LOADER_OBJECTS_GRANT],
                      drift=[bpc.LOADER_JOBS_GRANT])
    p["resource_drift"].append(_runner_drift())
    report = check(p, "loader-recovery")
    assert report["ok"], report["problems"]
    assert report["accepted_etag_drift"] == sorted([bpc.LOADER_JOBS_GRANT, bpc.RUNNER_GRANT])


def _table_rc(actions, **values):
    base = {"project": P, "dataset_id": "bankpilot_dev", "table_id": "customers",
            "role": f"{R}loader_tables", "member": LOADER, "condition": [], "id": "x",
            "etag": "BwX1"}
    return _rc(TABLE, "google_bigquery_table_iam_member", "loader", {**base, **values},
               actions, index="customers")


RECOVERY_BAD = {
    "table grant deleted": lambda p: p["resource_changes"].append(_table_rc(["delete"])),
    "table grant created": lambda p: p["resource_changes"].append(_table_rc(["create"])),
    "table grant in state unchanged": lambda p: p["resource_changes"].append(
        _table_rc(["no-op"])),
    "table grant already removed": lambda p: p["resource_drift"].append(
        _rc(TABLE, "x", "y", {}, ["delete"])),
    "conditional table grant deleted": lambda p: p["resource_changes"].append(
        _table_rc(["delete"], condition=[bpc.loader_expiry_condition(EXPIRY)])),
    "expiring grant created": lambda p: p["resource_changes"].append(
        grants(["create"])[0]),
    "expiring grant left unchanged": lambda p: p["resource_changes"].append(
        grants(["no-op"], with_ids=True)[0]),
    "removed and also deleted": lambda p: (
        p["resource_changes"].append(grants(["delete"], with_ids=True)[0]),
        p["resource_drift"].append(_rc(bpc.LOADER_JOBS_GRANT, "x", "y", {}, ["delete"]))),
    "duplicate removal": lambda p: p["resource_drift"].extend(
        [_rc(bpc.LOADER_JOBS_GRANT, "x", "y", {}, ["delete"])] * 2),
    "deleted grant without condition": lambda p: p["resource_changes"].append(_rc(
        bpc.LOADER_JOBS_GRANT, "google_project_iam_member", "loader_jobs",
        {"project": P, "role": f"{R}loader_jobs", "member": LOADER, "condition": [],
         "id": "x"}, ["delete"], index=0)),
    "deleted grant is a broader role": lambda p: p["resource_changes"].append(_rc(
        bpc.LOADER_JOBS_GRANT, "google_project_iam_member", "loader_jobs",
        {"project": P, "role": "roles/bigquery.admin", "member": LOADER,
         "condition": [bpc.loader_expiry_condition(EXPIRY)], "id": "x"}, ["delete"], index=0)),
    "provisioner grant deleted": lambda p: p["resource_changes"].append(_rc(
        bpc.PROVISIONER_GRANT, "google_project_iam_member", "provisioner",
        {"project": P, "role": f"{R}provision", "member": PROVISIONER,
         "condition": [bpc.expiry_condition(EXPIRY)]}, ["delete"], index=0)),
    "provisioner grant already removed": lambda p: p["resource_drift"].append(
        _rc(bpc.PROVISIONER_GRANT, "x", "y", {}, ["delete"])),
    "runner grant deleted": lambda p: by(p, bpc.RUNNER_GRANT)["change"].update(
        actions=["delete"], after=None),
    "identity destroyed": lambda p: by(p, bpc.LOADER_ACCOUNT)["change"].update(
        actions=["delete"], after=None),
    "loader role updated": lambda p: by(
        p, 'google_project_iam_custom_role.loader["loader_tables"]')["change"].update(
        actions=["update"]),
    "access still enabled": lambda p: p["variables"]["loader_access_enabled"].update(value=True),
    "expiry left set": lambda p: p["variables"]["loader_access_expiry"].update(value=EXPIRY),
    "bucket left set": lambda p: p["variables"]["loader_bucket_name"].update(value=BUCKET),
    "prefix left set": lambda p: p["variables"]["loader_object_prefix"].update(value=PREFIX),
    "identity disabled": lambda p: p["variables"]["loader_identity_enabled"].update(
        value=False),
    "provisioner access on": lambda p: p["variables"]["provisioner_access_enabled"].update(
        value=True),
    "drift widened a loader grant": lambda p: p["resource_drift"].append(_loader_drift(
        role="roles/bigquery.admin")),
    "drift on impersonation": lambda p: p["resource_drift"].append({
        **_loader_drift(), "address": bpc.LOADER_IMPERSONATION,
        "type": "google_service_account_iam_member", "name": "operator_loader_impersonation"}),
    "drift on loader identity": lambda p: p["resource_drift"].append({
        **by(p, bpc.LOADER_ACCOUNT), "change": {"actions": ["update"], "before": {},
                                                 "after": {}}}),
    "plan errored": lambda p: p.update(errored=True),
    "loader key": lambda p: p["resource_changes"].append(_rc(
        "google_service_account_key.loader[0]", "google_service_account_key", "loader", {},
        ["create"], index=0)),
}


@pytest.mark.parametrize("name", RECOVERY_BAD)
def test_adversarial_recovery_plans_are_rejected(name):
    p = recovery_plan([bpc.LOADER_IMPERSONATION], [bpc.LOADER_OBJECTS_GRANT])
    RECOVERY_BAD[name](p)
    assert not check(p, "loader-recovery")["ok"], name


def test_recovery_is_not_a_substitute_for_revoke():
    """A full window revocation (table grants in state) must use loader-revoke."""
    assert not check(plan("loader-revoke"), "loader-recovery")["ok"]
    problems = check(plan("loader-revoke"), "loader-recovery")["problems"]
    assert sum(a in x for x in problems for a in bpc.LOADER_TABLE_GRANTS) >= 3


def test_partial_state_fails_revoke_and_passes_only_the_partial_modes():
    p = recovery_plan(EXPIRING)
    for mode in bpc.BOOTSTRAP_MODES:
        assert check(p, mode)["ok"] is (mode in bpc.PARTIAL_GRANTS), mode
    problems = check(p, "loader-revoke")["problems"]
    assert sum("expected delete missing" in x for x in problems) == 3


def test_recovery_drift_table_matches_revoke_exactly():
    assert bpc.LOADER_ETAG_DRIFT_BY_MODE["loader-recovery"] == \
        bpc.LOADER_ETAG_DRIFT_BY_MODE["loader-revoke"]
    assert bpc.LOADER_EXPIRING_GRANTS == bpc.LOADER_GRANTS - bpc.LOADER_TABLE_GRANTS


def test_cli_recovery_mode():
    ok = _cli(recovery_plan([bpc.LOADER_JOBS_GRANT], [bpc.LOADER_IMPERSONATION]),
              "--mode", "loader-recovery")
    assert ok.returncode == 0, ok.stdout
    report = json.loads(ok.stdout)
    assert report["absent_from_state"] == [bpc.LOADER_OBJECTS_GRANT]
    assert "named-operator" not in ok.stdout
    bad = recovery_plan()
    bad["resource_changes"].append(_table_rc(["delete"]))
    assert _cli(bad, "--mode", "loader-recovery").returncode == 1


# ---------------------------------------------------------------- loader-revoke-partial (M7D)

ALL = sorted(bpc.LOADER_GRANTS)
TABLES = sorted(bpc.LOADER_TABLE_GRANTS)


def partial_plan(deleted=(), removed=(), drift=()) -> dict:
    """Base tfvars re-plan after a revoke (or access) apply that failed part-way."""
    p = plan("loader-revoke")
    full = {rc["address"]: rc for rc in p["resource_changes"]
            if rc["address"] in bpc.LOADER_GRANTS}
    p["resource_changes"] = [rc for rc in p["resource_changes"]
                             if rc["address"] not in bpc.LOADER_GRANTS]
    p["resource_changes"] += [full[a] for a in deleted]
    p["resource_drift"] = ([_rc(a, "x", "y", {}, ["delete"]) for a in removed]
                           + [_loader_drift(a) for a in drift])
    return p


@pytest.mark.parametrize("deleted,removed", [
    (TABLES, ()),                                       # revoke removed the expiring three
    ([TABLES[0]], ()),                                  # one table grant left
    (EXPIRING, ()),                                     # tables removed first
    ([bpc.LOADER_IMPERSONATION, TABLES[1]], [bpc.LOADER_JOBS_GRANT]),
    ((), [TABLES[2]]),                                  # last grant removed by hand
    (ALL[:5], ()),                                      # five of six still recorded
    ((), ()),                                           # everything already gone from state
])
def test_partial_revoke_accepts_every_honest_subset(deleted, removed):
    report = check(partial_plan(deleted, removed), "loader-revoke-partial")
    assert report["ok"], report["problems"]
    assert (report["creates"], report["unchanged"]) == (0, 15)
    assert report["delete_addresses"] == sorted(deleted)
    assert report["already_removed"] == sorted(removed)
    assert report["absent_from_state"] == sorted(
        bpc.LOADER_GRANTS - set(deleted) - set(removed))
    assert report["live_iam_readback_required"] is True


def test_every_single_grant_subset_passes_and_deletes_exactly_that_grant():
    for addr in ALL:
        report = check(partial_plan([addr]), "loader-revoke-partial")
        assert report["ok"], (addr, report["problems"])
        assert report["delete_addresses"] == [addr]


@pytest.mark.parametrize("removed", [(), [bpc.LOADER_JOBS_GRANT], TABLES])
def test_partial_revoke_refuses_a_complete_revocation(removed):
    """All six in state (deleted or drift-removed) must go through strict loader-revoke."""
    deleted = [a for a in ALL if a not in removed]
    report = check(partial_plan(deleted, removed), "loader-revoke-partial")
    assert not report["ok"]
    assert any("use --mode loader-revoke" in x for x in report["problems"])
    assert check(partial_plan(deleted, removed), "loader-revoke")["ok"]


def test_partial_revoke_accepts_strict_etag_drift_on_loader_project_grants():
    p = partial_plan([bpc.LOADER_JOBS_GRANT, *TABLES], drift=[bpc.LOADER_JOBS_GRANT])
    p["resource_drift"].append(_runner_drift())
    report = check(p, "loader-revoke-partial")
    assert report["ok"], report["problems"]
    assert report["accepted_etag_drift"] == sorted([bpc.LOADER_JOBS_GRANT, bpc.RUNNER_GRANT])


def _grant(addr, actions, **values):
    rc = copy.deepcopy(next(g for g in grants(["delete"], with_ids=True)
                            if g["address"] == addr))
    side = {**rc["change"]["before"], **values}
    return _rc(addr, rc["type"], rc["name"], side, actions, index=rc.get("index"))


PARTIAL_BAD = {
    # unexpected creates
    "table grant recreated": lambda p: p["resource_changes"].append(
        _grant(TABLES[2], ["create"])),
    "jobs grant recreated": lambda p: p["resource_changes"].append(
        _grant(bpc.LOADER_JOBS_GRANT, ["create"])),
    "fourth table grant created": lambda p: p["resource_changes"].append(_rc(
        'google_bigquery_table_iam_member.loader["transaction_daily"]',
        "google_bigquery_table_iam_member", "loader",
        {"project": P, "dataset_id": "bankpilot_dev", "table_id": "transaction_daily",
         "role": f"{R}loader_tables", "member": LOADER, "condition": []}, ["create"],
        index="transaction_daily")),
    "loader key created": lambda p: p["resource_changes"].append(_rc(
        "google_service_account_key.loader[0]", "google_service_account_key", "loader", {},
        ["create"], index=0)),
    # unexpected updates / replacements / left-in-place
    "table grant updated": lambda p: p["resource_changes"].append(
        _grant(TABLES[2], ["update"])),
    "impersonation replaced": lambda p: p["resource_changes"].append(
        _grant(bpc.LOADER_IMPERSONATION, ["delete", "create"])),
    "table grant left unchanged": lambda p: p["resource_changes"].append(
        _grant(TABLES[2], ["no-op"])),
    "loader role updated": lambda p: by(
        p, 'google_project_iam_custom_role.loader["loader_tables"]')["change"].update(
        actions=["update"]),
    # unexpected deletions
    "mart table grant deleted": lambda p: p["resource_changes"].append(_rc(
        'google_bigquery_table_iam_member.loader["transaction_daily"]',
        "google_bigquery_table_iam_member", "loader",
        {"project": P, "dataset_id": "bankpilot_dev", "table_id": "transaction_daily",
         "role": f"{R}loader_tables", "member": LOADER, "condition": [], "id": "x"},
        ["delete"], index="transaction_daily")),
    "runner grant deleted": lambda p: by(p, bpc.RUNNER_GRANT)["change"].update(
        actions=["delete"], after=None),
    "provisioner grant deleted": lambda p: p["resource_changes"].append(_rc(
        bpc.PROVISIONER_GRANT, "google_project_iam_member", "provisioner",
        {"project": P, "role": f"{R}provision", "member": PROVISIONER,
         "condition": [bpc.expiry_condition(EXPIRY)]}, ["delete"], index=0)),
    "M6 impersonation deleted": lambda p: p["resource_changes"].append(_rc(
        bpc.IMPERSONATION_GRANT, "google_service_account_iam_member", "operator_impersonation",
        {"service_account_id": f"projects/{P}/serviceAccounts/{bpc.PROVISIONER_EMAIL}",
         "role": f"{R}impersonate", "member": OPERATOR,
         "condition": [bpc.expiry_condition(EXPIRY)]}, ["delete"], index=0)),
    "loader account destroyed": lambda p: by(p, bpc.LOADER_ACCOUNT)["change"].update(
        actions=["delete"], after=None),
    "loader role destroyed": lambda p: by(
        p, 'google_project_iam_custom_role.loader["loader_jobs"]')["change"].update(
        actions=["delete"], after=None),
    "audit config deleted": lambda p: by(p, bpc.AUDIT_ADDRESS)["change"].update(
        actions=["delete"], after=None),
    "provisioner already removed": lambda p: p["resource_drift"].append(
        _rc(bpc.PROVISIONER_GRANT, "x", "y", {}, ["delete"])),
    "mart grant already removed": lambda p: p["resource_drift"].append(_rc(
        'google_bigquery_table_iam_member.loader["transaction_daily"]', "x", "y", {},
        ["delete"])),
    # deleted grant is not the exact approved grant
    "deleted table grant had a condition": lambda p: p["resource_changes"].append(
        _grant(TABLES[2], ["delete"], condition=[bpc.loader_expiry_condition(EXPIRY)])),
    "deleted table grant wrong role": lambda p: p["resource_changes"].append(
        _grant(TABLES[2], ["delete"], role="roles/bigquery.dataEditor")),
    "deleted table grant wrong member": lambda p: p["resource_changes"].append(
        _grant(TABLES[2], ["delete"], member=RUNNER)),
    "deleted table grant other dataset": lambda p: p["resource_changes"].append(
        _grant(TABLES[2], ["delete"], dataset_id="other_dataset")),
    "deleted table key and id disagree": lambda p: p["resource_changes"].append(
        _grant(TABLES[2], ["delete"], table_id="accounts")),
    "deleted objects grant had no condition": lambda p: p["resource_changes"].append(
        _grant(bpc.LOADER_OBJECTS_GRANT, ["delete"], condition=[])),
    "deleted objects grant jobs-style title": lambda p: p["resource_changes"].append(
        _grant(bpc.LOADER_OBJECTS_GRANT, ["delete"],
               condition=[bpc.loader_expiry_condition(EXPIRY)])),
    "deleted impersonation for another user": lambda p: p["resource_changes"].append(
        _grant(bpc.LOADER_IMPERSONATION, ["delete"], member="group:ops@example.invalid")),
    "deleted impersonation on provisioner": lambda p: p["resource_changes"].append(
        _grant(bpc.LOADER_IMPERSONATION, ["delete"], service_account_id=(
            f"projects/{P}/serviceAccounts/{bpc.PROVISIONER_EMAIL}"))),
    "deleted grant other project": lambda p: p["resource_changes"].append(
        _grant(bpc.LOADER_OBJECTS_GRANT, ["delete"], project="other-project")),
    # double accounting / drift
    "removed and also deleted": lambda p: (
        p["resource_changes"].append(_grant(TABLES[2], ["delete"])),
        p["resource_drift"].append(_rc(TABLES[2], "x", "y", {}, ["delete"]))),
    "duplicate removal": lambda p: p["resource_drift"].extend(
        [_rc(TABLES[2], "x", "y", {}, ["delete"])] * 2),
    "removal reported as update": lambda p: p["resource_drift"].append(
        _rc(TABLES[2], "x", "y", {}, ["update"])),
    "drift widened a loader grant": lambda p: (
        p["resource_changes"].append(_grant(bpc.LOADER_OBJECTS_GRANT, ["delete"])),
        p["resource_drift"].append(_loader_drift(bpc.LOADER_OBJECTS_GRANT,
                                                 role="roles/bigquery.admin"))),
    "etag drift on a grant not deleted": lambda p: p["resource_drift"].append(
        _loader_drift(bpc.LOADER_OBJECTS_GRANT)),
    "etag drift on a table grant": lambda p: p["resource_drift"].append({
        **_grant(TABLES[0], ["update"]), "change": {
            "actions": ["update"], "before": _grant(TABLES[0], ["no-op"])["change"]["before"],
            "after": {**_grant(TABLES[0], ["no-op"])["change"]["before"], "etag": "z"}}}),
    "drift on loader identity": lambda p: p["resource_drift"].append({
        **by(p, bpc.LOADER_ACCOUNT), "change": {"actions": ["update"], "before": {},
                                                 "after": {}}}),
    # variables and plan state
    "access still enabled": lambda p: p["variables"]["loader_access_enabled"].update(value=True),
    "expiry left set": lambda p: p["variables"]["loader_access_expiry"].update(value=EXPIRY),
    "bucket left set": lambda p: p["variables"]["loader_bucket_name"].update(value=BUCKET),
    "prefix left set": lambda p: p["variables"]["loader_object_prefix"].update(value=PREFIX),
    "identity disabled": lambda p: p["variables"]["loader_identity_enabled"].update(
        value=False),
    "provisioner access on": lambda p: p["variables"]["provisioner_access_enabled"].update(
        value=True),
    "plan errored": lambda p: p.update(errored=True),
    "binding text": lambda p: p.update(configuration={"x": "google_project_iam_binding"}),
}


@pytest.mark.parametrize("name", PARTIAL_BAD)
def test_adversarial_partial_revoke_plans_are_rejected(name):
    # Base: two table grants and the impersonation still recorded; jobs removed by hand.
    p = partial_plan([TABLES[0], TABLES[1], bpc.LOADER_IMPERSONATION],
                     [bpc.LOADER_JOBS_GRANT])
    assert check(p, "loader-revoke-partial")["ok"]
    PARTIAL_BAD[name](p)
    assert not check(p, "loader-revoke-partial")["ok"], name


def test_normal_revoke_stays_strict_for_partial_states():
    for deleted, removed in ((TABLES, ()), (EXPIRING, ()), ((), [TABLES[0]])):
        problems = check(partial_plan(deleted, removed), "loader-revoke")["problems"]
        missing = bpc.LOADER_GRANTS - set(deleted) - set(removed)
        assert sum("expected delete missing" in x for x in problems) == len(missing)


def test_partial_modes_share_the_revoke_drift_table():
    assert bpc.LOADER_ETAG_DRIFT_BY_MODE["loader-revoke-partial"] == \
        bpc.LOADER_ETAG_DRIFT_BY_MODE["loader-revoke"]
    assert bpc.PARTIAL_GRANTS == {"loader-recovery": bpc.LOADER_EXPIRING_GRANTS,
                                  "loader-revoke-partial": bpc.LOADER_GRANTS}


@pytest.mark.parametrize("source", ["loader-identity", "loader-access"])
def test_partial_revoke_rejects_window_plans(source):
    assert not check(plan(source), "loader-revoke-partial")["ok"]


def test_cli_partial_revoke():
    ok = _cli(partial_plan([TABLES[0]], [bpc.LOADER_IMPERSONATION]),
              "--mode", "loader-revoke-partial")
    assert ok.returncode == 0, ok.stdout
    report = json.loads(ok.stdout)
    assert report["delete_addresses"] == [TABLES[0]]
    assert report["live_iam_readback_required"] is True
    assert "named-operator" not in ok.stdout
    full = _cli(plan("loader-revoke"), "--mode", "loader-revoke-partial")
    assert full.returncode == 1
