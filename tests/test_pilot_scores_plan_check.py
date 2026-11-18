"""M9D: pilot-scores plan mode (score table + overdue A10) and the pilot Terraform change.

Synthetic ``terraform show -json`` documents only; no Terraform, gcloud or network access.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
import test_pilot_loaded_plan_check as tl

bpc, OWNER = tl.bpc, tl.OWNER
ROOT = Path(__file__).resolve().parents[1]
MARTS = ("account_daily", "customer_daily", "operations_daily", "transaction_daily")
LOADED = {"packet_id": tl.LOADED["packet_id"],
          "tables": {**tl.LOADED["tables"],
                     "transaction_daily": {"num_rows": 1312, "num_bytes": 80_032},
                     "customer_daily": {"num_rows": 3412, "num_bytes": 238_840},
                     "account_daily": {"num_rows": 3428, "num_bytes": 243_388},
                     "operations_daily": {"num_rows": 3762, "num_bytes": 278_877}}}
SCORE_TABLE = 'module.bigquery.google_bigquery_table.products["transaction_risk_scores"]'
SCORE_MEMBER = ('module.bigquery.google_bigquery_table_iam_member.runner'
                '["transaction_risk_scores"]')
RUNNER = f"serviceAccount:{bpc.RUNNER_EMAIL}"


def _score_creates() -> list[dict]:
    fields = json.loads((ROOT / "sql/scoring/v1/transaction_risk_scores.schema.json")
                        .read_text())["fields"]
    table = {"project": bpc.PROJECT, "dataset_id": "bankpilot_dev",
             "table_id": "transaction_risk_scores", "deletion_protection": True,
             "schema": json.dumps(fields), "labels": {"environment": "dev"}}
    member = {"project": bpc.PROJECT, "dataset_id": "bankpilot_dev",
              "table_id": "transaction_risk_scores", "member": RUNNER,
              "role": f"{bpc.ROLE_PREFIX}tables", "condition": []}
    return [{"address": SCORE_TABLE, "mode": "managed", "type": "google_bigquery_table",
             "name": "products", "index": "transaction_risk_scores",
             "change": {"actions": ["create"], "before": None, "after": table,
                        "after_unknown": {"etag": True, "id": True}}},
            {"address": SCORE_MEMBER, "mode": "managed",
             "type": "google_bigquery_table_iam_member", "name": "runner",
             "index": "transaction_risk_scores",
             "change": {"actions": ["create"], "before": None, "after": member,
                        "after_unknown": {"etag": True, "id": True}}}]


def scores_plan() -> dict:
    """The 18 resources unchanged; inputs and marts loaded (state refreshed from empty)."""
    plan = tl.loaded_plan(())
    for rc in plan["resource_changes"]:
        if rc["type"] == "google_bigquery_table":
            name = rc["address"].split('"')[1]
            if name in MARTS:
                rc["change"]["before"].update(LOADED["tables"][name], etag="loaded",
                                              last_modified_time=tl.PLANNED_MS - 60_000)
    drift = []
    for name in (*tl.INPUTS, *MARTS):
        after = copy.deepcopy(tl.rc_of(plan, name)["change"]["before"])
        # Inputs: load jobs change the etag. Marts: M8 DML leaves it identical (M9D G2).
        before = {**after, "num_rows": 0, "num_bytes": 0,
                  "etag": "empty" if name in tl.INPUTS else after["etag"],
                  "last_modified_time": tl.PLANNED_MS - 9_000_000}
        drift.append({"address": tl.table(name), "mode": "managed",
                      "type": "google_bigquery_table", "name": "products", "index": name,
                      "change": {"actions": ["update"], "before": before, "after": after,
                                 "after_unknown": {}}})
    plan["resource_drift"] = drift
    plan["resource_changes"] += _score_creates()
    return plan


def check(plan, loaded=LOADED, mode="pilot-scores"):
    return bpc.check_plan(plan, mode, dataset_owner=OWNER, loaded=loaded)


def _rc(plan, address):
    return next(rc for rc in plan["resource_changes"] if rc["address"] == address)


def test_scores_plan_is_accepted_with_a10_assertions():
    report = check(scores_plan())
    assert report["ok"], report["problems"]
    assert (report["creates"], report["unchanged"], report["deletes"]) == (2, 18, 0)
    assert len(report["accepted_load_drift"]) == 7


def test_the_original_a10_mode_cannot_pass_after_m8():
    plan = scores_plan()
    assert not check(plan, loaded=tl.LOADED, mode="pilot-loaded")["ok"]


def _schema(plan, mutate):
    fields = json.loads(_rc(plan, SCORE_TABLE)["change"]["after"]["schema"])
    mutate(fields)
    _rc(plan, SCORE_TABLE)["change"]["after"]["schema"] = json.dumps(fields)


BAD = {
    "score table not protected": lambda p: _rc(p, SCORE_TABLE)["change"]["after"].update(
        deletion_protection=False),
    "schema relaxed": lambda p: _schema(p, lambda f: f[0].update(mode="NULLABLE")),
    "schema extra column": lambda p: _schema(p, lambda f: f.append(
        {"name": "label", "type": "INT64", "mode": "NULLABLE"})),
    "schema reordered": lambda p: _schema(p, lambda f: f.reverse()),
    "schema float score": lambda p: _schema(p, lambda f: f[3].update(type="FLOAT64")),
    "wrong table id": lambda p: _rc(p, SCORE_TABLE)["change"]["after"].update(
        table_id="customers"),
    "runner gets reader role": lambda p: _rc(p, SCORE_MEMBER)["change"]["after"].update(
        role=f"{bpc.ROLE_PREFIX}readers"),
    "member is the provisioner": lambda p: _rc(p, SCORE_MEMBER)["change"]["after"].update(
        member=f"serviceAccount:{bpc.PROVISIONER_EMAIL}"),
    "member conditional": lambda p: _rc(p, SCORE_MEMBER)["change"]["after"].update(
        condition=[{"expression": "true", "title": "x"}]),
    "member missing": lambda p: p["resource_changes"].remove(_rc(p, SCORE_MEMBER)),
    "extra create": lambda p: p["resource_changes"].append(
        {**copy.deepcopy(_rc(p, SCORE_MEMBER)),
         "address": SCORE_MEMBER.replace("transaction_risk_scores", "customers")}),
    "mart rows differ from M8": lambda p: tl.drift_of(p, "customer_daily")["change"][
        "after"].update(num_rows=3411),
    "mart etag changed": lambda p: tl.drift_of(p, "customer_daily")["change"][
        "before"].update(etag="other"),
    "input rows differ": lambda p: tl.drift_of(p, "customers")["change"]["after"].update(
        num_rows=99),
    "mart schema drift": lambda p: tl.drift_of(p, "account_daily")["change"]["after"].update(
        schema="[changed]"),
    "existing table updated": lambda p: tl.rc_of(p, "account_daily")["change"].update(
        actions=["update"]),
    "dataset policy deleted": lambda p: _rc(p, bpc.DATASET_POLICY)["change"].update(
        actions=["delete"], after=None),
}


@pytest.mark.parametrize("name", BAD)
def test_adversarial_scores_plans_are_rejected(name):
    plan = scores_plan()
    BAD[name](plan)
    assert not check(plan)["ok"], name


def test_expectations_must_cover_inputs_and_marts():
    assert not check(scores_plan(), loaded=tl.LOADED)["ok"]
    broken = copy.deepcopy(LOADED)
    broken["tables"]["account_daily"]["num_rows"] = 0
    assert not check(scores_plan(), loaded=broken)["ok"]


def test_earlier_pilot_modes_are_unchanged():
    assert tl.check(tl.loaded_plan())["ok"]  # pilot-loaded on a pre-M8 plan still passes


# --------------------------------------------------------------------------- Terraform
def test_terraform_adds_the_score_table_with_existing_runner_role():
    main = (ROOT / "infrastructure/terraform/main.tf").read_text()
    assert ("score_contract = jsondecode(file(\"${path.module}/../../sql/scoring/v1/"
            "transaction_risk_scores.schema.json\"))") in main
    assert "{ (local.score_contract.table) = local.score_contract.fields }" in main
    module = json.loads((ROOT / "infrastructure/terraform/modules/bigquery/main.tf.json")
                        .read_text())
    member = module["resource"]["google_bigquery_table_iam_member"]["runner"]
    assert "transaction_risk_scores" not in member["role"]  # falls to var.table_role
    assert member["role"].endswith("var.reader_role : var.table_role}")
    roles = json.loads((ROOT / "infrastructure/iam/runtime_roles.json").read_text())
    assert roles["tables"]["permissions"] == ["bigquery.tables.get", "bigquery.tables.getData",
                                              "bigquery.tables.updateData"]
    table = module["resource"]["google_bigquery_table"]["products"]
    assert table["deletion_protection"] is True and table["lifecycle"]["prevent_destroy"]


def test_score_contract_has_no_label_or_outcome_columns():
    fields = json.loads((ROOT / "sql/scoring/v1/transaction_risk_scores.schema.json")
                        .read_text())["fields"]
    names = {f["name"] for f in fields}
    assert not any(re.search(r"label|target|fraud|suspicious|outcome|scenario|episode", n)
                   for n in names)
    assert bpc.score_schema_fields() == fields
