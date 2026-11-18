"""M9D G2: pilot-scores accepts the refresh drift of the real (failed) G2 plan, and nothing near it.

The fixture is the saved G2 plan's ``resource_drift`` section, unmodified except that the
dataset owner is replaced by the test placeholder. No Terraform, gcloud or network access.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
import test_pilot_loaded_plan_check as tl
import test_pilot_scores_plan_check as ts

bpc, OWNER = tl.bpc, tl.OWNER
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/m9d_g2_resource_drift.redacted.json").read_text())
DATASET = bpc.DATASET_ADDRESS
OWNER_EMAIL = OWNER.removeprefix("user:")
RUNNER = bpc.RUNNER_EMAIL
METADATA_ROLE = f"{bpc.ROLE_PREFIX}metadata"
MARTS, INPUTS = ts.MARTS, tl.INPUTS


def member(t: str) -> str:
    return f'module.bigquery.google_bigquery_table_iam_member.runner["{t}"]'


def _drift() -> list[dict]:
    return copy.deepcopy(FIXTURE["resource_drift"])


LOADED = {"packet_id": "m7p-a89332976c69d583137f9323",
          "tables": {rc["index"]: {"num_rows": rc["change"]["after"]["num_rows"],
                                   "num_bytes": rc["change"]["after"]["num_bytes"]}
                     for rc in _drift() if rc["type"] == "google_bigquery_table"}}


def real_plan() -> dict:
    """The synthetic pilot-scores plan with the real G2 drift and refreshed table values."""
    plan = ts.scores_plan()
    plan["timestamp"] = FIXTURE["timestamp"]
    plan["resource_drift"] = _drift()
    refreshed = {rc["address"]: rc["change"]["after"] for rc in plan["resource_drift"]
                 if rc["type"] == "google_bigquery_table"}
    for rc in plan["resource_changes"]:
        if rc["address"] in refreshed:
            rc["change"]["before"] = rc["change"]["after"] = copy.deepcopy(
                refreshed[rc["address"]])
    return plan


def check(plan, mode="pilot-scores", loaded=LOADED, owner=OWNER):
    return bpc.check_plan(plan, mode, dataset_owner=owner, loaded=loaded)


def drift(plan, address):
    return next(rc for rc in plan["resource_drift"] if rc["address"] == address)


def _rc(plan, address):
    return next(rc for rc in plan["resource_changes"] if rc["address"] == address)


# ------------------------------------------------------------------------------ fixture
def test_fixture_is_the_eight_findings_plus_three_loads_and_redacted():
    addresses = [rc["address"] for rc in FIXTURE["resource_drift"]]
    assert len(addresses) == 11 and DATASET in addresses
    assert {member(t) for t in INPUTS} <= set(addresses)
    text = json.dumps(FIXTURE)
    emails = set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]+", text))
    assert emails == {OWNER_EMAIL, RUNNER, bpc.PROVISIONER_EMAIL}
    assert "user:" not in text and "gs://" not in text


def test_failed_g2_findings_are_reproduced_before_the_fix_semantics():
    """The same drift is still rejected by pilot-loaded (and so was A10's original form)."""
    assert not check(real_plan(), mode="pilot-loaded")["ok"]


# ------------------------------------------------------------------------------ positive
def test_real_g2_drift_is_accepted():
    report = check(real_plan())
    assert report["ok"], report["problems"]
    assert (report["creates"], report["unchanged"], report["deletes"]) == (2, 18, 0)
    assert len(report["accepted_load_drift"]) == 7
    assert report["accepted_access_drift"] == [DATASET]
    assert report["accepted_etag_drift"] == sorted(member(t) for t in INPUTS)


def test_drift_already_reconciled_in_state_is_accepted():
    plan = real_plan()
    plan["resource_drift"] = [rc for rc in plan["resource_drift"]
                              if rc["type"] == "google_bigquery_table"]
    report = check(plan)
    assert report["ok"], report["problems"]
    assert report["accepted_access_drift"] == report["accepted_etag_drift"] == []


# ------------------------------------------------------------------------------ dataset
def _access(plan, side="after"):
    return drift(plan, DATASET)["change"][side]["access"]


def _entry(role, **value):
    entry = {**dict.fromkeys(bpc.ACCESS_ENTRY_KEYS, ""),
             "condition": [], "dataset": [], "routine": [], "view": []}
    return {**entry, "role": role, **value}


DATASET_BAD = {
    "project group still present": lambda p: _access(p).append(
        _entry("READER", special_group="projectReaders")),
    "public access": lambda p: _access(p).append(
        _entry("READER", iam_member="allAuthenticatedUsers")),
    "group grant": lambda p: _access(p).append(
        _entry("READER", group_by_email="team@example.invalid")),
    "domain grant": lambda p: _access(p).append(_entry("READER", domain="example.invalid")),
    "authorised view": lambda p: _access(p).append(_entry("", view=[{"x": 1}])),
    "owner swapped": lambda p: _access(p)[0].update(user_by_email="other@example.invalid"),
    "provisioner still owner": lambda p: _access(p).append(
        _entry("OWNER", user_by_email=bpc.PROVISIONER_EMAIL)),
    "runner widened to writer": lambda p: next(
        e for e in _access(p) if e["user_by_email"] == RUNNER).update(role="WRITER"),
    "runner missing": lambda p: _access(p).remove(
        next(e for e in _access(p) if e["user_by_email"] == RUNNER)),
    "conditional entry": lambda p: _access(p)[0].update(condition=[{"expression": "true"}]),
    "duplicate entry": lambda p: _access(p).append(copy.deepcopy(_access(p)[0])),
    "extra key": lambda p: _access(p)[0].update(extra="x"),
    "recorded access not creation default": lambda p: _access(p, "before").pop(),
    "labels change rides along": lambda p: drift(p, DATASET)["change"]["after"].update(
        labels={"environment": "prod"}),
    "location change rides along": lambda p: drift(p, DATASET)["change"]["after"].update(
        location="US"),
    "etag unchanged": lambda p: drift(p, DATASET)["change"]["after"].update(
        etag=drift(p, DATASET)["change"]["before"]["etag"]),
    "time moves backwards": lambda p: drift(p, DATASET)["change"]["after"].update(
        last_modified_time=1),
    "time after plan": lambda p: drift(p, DATASET)["change"]["after"].update(
        last_modified_time=4_000_000_000_000),
    "unknown values": lambda p: drift(p, DATASET)["change"].update(
        after_unknown={"access": True}),
    "action replace": lambda p: drift(p, DATASET)["change"].update(
        actions=["delete", "create"]),
    "dataset policy updated": lambda p: _rc(p, bpc.DATASET_POLICY)["change"].update(
        actions=["update"]),
    "duplicate dataset drift": lambda p: p["resource_drift"].append(
        copy.deepcopy(drift(p, DATASET))),
}


@pytest.mark.parametrize("name", DATASET_BAD)
def test_dataset_access_mutations_are_rejected(name):
    plan = real_plan()
    DATASET_BAD[name](plan)
    report = check(plan)
    assert not report["ok"], name


def test_dataset_rule_needs_the_approved_owner():
    report = check(real_plan(), owner="user:other@example.invalid")
    assert not report["ok"]
    assert any("live access is not exactly" in p for p in report["problems"])


# ------------------------------------------------------------------------------ marts
def _mart(plan, t, side="after"):
    return drift(plan, ts.tl.table(t))["change"][side]


MART_BAD = {
    "etag changed": lambda p: _mart(p, "account_daily").update(etag="changed"),
    "rows differ from M8": lambda p: _mart(p, "customer_daily").update(num_rows=3411),
    "bytes differ from M8": lambda p: _mart(p, "transaction_daily").update(num_bytes=80_031),
    "not from empty": lambda p: _mart(p, "operations_daily", "before").update(num_rows=1),
    "rows unchanged": lambda p: _mart(p, "operations_daily", "before").update(
        num_rows=3762),
    "time not shared": lambda p: _mart(p, "account_daily").update(
        last_modified_time=_mart(p, "account_daily")["last_modified_time"] - 1),
    "time unchanged": lambda p: _mart(p, "account_daily", "before").update(
        last_modified_time=_mart(p, "account_daily")["last_modified_time"]),
    "schema change rides along": lambda p: _mart(p, "customer_daily").update(schema="[]"),
    "protection removed": lambda p: _mart(p, "customer_daily").update(
        deletion_protection=False),
}


@pytest.mark.parametrize("name", MART_BAD)
def test_mart_dml_mutations_are_rejected(name):
    plan = real_plan()
    MART_BAD[name](plan)
    assert not check(plan)["ok"], name


def test_inputs_still_need_a_load_job_etag_change():
    plan = real_plan()
    d = drift(plan, ts.tl.table("customers"))["change"]
    d["before"]["etag"] = d["after"]["etag"]
    assert not check(plan)["ok"]


# ------------------------------------------------------------------------------ members
def _member(plan, t, side="after"):
    return drift(plan, member(t))["change"][side]


def _member_drift_on(t):
    def mutate(plan):
        rc = copy.deepcopy(drift(plan, member("accounts")))
        rc["address"], rc["index"] = member(t), t
        plan["resource_drift"].append(rc)
    return mutate


MEMBER_BAD = {
    "role widened": lambda p: _member(p, "accounts").update(
        role=f"{bpc.ROLE_PREFIX}tables"),
    "role widened on both sides": lambda p: [_member(p, "customers", s).update(
        role="roles/bigquery.dataEditor") for s in ("before", "after")],
    "member changed": lambda p: _member(p, "customers").update(
        member=f"serviceAccount:{bpc.PROVISIONER_EMAIL}"),
    "condition added": lambda p: _member(p, "curated_transactions").update(
        condition=[{"expression": "true", "title": "x", "description": ""}]),
    "table changed": lambda p: _member(p, "accounts").update(table_id="customers"),
    "etag unchanged": lambda p: _member(p, "accounts").update(
        etag=_member(p, "accounts", "before")["etag"]),
    "etag empty": lambda p: _member(p, "accounts").update(etag=""),
    "action delete": lambda p: drift(p, member("accounts"))["change"].update(
        actions=["delete"]),
    "mart member drift": _member_drift_on("customer_daily"),
    "score member drift": _member_drift_on("transaction_risk_scores"),
    "duplicate member drift": lambda p: p["resource_drift"].append(
        copy.deepcopy(drift(p, member("accounts")))),
}


@pytest.mark.parametrize("name", MEMBER_BAD)
def test_member_etag_mutations_are_rejected(name):
    plan = real_plan()
    MEMBER_BAD[name](plan)
    assert not check(plan)["ok"], name


# ------------------------------------------------------------------------------ elsewhere
@pytest.mark.parametrize("address", [
    "module.storage.google_storage_bucket.artifacts",
    "module.storage.google_storage_bucket_iam_policy.runner",
    bpc.DATASET_POLICY,
    'module.bigquery.google_bigquery_table.products["transaction_risk_scores"]',
])
def test_drift_anywhere_else_is_still_rejected(address):
    plan = real_plan()
    rc = copy.deepcopy(drift(plan, member("accounts")))
    rc["address"] = address
    plan["resource_drift"].append(rc)
    assert not check(plan)["ok"]


def test_new_rules_do_not_apply_to_other_pilot_modes():
    plan = real_plan()
    plan["resource_drift"] = [rc for rc in plan["resource_drift"]
                              if rc["type"] != "google_bigquery_table"]
    for mode in ("pilot-loaded", "pilot-recovery"):
        report = check(plan, mode=mode, loaded=tl.LOADED)
        assert not report["ok"], mode
        # pilot-loaded: unknown drift; pilot-recovery: its M6T rule rejects the access change
        assert any(p.startswith(f"drift detected outside Terraform: {DATASET}")
                   or p.startswith(f"{DATASET}: drift changes non-metadata attributes")
                   for p in report["problems"]), mode
        assert any(f"drift detected outside Terraform: {member('accounts')}" == p
                   for p in report["problems"]), mode
