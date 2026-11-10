#!/usr/bin/env python3
"""M6Q offline checker for a saved bootstrap plan rendered as ``terraform show -json``.

Reads the plan JSON on stdin and prints a non-secret summary (counts, addresses, problems).
Standard library only; never contacts a cloud API, never reads state, writes nothing.

Modes:
  initial   Empty state: exactly the 10 durable resources plus the audit config are created
            (11 creates, provisioner access off, audit on). Superseded M6P inline checker.
  recovery  After the partial first apply (M6Q): the 10 durable resources already exist and
            must be unchanged (no-op); the only change is one create of the audit config on
            iam.googleapis.com with ADMIN_READ and DATA_READ.

Exit 0 when ok, 1 when the plan has problems, 2 when the input cannot be parsed.

Operator use (Terminal.app, offline; see docs/upgrade/m6q_validation.md):
  python3 scripts/terraform_custody.py run --root bootstrap -- show -json "$P" \\
    | python3 scripts/bootstrap_plan_check.py --mode recovery
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IAM = REPO / "infrastructure" / "iam"
PROJECT = "ebank-pilot-dev-261031-k7m4"
PREFIX, ENVIRONMENT = "bankpilot", "dev"
AUDIT_ADDRESS = "google_project_iam_audit_config.iamcredentials[0]"
AUDIT_SERVICE = "iam.googleapis.com"
AUDIT_LOG_TYPES = {"ADMIN_READ", "DATA_READ"}
EXPECTED_VARIABLES = {"project_id": PROJECT, "prefix": PREFIX, "environment": ENVIRONMENT,
                      "provisioner_access_enabled": False, "token_audit_logging_enabled": True}
FORBIDDEN_TEXT = ("roles/owner", "roles/editor", "allUsers", "allAuthenticatedUsers",
                  "allServices", "iamcredentials.googleapis.com",
                  "google_project_iam_policy", "google_project_iam_binding",
                  "google_service_account_key", "google_project_service",
                  "impersonate_service_account")


def expected_roles(iam: Path = IAM) -> dict[str, set[str]]:
    """role_id -> permissions, from the repository JSON the bootstrap root reads."""
    role_prefix = f"{PREFIX}_{ENVIRONMENT}"
    runtime = json.loads((iam / "runtime_roles.json").read_text())
    roles = {f"{role_prefix}_{k}": set(v["permissions"]) for k, v in runtime.items()}
    for fname, rid in (("provisioner", "provision"), ("impersonate", "impersonate")):
        data = json.loads((iam / f"{fname}.role.json").read_text())
        roles[f"{role_prefix}_{rid}"] = set(data["includedPermissions"])
    return roles


def durable_addresses(iam: Path = IAM) -> set[str]:
    """The 10 always-present bootstrap resources (created by the first, partial apply)."""
    runtime = json.loads((iam / "runtime_roles.json").read_text())
    return {
        "google_service_account.runner",
        "google_service_account.provisioner",
        *(f'google_project_iam_custom_role.runtime["{k}"]' for k in runtime),
        "google_project_iam_custom_role.provision",
        "google_project_iam_custom_role.impersonate",
        "google_project_iam_member.runner_jobs",
    }


def _check_values(rc: dict, roles: dict[str, set[str]], problems: list[str]) -> None:
    addr, kind = rc["address"], rc["type"]
    after = rc["change"].get("after") or {}
    if after.get("project") not in (None, PROJECT):
        problems.append(f"{addr}: other project")
    if kind == "google_service_account":
        if after.get("account_id") != f"{PREFIX}-{ENVIRONMENT}-{rc['name']}":
            problems.append(f"{addr}: unexpected account_id")
    elif kind == "google_project_iam_custom_role":
        if set(after.get("permissions") or []) != roles.get(after.get("role_id"), {None}):
            problems.append(f"{addr}: permissions differ from repository JSON")
    elif kind == "google_project_iam_member":
        if rc["name"] != "runner_jobs" or after.get("condition"):
            problems.append(f"{addr}: unexpected project member")
        role, member = after.get("role"), after.get("member")
        if role is not None and role != f"projects/{PROJECT}/roles/{PREFIX}_{ENVIRONMENT}_jobs":
            problems.append(f"{addr}: unexpected role")
        if member is not None and member != (
                f"serviceAccount:{PREFIX}-{ENVIRONMENT}-runner@{PROJECT}.iam.gserviceaccount.com"):
            problems.append(f"{addr}: unexpected member")
    elif kind == "google_project_iam_audit_config":
        if after.get("service") != AUDIT_SERVICE:
            problems.append(f"{addr}: audit service must be {AUDIT_SERVICE}")
        configs = after.get("audit_log_config") or []
        types = [c.get("log_type") for c in configs]
        if sorted(types) != sorted(AUDIT_LOG_TYPES):
            problems.append(f"{addr}: audit log types {sorted(map(str, types))} "
                            f"!= {sorted(AUDIT_LOG_TYPES)}")
        if any(c.get("exempted_members") for c in configs):
            problems.append(f"{addr}: exempted members present")
    else:
        problems.append(f"{addr}: unexpected resource type")


def check_plan(plan: dict, mode: str, iam: Path = IAM) -> dict:
    if mode not in ("initial", "recovery"):
        raise ValueError(f"unknown mode: {mode}")
    roles, durable = expected_roles(iam), durable_addresses(iam)
    want_create = durable | {AUDIT_ADDRESS} if mode == "initial" else {AUDIT_ADDRESS}
    want_noop = set() if mode == "initial" else durable
    problems: list[str] = []
    if plan.get("errored"):
        problems.append("plan errored")
    variables = plan.get("variables") or {}
    for name, value in EXPECTED_VARIABLES.items():
        if name not in variables or variables[name].get("value") != value:
            problems.append(f"variable {name} is not the approved value")
    for rc in plan.get("resource_drift") or []:
        problems.append(f"drift detected outside Terraform: {rc.get('address')}")
    counts: dict[str, int] = {}
    created, unchanged = set(), set()
    for rc in plan.get("resource_changes") or []:
        addr, actions = rc["address"], rc["change"]["actions"]
        if rc.get("mode", "managed") != "managed":
            problems.append(f"{addr}: unexpected data source")
            continue
        if actions == ["no-op"]:
            unchanged.add(addr)
        else:
            counts[rc["type"]] = counts.get(rc["type"], 0) + 1
            if actions == ["create"]:
                created.add(addr)
            else:
                problems.append(f"{addr}: {actions}")
        _check_values(rc, roles, problems)
    for addr in sorted(created - want_create):
        problems.append(f"{addr}: unexpected create")
    for addr in sorted(want_create - created):
        problems.append(f"{addr}: expected create missing")
    for addr in sorted(unchanged - want_noop):
        problems.append(f"{addr}: unexpected existing resource")
    for addr in sorted(want_noop - unchanged):
        problems.append(f"{addr}: expected unchanged existing resource missing")
    text = json.dumps(plan)
    for bad in FORBIDDEN_TEXT:
        if bad in text:
            problems.append(f"forbidden token present: {bad}")
    return {"mode": mode, "counts": counts, "creates": len(created),
            "create_addresses": sorted(created), "unchanged": len(unchanged),
            "problems": problems, "ok": not problems}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("initial", "recovery"), required=True)
    args = parser.parse_args(argv)
    try:
        plan = json.load(sys.stdin)
        if not isinstance(plan, dict):
            raise ValueError("plan JSON is not an object")
    except ValueError as exc:
        print(json.dumps({"ok": False, "problems": [f"invalid plan JSON: {exc}"]}))
        return 2
    report = check_plan(plan, args.mode)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
