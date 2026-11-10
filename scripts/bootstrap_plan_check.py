#!/usr/bin/env python3
"""M6Q/M6S offline checker for saved plans rendered as ``terraform show -json``.

Reads the plan JSON on stdin and prints a non-secret summary (counts, addresses, problems).
Standard library only; never contacts a cloud API, never reads state, writes nothing. The
operator principal and bucket name are never printed.

Bootstrap root modes:
  initial   Empty state: the 10 durable resources plus the audit config are created
            (11 creates, provisioner access off, audit on).
  recovery  After the partial first apply (M6Q): the 10 durable resources are unchanged and
            the only change is one create of the iam.googleapis.com audit config.
  access    M6S access window: the 11 bootstrap resources are unchanged and exactly the two
            conditional grants are created (provisioner project role; operator token minting
            on the provisioner account), both expiring at access_expiry, which must be after
            the plan timestamp and at most 8 hours later.
  revoke    M6S revocation: access disabled; the 11 bootstrap resources are unchanged and the
            two conditional grants are deleted (or were already removed outside Terraform).

Pilot root mode:
  pilot     M6S first pilot deployment: exactly 18 creates (bucket, dataset, 7 tables, two
            resource-level policies and 7 table members), all bound to the runner only.

Exit 0 when ok, 1 when the plan has problems, 2 when the input cannot be parsed.

Operator use (Terminal.app, offline; see docs/upgrade/m6s_validation.md):
  python3 scripts/terraform_custody.py run --root bootstrap -- show -json "$P" \\
    | python3 scripts/bootstrap_plan_check.py --mode access
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IAM = REPO / "infrastructure" / "iam"
SCHEMAS = (REPO / "infrastructure" / "schemas" / "v1" / "source_tables.json",
           REPO / "sql" / "analytics" / "v1" / "bigquery.schemas.json")
PROJECT = "ebank-pilot-dev-261031-k7m4"
PREFIX, ENVIRONMENT, REGION = "bankpilot", "dev", "europe-west2"
ROLE_PREFIX = f"projects/{PROJECT}/roles/{PREFIX}_{ENVIRONMENT}_"
RUNNER_EMAIL = f"{PREFIX}-{ENVIRONMENT}-runner@{PROJECT}.iam.gserviceaccount.com"
PROVISIONER_EMAIL = f"{PREFIX}-{ENVIRONMENT}-provisioner@{PROJECT}.iam.gserviceaccount.com"
AUDIT_ADDRESS = "google_project_iam_audit_config.iamcredentials[0]"
AUDIT_SERVICE = "iam.googleapis.com"
AUDIT_LOG_TYPES = {"ADMIN_READ", "DATA_READ"}
PROVISIONER_GRANT = "google_project_iam_member.provisioner[0]"
IMPERSONATION_GRANT = "google_service_account_iam_member.operator_impersonation[0]"
ACCESS_ADDRESSES = frozenset({PROVISIONER_GRANT, IMPERSONATION_GRANT})
MAX_WINDOW = timedelta(hours=8)
EXPIRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
OPERATOR_RE = re.compile(r"^user:[^@\s]+@[^@\s]+\.[^@\s]+$")
EXPECTED_VARIABLES = {"project_id": PROJECT, "prefix": PREFIX, "environment": ENVIRONMENT,
                      "provisioner_access_enabled": False, "token_audit_logging_enabled": True}
PILOT_VARIABLES = {"project_id": PROJECT, "prefix": PREFIX, "environment": ENVIRONMENT,
                   "region": REGION, "provisioner_service_account": PROVISIONER_EMAIL}
COMMON_FORBIDDEN = ("roles/owner", "roles/editor", "allUsers", "allAuthenticatedUsers",
                    "roles/iam.serviceAccount", "google_service_account_key",
                    "google_project_service", "google_project_iam_binding")
FORBIDDEN_TEXT = (*COMMON_FORBIDDEN, "allServices", "iamcredentials.googleapis.com",
                  "google_project_iam_policy", "impersonate_service_account")
PILOT_FORBIDDEN = (*COMMON_FORBIDDEN, "google_project_iam_member",
                   "google_project_iam_audit_config", "google_service_account_iam")
PILOT_SCAN_KEYS = ("variables", "resource_changes", "resource_drift", "output_changes",
                   "configuration")
READER_TABLES = {"curated_transactions", "customers", "accounts"}
BOOTSTRAP_MODES = ("initial", "recovery", "access", "revoke")
MODES = (*BOOTSTRAP_MODES, "pilot")


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


def pilot_tables() -> list[str]:
    source = json.loads(SCHEMAS[0].read_text())
    marts = json.loads(SCHEMAS[1].read_text())["tables"]
    return sorted(set(source) | set(marts))


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def expiry_condition(expiry: str) -> dict:
    """The exact IAM condition the bootstrap root renders for an access window."""
    return {"title": f"expires-{expiry.replace(':', '-')}",
            "description": "M6K time-bound access; remove explicitly after expiry",
            "expression": f'request.time < timestamp("{expiry}")'}


def _check_condition(addr: str, values: dict, expected: dict | None,
                     problems: list[str]) -> None:
    conditions = values.get("condition") or []
    if len(conditions) != 1:
        problems.append(f"{addr}: must carry exactly one expiring condition")
        return
    cond = {k: conditions[0].get(k) for k in ("title", "description", "expression")}
    if expected is not None and cond != expected:
        problems.append(f"{addr}: condition differs from the approved expiry")
    elif expected is None and not str(cond["expression"] or "").startswith(
            'request.time < timestamp("'):
        problems.append(f"{addr}: condition is not an expiry")


def _check_values(rc: dict, roles: dict[str, set[str]], problems: list[str],
                  ctx: dict) -> None:
    addr, kind, name = rc["address"], rc["type"], rc["name"]
    actions = rc["change"]["actions"]
    values = (rc["change"].get("before") if actions == ["delete"]
              else rc["change"].get("after")) or {}
    if values.get("project") not in (None, PROJECT):
        problems.append(f"{addr}: other project")
    if kind == "google_service_account":
        if values.get("account_id") != f"{PREFIX}-{ENVIRONMENT}-{name}":
            problems.append(f"{addr}: unexpected account_id")
    elif kind == "google_project_iam_custom_role":
        if set(values.get("permissions") or []) != roles.get(values.get("role_id"), {None}):
            problems.append(f"{addr}: permissions differ from repository JSON")
    elif kind == "google_project_iam_member" and name == "runner_jobs":
        if values.get("condition"):
            problems.append(f"{addr}: unexpected project member")
        role, member = values.get("role"), values.get("member")
        if role is not None and role != f"{ROLE_PREFIX}jobs":
            problems.append(f"{addr}: unexpected role")
        if member is not None and member != f"serviceAccount:{RUNNER_EMAIL}":
            problems.append(f"{addr}: unexpected member")
    elif kind == "google_project_iam_member" and name == "provisioner":
        if values.get("role") != f"{ROLE_PREFIX}provision":
            problems.append(f"{addr}: unexpected role")
        if values.get("member") != f"serviceAccount:{PROVISIONER_EMAIL}":
            problems.append(f"{addr}: unexpected member")
        _check_condition(addr, values, ctx.get("condition"), problems)
    elif kind == "google_service_account_iam_member" and name == "operator_impersonation":
        if values.get("service_account_id") != (
                f"projects/{PROJECT}/serviceAccounts/{PROVISIONER_EMAIL}"):
            problems.append(f"{addr}: grant is not on the provisioner service account")
        if values.get("role") != f"{ROLE_PREFIX}impersonate":
            problems.append(f"{addr}: unexpected role")
        member = values.get("member")
        if not OPERATOR_RE.match(str(member)) or (
                ctx.get("operator") is not None and member != ctx["operator"]):
            problems.append(f"{addr}: member is not the single named operator")
        _check_condition(addr, values, ctx.get("condition"), problems)
    elif kind == "google_project_iam_audit_config":
        if values.get("service") != AUDIT_SERVICE:
            problems.append(f"{addr}: audit service must be {AUDIT_SERVICE}")
        configs = values.get("audit_log_config") or []
        types = [c.get("log_type") for c in configs]
        if sorted(types) != sorted(AUDIT_LOG_TYPES):
            problems.append(f"{addr}: audit log types {sorted(map(str, types))} "
                            f"!= {sorted(AUDIT_LOG_TYPES)}")
        if any(c.get("exempted_members") for c in configs):
            problems.append(f"{addr}: exempted members present")
    else:
        problems.append(f"{addr}: unexpected resource type")


def _check_variables(variables: dict, expected: dict, problems: list[str]) -> None:
    for name, value in expected.items():
        if name not in variables or variables[name].get("value") != value:
            problems.append(f"variable {name} is not the approved value")


def _access_context(plan: dict, variables: dict, problems: list[str]) -> dict:
    expiry = (variables.get("access_expiry") or {}).get("value")
    operator = (variables.get("operator_member") or {}).get("value")
    if not OPERATOR_RE.match(str(operator)):
        problems.append("variable operator_member is not a single user: principal")
    planned = _parse_utc(plan.get("timestamp"))
    if planned is None:
        problems.append("plan timestamp missing or invalid")
    if not (isinstance(expiry, str) and EXPIRY_RE.match(expiry)):
        problems.append("variable access_expiry is not a UTC RFC 3339 timestamp")
        return {"operator": operator, "condition": None, "expiry": None}
    end = _parse_utc(expiry)
    window = None
    if planned is not None and end is not None:
        window = end - planned
        if not timedelta(0) < window <= MAX_WINDOW:
            problems.append("access_expiry must be after the plan timestamp and at most 8 h later")
    return {"operator": operator, "condition": expiry_condition(expiry), "expiry": expiry,
            "window_hours": None if window is None else round(window.total_seconds() / 3600, 2)}


def _check_bootstrap(plan: dict, mode: str, iam: Path) -> dict:
    roles, durable = expected_roles(iam), durable_addresses(iam)
    existing = durable | {AUDIT_ADDRESS}
    want_create, want_noop, want_delete = {
        "initial": (existing, set(), set()),
        "recovery": ({AUDIT_ADDRESS}, durable, set()),
        "access": (ACCESS_ADDRESSES, existing, set()),
        "revoke": (set(), existing, ACCESS_ADDRESSES),
    }[mode]
    problems: list[str] = []
    variables = plan.get("variables") or {}
    expected_vars = dict(EXPECTED_VARIABLES, provisioner_access_enabled=(mode == "access"))
    _check_variables(variables, expected_vars, problems)
    ctx: dict = {}
    if mode == "access":
        ctx = _access_context(plan, variables, problems)
    elif "access_expiry" not in variables or variables["access_expiry"].get("value") is not None:
        problems.append("variable access_expiry must be null while access is disabled")
    already_removed: set[str] = set()
    for rc in plan.get("resource_drift") or []:
        addr, actions = rc.get("address"), (rc.get("change") or {}).get("actions")
        if mode == "revoke" and addr in ACCESS_ADDRESSES and actions == ["delete"]:
            already_removed.add(addr)
        else:
            problems.append(f"drift detected outside Terraform: {addr}")
    want_delete = want_delete - already_removed  # never mutate the module constants
    report = _classify(plan, problems, lambda rc: _check_values(rc, roles, problems, ctx))
    _compare(report, want_create, want_noop, want_delete, problems)
    _forbid(plan, FORBIDDEN_TEXT, problems)
    report.update(mode=mode, problems=problems, ok=not problems)
    if mode == "access":
        report.update(access_expiry=ctx.get("expiry"), window_hours=ctx.get("window_hours"))
    if mode == "revoke":
        report["already_removed"] = sorted(already_removed)
    return report


def _classify(plan: dict, problems: list[str], check) -> dict:
    if plan.get("errored"):
        problems.append("plan errored")
    counts: dict[str, int] = {}
    created, unchanged, deleted = set(), set(), set()
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
            elif actions == ["delete"]:
                deleted.add(addr)
            else:
                problems.append(f"{addr}: {actions}")
        check(rc)
    return {"counts": counts, "creates": len(created), "create_addresses": sorted(created),
            "unchanged": len(unchanged), "deletes": len(deleted),
            "delete_addresses": sorted(deleted), "_sets": (created, unchanged, deleted)}


def _compare(report: dict, want_create: set, want_noop: set, want_delete: set,
             problems: list[str]) -> None:
    created, unchanged, deleted = report.pop("_sets")
    for got, want, label in ((created, want_create, "create"),
                             (unchanged, want_noop, "unchanged existing resource"),
                             (deleted, want_delete, "delete")):
        for addr in sorted(got - want):
            problems.append(f"{addr}: unexpected {label}")
        for addr in sorted(want - got):
            problems.append(f"{addr}: expected {label} missing")


def _forbid(plan: dict, words: tuple[str, ...], problems: list[str],
            keys: tuple[str, ...] | None = None) -> None:
    text = json.dumps(plan if keys is None else {k: plan.get(k) for k in keys})
    for bad in words:
        if bad in text:
            problems.append(f"forbidden token present: {bad}")


def _policy_is_runner_only(policy_data: object, role: str) -> bool:
    try:
        policy = json.loads(policy_data) if isinstance(policy_data, str) else None
    except ValueError:
        return False
    if not isinstance(policy, dict) or set(policy) - {"bindings"}:
        return False
    return policy.get("bindings") == [{"role": role,
                                       "members": [f"serviceAccount:{RUNNER_EMAIL}"]}]


def _check_pilot_values(rc: dict, problems: list[str]) -> None:
    addr, kind, after = rc["address"], rc["type"], rc["change"].get("after") or {}
    dataset = f"{PREFIX}_{ENVIRONMENT}"
    if after.get("project") not in (None, PROJECT):
        problems.append(f"{addr}: other project")
    if kind == "google_storage_bucket":
        if str(after.get("location", "")).lower() != REGION:
            problems.append(f"{addr}: bucket outside {REGION}")
        if after.get("public_access_prevention") != "enforced" or \
                after.get("uniform_bucket_level_access") is not True:
            problems.append(f"{addr}: bucket is not private with uniform access")
        if after.get("force_destroy") is not False or \
                after.get("versioning") != [{"enabled": True}]:
            problems.append(f"{addr}: bucket protection settings differ")
    elif kind == "google_storage_bucket_iam_policy":
        if not _policy_is_runner_only(after.get("policy_data"), f"{ROLE_PREFIX}objects"):
            problems.append(f"{addr}: bucket policy must bind only the runner to the objects role")
    elif kind == "google_bigquery_dataset":
        if str(after.get("location", "")).lower() != REGION or after.get("dataset_id") != dataset:
            problems.append(f"{addr}: dataset name or location differs")
        if after.get("delete_contents_on_destroy") is not False:
            problems.append(f"{addr}: dataset delete_contents_on_destroy must be false")
    elif kind == "google_bigquery_dataset_iam_policy":
        if not _policy_is_runner_only(after.get("policy_data"), f"{ROLE_PREFIX}metadata"):
            problems.append(f"{addr}: dataset policy must bind only the runner "
                            "to the metadata role")
    elif kind == "google_bigquery_table":
        if after.get("deletion_protection") is not True or after.get("dataset_id") != dataset:
            problems.append(f"{addr}: table must be deletion-protected in {dataset}")
    elif kind == "google_bigquery_table_iam_member":
        table = after.get("table_id")
        role = f"{ROLE_PREFIX}{'readers' if table in READER_TABLES else 'tables'}"
        if after.get("member") != f"serviceAccount:{RUNNER_EMAIL}" or \
                after.get("role") != role or after.get("condition"):
            problems.append(f"{addr}: table member must be the runner with its approved role")
    else:
        problems.append(f"{addr}: unexpected resource type")


def pilot_addresses() -> set[str]:
    tables = pilot_tables()
    return {
        "module.storage.google_storage_bucket.artifacts",
        "module.storage.google_storage_bucket_iam_policy.runner",
        "module.bigquery.google_bigquery_dataset.foundation",
        "module.bigquery.google_bigquery_dataset_iam_policy.metadata",
        *(f'module.bigquery.google_bigquery_table.products["{t}"]' for t in tables),
        *(f'module.bigquery.google_bigquery_table_iam_member.runner["{t}"]' for t in tables),
    }


def _check_pilot(plan: dict) -> dict:
    problems: list[str] = []
    variables = plan.get("variables") or {}
    _check_variables(variables, PILOT_VARIABLES, problems)
    if not (variables.get("bucket_name") or {}).get("value"):
        problems.append("variable bucket_name is missing")
    for rc in plan.get("resource_drift") or []:
        problems.append(f"drift detected outside Terraform: {rc.get('address')}")
    report = _classify(plan, problems, lambda rc: _check_pilot_values(rc, problems))
    _compare(report, pilot_addresses(), set(), set(), problems)
    # prior_state legitimately holds the identity module's project-policy data source (the
    # Owner binding), so the pilot scan covers everything the plan would change or configure.
    _forbid(plan, PILOT_FORBIDDEN, problems, PILOT_SCAN_KEYS)
    report.update(mode="pilot", problems=problems, ok=not problems)
    return report


def check_plan(plan: dict, mode: str, iam: Path = IAM) -> dict:
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    return _check_pilot(plan) if mode == "pilot" else _check_bootstrap(plan, mode, iam)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, required=True)
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
