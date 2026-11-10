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

Pilot root modes (both require --dataset-owner=user:<approved owner>; M6T):
  pilot           First pilot deployment: exactly 18 creates (bucket, dataset, 7 tables, two
                  resource-level policies and 7 table members). The bucket and table grants
                  bind only the runner; the authoritative dataset policy binds exactly the
                  approved owner (roles/bigquery.dataOwner) and the runner metadata role.
  pilot-recovery  After the M6S partial apply (17/18): the 17 created resources unchanged and
                  exactly one create of the dataset policy, with the same content rule.

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
PILOT_MODES = ("pilot", "pilot-recovery")
MODES = (*BOOTSTRAP_MODES, *PILOT_MODES)
DATASET_POLICY = "module.bigquery.google_bigquery_dataset_iam_policy.metadata"
DATASET_OWNER_ROLE = "roles/bigquery.dataOwner"
# Mirrors the pilot dataset_owner_member validation in infrastructure/terraform/variables.tf.
DATASET_OWNER_RE = re.compile(r"^user:[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


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


RUNNER_GRANT = "google_project_iam_member.runner_jobs"
RUNNER_GRANT_KEYS = frozenset({"condition", "etag", "id", "member", "project", "role"})
AUDIT_CONFIG_KEYS = frozenset({"audit_log_config", "etag", "id", "project", "service"})
AUDIT_LOG_CONFIG_KEYS = frozenset({"exempted_members", "log_type"})


def _has_unknown(value: object) -> bool:
    """True if any leaf of a Terraform ``after_unknown`` structure is ``true``."""
    if isinstance(value, dict):
        return any(_has_unknown(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_unknown(v) for v in value)
    return value is True


def _runner_values_ok(side: dict) -> bool:
    return (side.get("role") == f"{ROLE_PREFIX}jobs"
            and side.get("member") == f"serviceAccount:{RUNNER_EMAIL}"
            and side.get("project") == PROJECT and not side.get("condition")
            and bool(side.get("id")))


def _audit_values_ok(side: dict) -> bool:
    configs = side.get("audit_log_config")
    if not isinstance(configs, list) or not all(
            isinstance(c, dict) and set(c) == AUDIT_LOG_CONFIG_KEYS for c in configs):
        return False
    return (side.get("service") == AUDIT_SERVICE and side.get("project") == PROJECT
            and bool(side.get("id"))
            and sorted(c["log_type"] for c in configs) == sorted(AUDIT_LOG_TYPES)
            and all(c["exempted_members"] in ([], None) for c in configs))


# address -> (type, name, index, exact attribute set, approved-values predicate, label)
ETAG_DRIFT_SPECS = {
    RUNNER_GRANT: ("google_project_iam_member", "runner_jobs", None, RUNNER_GRANT_KEYS,
                   _runner_values_ok, "the approved runner grant"),
    AUDIT_ADDRESS: ("google_project_iam_audit_config", "iamcredentials", 0, AUDIT_CONFIG_KEYS,
                    _audit_values_ok, "the approved iam.googleapis.com audit config"),
}
# Which etag-only drift each mode tolerates. Every other mode tolerates none.
# M6T: access mode also tolerates the audit config, after a revoke apply rewrote the project
# policy (verified by operator inspection: only etag differed). Same strict validation.
ETAG_DRIFT_BY_MODE = {"access": frozenset({RUNNER_GRANT, AUDIT_ADDRESS}),
                      "revoke": frozenset({RUNNER_GRANT, AUDIT_ADDRESS})}


def etag_drift_problems(rc: dict, address: str) -> list[str]:
    """M6S exceptions: refresh reports ``etag`` changed on a project-policy-backed resource
    because the project IAM policy (whose etag these resources store) was rewritten by
    another change. Accepted only if it is exactly an ``update`` of ``address`` in which every
    attribute other than ``etag`` is present, identical before and after, and equal to the
    approved configuration, and both etags are known, non-empty and different.
    """
    kind, name, index, keys, values_ok, label = ETAG_DRIFT_SPECS[address]
    addr = rc.get("address")
    change = rc.get("change") or {}
    before, after = change.get("before"), change.get("after")
    if (addr != address or rc.get("mode", "managed") != "managed"
            or rc.get("type") != kind or rc.get("name") != name
            or rc.get("index") != index):
        return [f"drift detected outside Terraform: {addr}"]
    if change.get("actions") != ["update"]:
        return [f"drift detected outside Terraform: {addr} {change.get('actions')}"]
    if not isinstance(before, dict) or not isinstance(after, dict):
        return [f"{addr}: drift lacks before/after values"]
    problems = []
    if set(before) != keys or set(after) != keys:
        problems.append(f"{addr}: drift attributes are not exactly {sorted(keys)}")
    changed = sorted(k for k in set(before) | set(after)
                     if k != "etag" and before.get(k) != after.get(k))
    if changed:
        problems.append(f"{addr}: drift changes more than etag: {changed}")
    if _has_unknown(change.get("after_unknown")):
        problems.append(f"{addr}: drift has unknown values")
    if not (values_ok(before) and values_ok(after)):
        problems.append(f"{addr}: drift values are not {label}")
    etags = (before.get("etag"), after.get("etag"))
    if not all(isinstance(e, str) and e for e in etags) or etags[0] == etags[1]:
        problems.append(f"{addr}: drift is not an etag change")
    return problems


def runner_etag_drift_problems(rc: dict) -> list[str]:
    """The access-mode exception (runner grant only); see ``etag_drift_problems``."""
    return etag_drift_problems(rc, RUNNER_GRANT)


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
    etag_drift: list[str] = []
    seen: set[str] = set()  # each tolerated address at most once; duplicates are rejected
    tolerated = ETAG_DRIFT_BY_MODE.get(mode, frozenset())
    for rc in plan.get("resource_drift") or []:
        addr, actions = rc.get("address"), (rc.get("change") or {}).get("actions")
        if addr in seen:
            problems.append(f"drift detected outside Terraform: {addr} (duplicate)")
        elif mode == "revoke" and addr in ACCESS_ADDRESSES and actions == ["delete"]:
            seen.add(addr)
            already_removed.add(addr)
        elif addr in tolerated:
            seen.add(addr)
            drift_problems = etag_drift_problems(rc, addr)
            problems.extend(drift_problems)
            if not drift_problems:
                etag_drift.append(addr)
        else:
            problems.append(f"drift detected outside Terraform: {addr}")
    want_delete = want_delete - already_removed  # never mutate the module constants
    report = _classify(plan, problems, lambda rc: _check_values(rc, roles, problems, ctx))
    _compare(report, want_create, want_noop, want_delete, problems)
    _forbid(plan, FORBIDDEN_TEXT, problems)
    report.update(mode=mode, problems=problems, ok=not problems)
    if mode == "access":
        report.update(access_expiry=ctx.get("expiry"), window_hours=ctx.get("window_hours"),
                      accepted_etag_drift=sorted(etag_drift))
    if mode == "revoke":
        report.update(already_removed=sorted(already_removed),
                      accepted_etag_drift=sorted(etag_drift))
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


def dataset_owner_problems(owner: object) -> list[str]:
    """M6T: the dataset owner must be an explicitly approved single human ``user:``.

    BigQuery rejects a dataset policy without a directly assigned owner (HTTP 400 "No owners
    specified"). The owner is an operator decision: never defaulted, never the runner or
    provisioner (or any service account), never a group, domain, special group or public.
    """
    if owner is None:
        return ["dataset owner not supplied (operator approval required: --dataset-owner)"]
    if not DATASET_OWNER_RE.match(str(owner)) or str(owner).lower().endswith(
            ".gserviceaccount.com"):
        return ["dataset owner must be a single approved user: principal"]
    return []


def _dataset_policy_ok(policy_data: object, owner: object) -> bool:
    """Exactly two unconditional bindings: runner -> metadata role; approved owner -> owner."""
    try:
        policy = json.loads(policy_data) if isinstance(policy_data, str) else None
    except ValueError:
        return False
    if not isinstance(policy, dict) or set(policy) != {"bindings"} or dataset_owner_problems(
            owner):
        return False
    bindings = policy["bindings"]
    # Exact equality below also rejects any extra key such as a binding condition.
    if not isinstance(bindings, list) or not all(isinstance(b, dict) for b in bindings):
        return False
    want = [{"role": DATASET_OWNER_ROLE, "members": [owner]},
            {"role": f"{ROLE_PREFIX}metadata", "members": [f"serviceAccount:{RUNNER_EMAIL}"]}]
    by_role = lambda b: str(b["role"])  # noqa: E731
    return sorted(bindings, key=by_role) == sorted(want, key=by_role)


def _policy_is_runner_only(policy_data: object, role: str) -> bool:
    try:
        policy = json.loads(policy_data) if isinstance(policy_data, str) else None
    except ValueError:
        return False
    if not isinstance(policy, dict) or set(policy) - {"bindings"}:
        return False
    return policy.get("bindings") == [{"role": role,
                                       "members": [f"serviceAccount:{RUNNER_EMAIL}"]}]


def _check_pilot_values(rc: dict, problems: list[str], owner: object = None) -> None:
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
        # M6T: authoritative, so it must keep a direct owner (BigQuery HTTP 400 otherwise)
        # and must contain nothing else: no creator/provisioner OWNER, no special groups.
        if not _dataset_policy_ok(after.get("policy_data"), owner):
            problems.append(f"{addr}: dataset policy must be exactly the approved owner "
                            "(roles/bigquery.dataOwner) and the runner metadata role")
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


def _check_pilot(plan: dict, mode: str = "pilot", owner: object = None) -> dict:
    """``pilot``: 18 creates. ``pilot-recovery`` (M6T): after the partial apply, the 17
    created resources are unchanged and the only change is one create of the dataset policy."""
    problems: list[str] = list(dataset_owner_problems(owner))
    variables = plan.get("variables") or {}
    _check_variables(variables, PILOT_VARIABLES, problems)
    if not (variables.get("bucket_name") or {}).get("value"):
        problems.append("variable bucket_name is missing")
    if (variables.get("dataset_owner_member") or {}).get("value") != owner:
        problems.append("variable dataset_owner_member is not the approved dataset owner")
    for rc in plan.get("resource_drift") or []:
        problems.append(f"drift detected outside Terraform: {rc.get('address')}")
    report = _classify(plan, problems, lambda rc: _check_pilot_values(rc, problems, owner))
    if mode == "pilot":
        _compare(report, pilot_addresses(), set(), set(), problems)
    else:
        _compare(report, {DATASET_POLICY}, pilot_addresses() - {DATASET_POLICY}, set(),
                 problems)
    # prior_state legitimately holds the identity module's project-policy data source (the
    # Owner binding), so the pilot scan covers everything the plan would change or configure.
    _forbid(plan, PILOT_FORBIDDEN, problems, PILOT_SCAN_KEYS)
    report.update(mode=mode, dataset_owner_approved=not dataset_owner_problems(owner),
                  problems=problems, ok=not problems)
    return report


def check_plan(plan: dict, mode: str, iam: Path = IAM, dataset_owner: object = None) -> dict:
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    if mode in PILOT_MODES:
        return _check_pilot(plan, mode, dataset_owner)
    return _check_bootstrap(plan, mode, iam)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--dataset-owner", default=None,
                        help="pilot modes: the approved user: dataset owner (never printed)")
    args = parser.parse_args(argv)
    try:
        plan = json.load(sys.stdin)
        if not isinstance(plan, dict):
            raise ValueError("plan JSON is not an object")
    except ValueError as exc:
        print(json.dumps({"ok": False, "problems": [f"invalid plan JSON: {exc}"]}))
        return 2
    report = check_plan(plan, args.mode, dataset_owner=args.dataset_owner)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
