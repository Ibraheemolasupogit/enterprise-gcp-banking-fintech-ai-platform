"""M7C2 G6: ``pilot-loaded`` accepts only the exact row/byte metadata drift of the loads.

Synthetic ``terraform show -json`` documents only; no Terraform command is run.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from m7c2_support import build_fixture_packet

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("bootstrap_plan_check_m7c2",
                                               ROOT / "scripts" / "bootstrap_plan_check.py")
bpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bpc)
_rspec = importlib.util.spec_from_file_location("pilot_recovery_fixture_m7c2",
                                                ROOT / "tests" /
                                                "test_pilot_recovery_plan_check.py")
recovery = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(recovery)

OWNER = recovery.OWNER
INPUTS = ("accounts", "curated_transactions", "customers")
LOADED = {"packet_id": "m7p-0123456789abcdef01234567",
          "tables": {"customers": {"num_rows": 100, "num_bytes": 23_456},
                     "accounts": {"num_rows": 150, "num_bytes": 31_002},
                     "curated_transactions": {"num_rows": 4000, "num_bytes": 1_234_567}}}
PLANNED_MS = 1_763_000_000_000  # 2025-11-13T02:13:20Z
TIMESTAMP = "2025-11-13T02:13:20Z"


def table(t: str) -> str:
    return f'module.bigquery.google_bigquery_table.products["{t}"]'


def loaded_plan(drift: tuple[str, ...] = INPUTS) -> dict:
    """All 18 resources unchanged after the loads; refresh drift on ``drift`` tables."""
    plan = recovery.recovery_plan()
    plan["timestamp"] = TIMESTAMP
    for rc in plan["resource_changes"]:
        rc["change"]["actions"] = ["no-op"]
        rc["change"]["before"] = rc["change"]["after"]
        if rc["type"] == "google_bigquery_table":
            name = rc["address"].split('"')[1]
            spec = LOADED["tables"].get(name, {"num_rows": 0, "num_bytes": 0})
            values = rc["change"]["after"]
            values.update(num_rows=spec["num_rows"], num_bytes=spec["num_bytes"],
                          num_long_term_bytes=0, etag="loaded" if name in INPUTS else "e0",
                          last_modified_time=PLANNED_MS - 60_000 if name in INPUTS
                          else PLANNED_MS - 9_000_000, labels={"environment": "dev"},
                          schema="[...]")
            rc["change"]["before"] = rc["change"]["after"] = values
    plan["resource_drift"] = [drift_entry(t) for t in drift]
    return plan


def drift_entry(t: str) -> dict:
    after = copy.deepcopy(next(rc for rc in loaded_plan(())["resource_changes"]
                               if rc["address"] == table(t))["change"]["after"])
    before = {**after, "num_rows": 0, "num_bytes": 0, "etag": "empty",
              "last_modified_time": PLANNED_MS - 9_000_000}
    return {"address": table(t), "mode": "managed", "type": "google_bigquery_table",
            "name": "products", "index": t,
            "change": {"actions": ["update"], "before": before, "after": after,
                       "after_unknown": {}}}


def check(plan, loaded=LOADED, owner=OWNER, mode="pilot-loaded"):
    return bpc.check_plan(plan, mode, dataset_owner=owner, loaded=loaded)


def drift_of(plan, t):
    return next(rc for rc in plan["resource_drift"] if rc["address"] == table(t))


def rc_of(plan, t):
    return next(rc for rc in plan["resource_changes"] if rc["address"] == table(t))


# ------------------------------------------------------------------ accepted shapes
def test_loaded_plan_with_load_drift_is_accepted():
    r = check(loaded_plan())
    assert r["ok"], r["problems"]
    assert r["unchanged"] == 18 and r["creates"] == 0 and r["deletes"] == 0
    assert r["accepted_load_drift"] == sorted(table(t) for t in INPUTS)
    assert r["packet_id"] == LOADED["packet_id"] and r["mode"] == "pilot-loaded"


@pytest.mark.parametrize("drift", [(), ("customers",), ("accounts", "curated_transactions")])
def test_drift_already_persisted_in_state_is_accepted(drift):
    r = check(loaded_plan(drift))
    assert r["ok"], r["problems"]
    assert r["accepted_load_drift"] == sorted(table(t) for t in drift)


def test_other_modes_stay_unchanged_and_reject_a_loaded_plan():
    for mode in ("pilot", "pilot-recovery"):
        r = bpc.check_plan(loaded_plan(), mode, dataset_owner=OWNER)
        assert not r["ok"]
        assert any("drift" in p or "expected create missing" in p for p in r["problems"])
    assert not check(recovery.recovery_plan())["ok"]
    assert bpc.check_plan(recovery.recovery_plan(), "pilot-recovery", dataset_owner=OWNER)["ok"]


def test_dataset_owner_is_still_required():
    assert not check(loaded_plan(), owner=None)["ok"]
    assert not check(loaded_plan(), owner="user:other@example.invalid")["ok"]


# ------------------------------------------------------------------ the expectation
@pytest.mark.parametrize("loaded", [
    None, {}, {"tables": {}}, {"tables": {t: {"num_rows": 1, "num_bytes": 1}
                                          for t in ("customers", "accounts")}},
    {"tables": {**LOADED["tables"], "alerts": {"num_rows": 1, "num_bytes": 1}}},
    {"tables": {**LOADED["tables"], "customers": {"num_rows": 0, "num_bytes": 1}}},
    {"tables": {**LOADED["tables"], "customers": {"num_rows": True, "num_bytes": 1}}},
    {"tables": {**LOADED["tables"], "customers": {"num_rows": "100", "num_bytes": 1}}},
    {"tables": {**LOADED["tables"], "customers": {"num_rows": 100}}},
    {"tables": {**LOADED["tables"], "customers": {"num_rows": 1, "num_bytes": 1, "x": 1}}},
])
def test_malformed_expectations_fail_closed(loaded):
    r = check(loaded_plan(), loaded=loaded)
    assert not r["ok"] and any("expectation" in p for p in r["problems"])


# ------------------------------------------------------------------ rejected drift
DRIFT_MUTATIONS = {
    "schema change": lambda b, a: a.update(schema="[changed]"),
    "labels": lambda b, a: a.update(labels={"environment": "prod"}),
    "deletion protection off": lambda b, a: a.update(deletion_protection=False),
    "expiration": lambda b, a: a.update(expiration_time=1),
    "resource tags": lambda b, a: a.update(resource_tags={}),
    "long term bytes": lambda b, a: a.update(num_long_term_bytes=5),
    "rows differ": lambda b, a: a.update(num_rows=99),
    "rows text": lambda b, a: a.update(num_rows="100"),
    "rows float": lambda b, a: a.update(num_rows=100.0),
    "bytes differ": lambda b, a: a.update(num_bytes=a["num_bytes"] + 1),
    "bytes zero": lambda b, a: a.update(num_bytes=0),
    "reload on data": lambda b, a: b.update(num_rows=5, num_bytes=10),
    "before rows bool": lambda b, a: b.update(num_rows=False),
    "etag unchanged": lambda b, a: a.update(etag=b["etag"]),
    "etag empty": lambda b, a: a.update(etag=""),
    "time unchanged": lambda b, a: a.update(last_modified_time=b["last_modified_time"]),
    "time backwards": lambda b, a: a.update(last_modified_time=b["last_modified_time"] - 1),
    "time after plan": lambda b, a: a.update(last_modified_time=PLANNED_MS + 3_600_000),
    "time text": lambda b, a: a.update(last_modified_time=str(PLANNED_MS)),
    "rows unchanged": lambda b, a: b.update(num_rows=a["num_rows"]),
    "attribute added": lambda b, a: a.update(friendly_name="x"),
    "table id": lambda b, a: a.update(table_id="other"),
}


@pytest.mark.parametrize("name", sorted(DRIFT_MUTATIONS))
@pytest.mark.parametrize("t", INPUTS)
def test_drift_mutations_are_rejected(name, t):
    plan = loaded_plan()
    entry = drift_of(plan, t)
    DRIFT_MUTATIONS[name](entry["change"]["before"], entry["change"]["after"])
    r = check(plan)
    assert not r["ok"], name
    assert table(t) not in r["accepted_load_drift"]


@pytest.mark.parametrize("actions", [["delete"], ["create"], ["delete", "create"], ["no-op"],
                                     ["read"]])
def test_drift_actions_other_than_update_are_rejected(actions):
    plan = loaded_plan()
    drift_of(plan, "customers")["change"]["actions"] = actions
    assert not check(plan)["ok"]


def test_drift_shape_problems_are_rejected():
    for mutate in (lambda e: e.update(mode="data"), lambda e: e.update(type="google_x"),
                   lambda e: e["change"].update(after_unknown={"num_rows": True}),
                   lambda e: e["change"].update(before=None),
                   lambda e: e["change"]["before"].pop("schema")):
        plan = loaded_plan()
        mutate(drift_of(plan, "accounts"))
        assert not check(plan)["ok"]


def test_duplicate_drift_entry_is_rejected():
    plan = loaded_plan()
    plan["resource_drift"].append(copy.deepcopy(plan["resource_drift"][0]))
    assert not check(plan)["ok"]


@pytest.mark.parametrize("address", [
    'module.bigquery.google_bigquery_table.products["customer_daily"]',
    "module.bigquery.google_bigquery_dataset.foundation",
    "module.storage.google_storage_bucket.artifacts",
    "module.storage.google_storage_bucket_iam_policy.runner",
    "module.bigquery.google_bigquery_dataset_iam_policy.metadata",
    'module.bigquery.google_bigquery_table_iam_member.runner["customers"]',
    'google_bigquery_table_iam_member.loader["customers"]',
])
def test_drift_anywhere_else_is_rejected(address):
    plan = loaded_plan()
    entry = copy.deepcopy(drift_of(plan, "customers"))
    entry["address"] = address
    plan["resource_drift"].append(entry)
    r = check(plan)
    assert not r["ok"] and any(f"drift detected outside Terraform: {address}" in p
                               for p in r["problems"])


# ------------------------------------------------------------------ planned changes
def test_marts_must_still_be_empty():
    plan = loaded_plan()
    mart = rc_of(plan, "customer_daily")["change"]
    mart["before"]["num_rows"] = 1
    assert not check(plan)["ok"]


@pytest.mark.parametrize("attr,value", [("num_rows", 101), ("num_bytes", 1),
                                        ("num_long_term_bytes", 1), ("num_rows", None),
                                        ("num_bytes", "23456")])
def test_refreshed_input_sizes_must_be_exact(attr, value):
    plan = loaded_plan(())
    values = rc_of(plan, "customers")["change"]["before"]
    if value is None:
        del values[attr]
    else:
        values[attr] = value
    r = check(plan)
    assert not r["ok"] and any(f"refreshed {attr}" in p for p in r["problems"])


@pytest.mark.parametrize("actions", [["update"], ["delete", "create"], ["create"],
                                     ["delete"]])
def test_any_planned_change_is_rejected(actions):
    plan = loaded_plan()
    rc_of(plan, "customers")["change"]["actions"] = actions
    assert not check(plan)["ok"]


def test_extra_resources_and_iam_are_rejected():
    plan = loaded_plan()
    extra = copy.deepcopy(rc_of(plan, "customers"))
    extra["address"] = table("alerts")
    extra["change"]["actions"] = ["create"]
    plan["resource_changes"].append(extra)
    assert not check(plan)["ok"]
    plan = loaded_plan()
    plan["resource_changes"].append(
        {"address": "google_project_iam_member.x", "mode": "managed",
         "type": "google_project_iam_member", "name": "x",
         "change": {"actions": ["create"], "before": None,
                    "after": {"member": "user:x@example.com", "role": "roles/owner"}}})
    r = check(plan)
    assert not r["ok"] and any("forbidden token" in p for p in r["problems"])


def test_runner_table_member_change_is_rejected():
    plan = loaded_plan()
    member = next(rc for rc in plan["resource_changes"]
                  if rc["address"].endswith('runner["customers"]'))
    member["change"]["after"] = {**member["change"]["after"],
                                 "role": f"{bpc.ROLE_PREFIX}tables"}
    assert not check(plan)["ok"]


# ------------------------------------------------------------------ packet binding (CLI)
@pytest.fixture(scope="module")
def packet(tmp_path_factory):
    return build_fixture_packet(tmp_path_factory.mktemp("m7c2-g6"))


def run_main(monkeypatch, capsys, plan, *args):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(plan)))
    code = bpc.main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_cli_binds_the_verified_packet(packet, monkeypatch, capsys):
    loaded = bpc.loaded_tables(packet["packet_dir"], packet["manifest_sha256"])
    assert loaded["packet_id"] == packet["packet_id"]
    for t in INPUTS:
        assert loaded["tables"][t]["num_rows"] == packet["manifest"]["tables"][t]["row_count"]
        assert loaded["tables"][t]["num_bytes"] > 0
    global LOADED
    saved, LOADED = LOADED, loaded
    try:
        plan = loaded_plan()
    finally:
        LOADED = saved
    args = ("--mode", "pilot-loaded", f"--dataset-owner={OWNER}",
            f"--packet-dir={packet['packet_dir']}",
            f"--expected-manifest-sha256={packet['manifest_sha256']}")
    code, report = run_main(monkeypatch, capsys, plan, *args)
    assert code == 0 and report["ok"], report["problems"]
    code, report = run_main(monkeypatch, capsys, plan, *args[:-1],
                            "--expected-manifest-sha256=" + "0" * 64)
    assert code == 2 and "packet not verified" in report["problems"][0]


def test_cli_requires_packet_arguments_only_for_pilot_loaded(monkeypatch, capsys, packet):
    code, report = run_main(monkeypatch, capsys, loaded_plan(), "--mode", "pilot-loaded",
                            f"--dataset-owner={OWNER}")
    assert code == 2 and "requires --packet-dir" in report["problems"][0]
    code, report = run_main(monkeypatch, capsys, recovery.recovery_plan(), "--mode",
                            "pilot-recovery", f"--dataset-owner={OWNER}",
                            f"--packet-dir={packet['packet_dir']}")
    assert code == 2 and "apply only to pilot-loaded" in report["problems"][0]
