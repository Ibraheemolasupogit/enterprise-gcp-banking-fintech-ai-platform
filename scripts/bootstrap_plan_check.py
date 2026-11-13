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

  M6 modes stay exactly as strict: any loader variable present must hold its disabled value,
  and the M7C1 loader identity (4 resources) may appear only unchanged, never in initial or
  recovery, and never with loader grants.

M7C1 loader modes (bootstrap root; provisioner access must stay disabled):
  loader-identity  The 11 bootstrap resources unchanged; exactly 4 creates: the keyless loader
                   account and its three custom roles (infrastructure/iam/loader_roles.json).
  loader-access    The 15 resources unchanged; exactly 6 expiring creates: loader jobs (project)
                   and objects (project, packet-prefix condition) grants, three input-table
                   members and operator token minting on the loader account. Requires
                   --packet-id=m7p-<24 hex> and --bucket=<pilot bucket> (never printed).
  loader-revoke    Loader access disabled; the 15 resources unchanged and the six grants
                   deleted (or already removed outside Terraform).

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
M6_BOOTSTRAP_MODES = ("initial", "recovery", "access", "revoke")
LOADER_MODES = ("loader-identity", "loader-access", "loader-revoke")
BOOTSTRAP_MODES = (*M6_BOOTSTRAP_MODES, *LOADER_MODES)
PILOT_MODES = ("pilot", "pilot-recovery")
MODES = (*BOOTSTRAP_MODES, *PILOT_MODES)
# ---- M7C1 dedicated loader ----
LOADER_EMAIL = f"{PREFIX}-{ENVIRONMENT}-loader@{PROJECT}.iam.gserviceaccount.com"
LOADER_MEMBER = f"serviceAccount:{LOADER_EMAIL}"
LOADER_DATASET = f"{PREFIX}_{ENVIRONMENT}"
LOADER_TABLES = ("accounts", "curated_transactions", "customers")
LOADER_ACCOUNT = "google_service_account.loader[0]"
LOADER_JOBS_GRANT = "google_project_iam_member.loader_jobs[0]"
LOADER_OBJECTS_GRANT = "google_project_iam_member.loader_objects[0]"
LOADER_TABLE_GRANTS = frozenset(f'google_bigquery_table_iam_member.loader["{t}"]'
                                for t in LOADER_TABLES)
LOADER_IMPERSONATION = "google_service_account_iam_member.operator_loader_impersonation[0]"
LOADER_GRANTS = frozenset({LOADER_JOBS_GRANT, LOADER_OBJECTS_GRANT, *LOADER_TABLE_GRANTS,
                           LOADER_IMPERSONATION})
LOADER_OFF = {"loader_access_enabled": False, "loader_access_expiry": None,
              "loader_bucket_name": None, "loader_object_prefix": None}
PACKET_ID_RE = re.compile(r"^m7p-[0-9a-f]{24}$")
BUCKET_RE = re.compile(r"^[a-z][a-z0-9-]{6,61}[a-z0-9]$")
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
    for key, data in loader_roles(iam).items():
        roles[f"{role_prefix}_{key}"] = set(data["permissions"])
    return roles


def loader_roles(iam: Path = IAM) -> dict:
    path = iam / "loader_roles.json"
    return json.loads(path.read_text()) if path.exists() else {}


def loader_identity_addresses(iam: Path = IAM) -> set[str]:
    """The M7C1 loader identity: keyless account + three custom roles (no grants)."""
    return {LOADER_ACCOUNT,
            *(f'google_project_iam_custom_role.loader["{k}"]' for k in loader_roles(iam))}


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


def loader_expiry_condition(expiry: str) -> dict:
    """The exact condition the bootstrap root renders on the loader grants (except objects)."""
    return {"title": f"loader-expires-{expiry.replace(':', '-')}",
            "description": "M7C1 time-bound loader access; revoke explicitly after use",
            "expression": f'request.time < timestamp("{expiry}")'}


def loader_object_condition(expiry: str, bucket: str, prefix: str) -> dict:
    """Expiry AND object type AND packet prefix in the approved bucket (all three required)."""
    return {"title": f"loader-objects-expires-{expiry.replace(':', '-')}",
            "description": "M7C1 time-bound packet-prefix object access; revoke explicitly "
                           "after use",
            "expression": (f'request.time < timestamp("{expiry}") && resource.type == '
                           f'"storage.googleapis.com/Object" && resource.name.startsWith('
                           f'"projects/_/buckets/{bucket}/objects/{prefix}")')}


def _loader_condition_shape_ok(values: dict, title_prefix: str) -> bool:
    """Used where the approved expiry is unknown (revocation): one expiring condition."""
    conditions = values.get("condition") or []
    return (len(conditions) == 1 and isinstance(conditions[0], dict)
            and str(conditions[0].get("title", "")).startswith(title_prefix)
            and str(conditions[0].get("expression", "")).startswith(
                'request.time < timestamp("'))


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
    elif kind == "google_project_iam_member" and name in ("loader_jobs", "loader_objects"):
        if values.get("role") != f"{ROLE_PREFIX}{name}":
            problems.append(f"{addr}: unexpected role")
        if values.get("member") != LOADER_MEMBER:
            problems.append(f"{addr}: member is not the loader service account")
        _check_loader_condition(addr, values, ctx, "objects" if name == "loader_objects"
                                else "expiry", problems)
    elif kind == "google_bigquery_table_iam_member" and name == "loader":
        key = re.fullmatch(r'google_bigquery_table_iam_member\.loader\["([a-z_]+)"\]', addr)
        table = key.group(1) if key else None
        if table not in LOADER_TABLES or values.get("table_id") not in (
                table, f"projects/{PROJECT}/datasets/{LOADER_DATASET}/tables/{table}"):
            problems.append(f"{addr}: not one of the three approved input tables")
        if values.get("dataset_id") != LOADER_DATASET:
            problems.append(f"{addr}: not in dataset {LOADER_DATASET}")
        if values.get("role") != f"{ROLE_PREFIX}loader_tables":
            problems.append(f"{addr}: unexpected role")
        if values.get("member") != LOADER_MEMBER:
            problems.append(f"{addr}: member is not the loader service account")
        _check_loader_condition(addr, values, ctx, "expiry", problems)
    elif kind == "google_service_account_iam_member" and name == "operator_loader_impersonation":
        if values.get("service_account_id") != (
                f"projects/{PROJECT}/serviceAccounts/{LOADER_EMAIL}"):
            problems.append(f"{addr}: grant is not on the loader service account")
        if values.get("role") != f"{ROLE_PREFIX}impersonate":
            problems.append(f"{addr}: unexpected role")
        member = values.get("member")
        if not OPERATOR_RE.match(str(member)) or (
                ctx.get("operator") is not None and member != ctx["operator"]):
            problems.append(f"{addr}: member is not the single named operator")
        _check_loader_condition(addr, values, ctx, "expiry", problems)
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


def _check_loader_condition(addr: str, values: dict, ctx: dict, kind: str,
                            problems: list[str]) -> None:
    expected = ctx.get(f"loader_{kind}_condition")
    if expected is not None:
        _check_condition(addr, values, expected, problems)
    elif not _loader_condition_shape_ok(
            values, "loader-objects-expires-" if kind == "objects" else "loader-expires-"):
        problems.append(f"{addr}: must carry exactly one expiring loader condition")


def _check_variables(variables: dict, expected: dict, problems: list[str]) -> None:
    for name, value in expected.items():
        if name not in variables or variables[name].get("value") != value:
            problems.append(f"variable {name} is not the approved value")


def _window(plan: dict, expiry: object, name: str, problems: list[str]) -> float | None:
    planned, end = _parse_utc(plan.get("timestamp")), _parse_utc(expiry)
    if planned is None:
        problems.append("plan timestamp missing or invalid")
    if planned is None or end is None:
        return None
    window = end - planned
    if not timedelta(0) < window <= MAX_WINDOW:
        problems.append(f"{name} must be after the plan timestamp and at most 8 h later")
    return round(window.total_seconds() / 3600, 2)


def _loader_context(plan: dict, variables: dict, problems: list[str], packet_id: object,
                    bucket: object) -> dict:
    """loader-access: approved expiry, bucket and packet prefix; nothing is printed but the
    packet ID and expiry. The bucket comes from the operator (--bucket), like --dataset-owner."""
    def value(name: str) -> object:
        return (variables.get(name) or {}).get("value")

    operator, expiry = value("operator_member"), value("loader_access_expiry")
    if not OPERATOR_RE.match(str(operator)):
        problems.append("variable operator_member is not a single user: principal")
    if not (isinstance(packet_id, str) and PACKET_ID_RE.match(packet_id)):
        problems.append("approved packet ID not supplied (--packet-id=m7p-<24 hex>)")
        packet_id = None
    if not (isinstance(bucket, str) and BUCKET_RE.match(bucket)):
        problems.append("approved bucket not supplied (--bucket)")
        bucket = None
    prefix = None if packet_id is None else f"m7/ingestion/{packet_id}/"
    if value("loader_object_prefix") != prefix or prefix is None:
        problems.append("variable loader_object_prefix is not the approved packet prefix")
    if value("loader_bucket_name") != bucket or bucket is None:
        problems.append("variable loader_bucket_name is not the approved bucket")
    if not (isinstance(expiry, str) and EXPIRY_RE.match(expiry)):
        problems.append("variable loader_access_expiry is not a UTC RFC 3339 timestamp")
        # Unmatchable sentinels: every grant's condition is then reported as wrong.
        return {"operator": operator, "loader_expiry_condition": {"title": None},
                "loader_objects_condition": {"title": None}, "expiry": None,
                "packet_id": packet_id}
    hours = _window(plan, expiry, "loader_access_expiry", problems)
    objects = (loader_object_condition(expiry, bucket, prefix) if bucket and prefix
               else {"title": None})
    return {"operator": operator, "loader_expiry_condition": loader_expiry_condition(expiry),
            "loader_objects_condition": objects, "expiry": expiry, "window_hours": hours,
            "packet_id": packet_id}


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
def _loader_grant_values_ok(name: str, title_prefix: str):
    def ok(side: dict) -> bool:
        return (side.get("role") == f"{ROLE_PREFIX}{name}" and side.get("member") == LOADER_MEMBER
                and side.get("project") == PROJECT and bool(side.get("id"))
                and _loader_condition_shape_ok(side, title_prefix))
    return ok


# M7C1: the two loader project members are created by one apply, so the first one's stored
# project-policy etag is stale at revocation. Same strict etag-only rule as the M6 exceptions.
ETAG_DRIFT_SPECS[LOADER_JOBS_GRANT] = (
    "google_project_iam_member", "loader_jobs", 0, RUNNER_GRANT_KEYS,
    _loader_grant_values_ok("loader_jobs", "loader-expires-"), "the approved loader jobs grant")
ETAG_DRIFT_SPECS[LOADER_OBJECTS_GRANT] = (
    "google_project_iam_member", "loader_objects", 0, RUNNER_GRANT_KEYS,
    _loader_grant_values_ok("loader_objects", "loader-objects-expires-"),
    "the approved loader objects grant")

# Which etag-only drift each mode tolerates. Every other mode tolerates none.
# M6T: access mode also tolerates the audit config, after a revoke apply rewrote the project
# policy (verified by operator inspection: only etag differed). Same strict validation.
ETAG_DRIFT_BY_MODE = {"access": frozenset({RUNNER_GRANT, AUDIT_ADDRESS}),
                      "revoke": frozenset({RUNNER_GRANT, AUDIT_ADDRESS})}
# M7C1, kept separate so the M6 table above is unchanged: the loader modes follow the M6T
# revoke, so they tolerate the same two; loader-revoke also tolerates the two loader project
# grants (and nothing else).
LOADER_ETAG_DRIFT_BY_MODE = {
    "loader-identity": frozenset({RUNNER_GRANT, AUDIT_ADDRESS}),
    "loader-access": frozenset({RUNNER_GRANT, AUDIT_ADDRESS}),
    "loader-revoke": frozenset({RUNNER_GRANT, AUDIT_ADDRESS, LOADER_JOBS_GRANT,
                                LOADER_OBJECTS_GRANT}),
}


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


def _check_bootstrap(plan: dict, mode: str, iam: Path, packet_id: object = None,
                     bucket: object = None) -> dict:
    roles, durable = expected_roles(iam), durable_addresses(iam)
    existing = durable | {AUDIT_ADDRESS}
    loader_identity = loader_identity_addresses(iam)
    problems: list[str] = []
    variables = plan.get("variables") or {}
    identity_on = (variables.get("loader_identity_enabled") or {}).get("value")
    if mode in LOADER_MODES:
        if identity_on is not True:
            problems.append("variable loader_identity_enabled must be true in loader modes")
    elif "loader_identity_enabled" in variables and identity_on is not False and not (
            mode in ("access", "revoke") and identity_on is True):
        problems.append("variable loader_identity_enabled is not allowed in this mode")
    # M6 access/revoke after M7D: the loader identity may exist, unchanged, without grants.
    m6_existing = existing | (loader_identity if identity_on is True else set())
    want_create, want_noop, want_delete = {
        "initial": (existing, set(), set()),
        "recovery": ({AUDIT_ADDRESS}, durable, set()),
        "access": (ACCESS_ADDRESSES, m6_existing, set()),
        "revoke": (set(), m6_existing, ACCESS_ADDRESSES),
        "loader-identity": (loader_identity, existing, set()),
        "loader-access": (LOADER_GRANTS, existing | loader_identity, set()),
        "loader-revoke": (set(), existing | loader_identity, LOADER_GRANTS),
    }[mode]
    expected_vars = dict(EXPECTED_VARIABLES, provisioner_access_enabled=(mode == "access"))
    _check_variables(variables, expected_vars, problems)
    ctx: dict = {}
    if mode == "access":
        ctx = _access_context(plan, variables, problems)
    elif "access_expiry" not in variables or variables["access_expiry"].get("value") is not None:
        problems.append("variable access_expiry must be null while access is disabled")
    if mode == "loader-access":
        _check_variables(variables, {"loader_access_enabled": True}, problems)
        ctx = _loader_context(plan, variables, problems, packet_id, bucket)
    elif mode in LOADER_MODES:
        _check_variables(variables, LOADER_OFF, problems)  # all present, all disabled
    else:
        for name, value in LOADER_OFF.items():  # pre-M7C1 plans omit them entirely
            if name in variables and variables[name].get("value") != value:
                problems.append(f"variable {name} is not the approved value")
    already_removed: set[str] = set()
    etag_drift: list[str] = []
    seen: set[str] = set()  # each tolerated address at most once; duplicates are rejected
    tolerated = ETAG_DRIFT_BY_MODE.get(mode) or LOADER_ETAG_DRIFT_BY_MODE.get(mode, frozenset())
    for rc in plan.get("resource_drift") or []:
        addr, actions = rc.get("address"), (rc.get("change") or {}).get("actions")
        if addr in seen:
            problems.append(f"drift detected outside Terraform: {addr} (duplicate)")
        elif ((mode == "revoke" and addr in ACCESS_ADDRESSES)
              or (mode == "loader-revoke" and addr in LOADER_GRANTS)) and actions == ["delete"]:
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
    if mode in ("revoke", "loader-revoke"):
        report.update(already_removed=sorted(already_removed),
                      accepted_etag_drift=sorted(etag_drift))
    if mode in ("loader-identity", "loader-access"):
        report["accepted_etag_drift"] = sorted(etag_drift)
    if mode == "loader-access":
        report.update(loader_access_expiry=ctx.get("expiry"),
                      window_hours=ctx.get("window_hours"), packet_id=ctx.get("packet_id"),
                      bucket_approved=bucket is not None and not any(
                          "bucket" in p for p in problems))
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
        # M6T: the table comes from the instance key; table_id must be that table, either as
        # configured (bare id, on create) or as the provider's refreshed fully qualified path.
        key = re.fullmatch(r'module\.bigquery\.google_bigquery_table_iam_member\.runner'
                           r'\["([a-z_]+)"\]', addr)
        table = key.group(1) if key else None
        approved_ids = {table, f"projects/{PROJECT}/datasets/{dataset}/tables/{table}"}
        if table not in pilot_tables() or after.get("table_id") not in approved_ids:
            problems.append(f"{addr}: table_id is not the approved table")
        if after.get("dataset_id") not in (None, dataset):
            problems.append(f"{addr}: table member is not in {dataset}")
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


# M6T: refresh-only metadata churn observed on the 17 resources created by the partial apply.
# address -> (type, attributes allowed to differ). Nothing else may differ; see below.
_TABLE_METADATA = frozenset({"etag", "last_modified_time", "resource_tags"})
PILOT_METADATA_DRIFT = {
    "module.bigquery.google_bigquery_dataset.foundation":
        ("google_bigquery_dataset", _TABLE_METADATA),
    "module.storage.google_storage_bucket.artifacts":
        ("google_storage_bucket", frozenset({"updated"})),
    **{f'module.bigquery.google_bigquery_table.products["{t}"]':
       ("google_bigquery_table", _TABLE_METADATA) for t in pilot_tables()},
}
CLOCK_SKEW = timedelta(minutes=5)


def _epoch_ms(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, UTC)


def _rfc3339(value: object) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})", value):
        return None
    return _parse_utc(value)


def _timestamp_moved_forward(name: str, before: object, after: object,
                             planned: datetime | None, parse) -> str | None:
    old, new = parse(before), parse(after)
    if old is None or new is None:
        return f"{name} is not a valid timestamp"
    if not old < new:
        return f"{name} did not move forward"
    if planned is None or new > planned + CLOCK_SKEW:
        return f"{name} is later than the plan timestamp"
    return None


def pilot_metadata_drift_problems(rc: dict, planned: datetime | None) -> list[str]:
    """M6T pilot-recovery only: accept refresh metadata churn on an existing pilot resource.

    Exact managed address/type, no index other than the table key, action exactly
    ``update``, identical attribute sets, no unknown values, and only these differences:
    BigQuery dataset/table ``etag`` (non-empty strings), ``last_modified_time`` (epoch ms,
    strictly increasing, not after the plan timestamp + 5 min) and ``resource_tags``
    (exactly null -> {}), with etag and last_modified_time changing together; bucket
    ``updated`` (RFC 3339, strictly increasing, same bound).
    Every other attribute (schema, labels, location, protection, access, IAM, ...) must be
    identical, so a security-relevant change cannot ride along with the metadata.
    """
    addr = rc.get("address")
    kind, allowed = PILOT_METADATA_DRIFT[addr]
    change = rc.get("change") or {}
    before, after = change.get("before"), change.get("after")
    if rc.get("mode", "managed") != "managed" or rc.get("type") != kind:
        return [f"drift detected outside Terraform: {addr}"]
    if change.get("actions") != ["update"]:
        return [f"drift detected outside Terraform: {addr} {change.get('actions')}"]
    if not isinstance(before, dict) or not isinstance(after, dict):
        return [f"{addr}: drift lacks before/after values"]
    if set(before) != set(after):
        return [f"{addr}: drift attribute sets differ"]
    if _has_unknown(change.get("after_unknown")):
        return [f"{addr}: drift has unknown values"]
    changed = {k for k in before if before[k] != after[k]}
    problems = []
    if not changed:
        problems.append(f"{addr}: drift without a change")
    if changed - allowed:
        problems.append(f"{addr}: drift changes non-metadata attributes: "
                        f"{sorted(changed - allowed)}")
    if "etag" in changed and not all(isinstance(after_or_before, str) and after_or_before
                                     for after_or_before in (before["etag"], after["etag"])):
        problems.append(f"{addr}: etag is not a non-empty string")
    if ("etag" in changed) != ("last_modified_time" in changed):
        # A BigQuery modification changes both; one without the other is unverified.
        problems.append(f"{addr}: etag and last_modified_time must change together")
    if "resource_tags" in changed and not (before["resource_tags"] is None
                                           and after["resource_tags"] == {}):
        problems.append(f"{addr}: resource_tags change is not null -> {{}}")
    for name, parse in (("last_modified_time", _epoch_ms), ("updated", _rfc3339)):
        if name in changed:
            issue = _timestamp_moved_forward(name, before[name], after[name], planned, parse)
            if issue:
                problems.append(f"{addr}: {issue}")
    return problems


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
    accepted_metadata_drift: list[str] = []
    seen: set[str] = set()
    planned = _parse_utc(plan.get("timestamp"))
    for rc in plan.get("resource_drift") or []:
        addr = rc.get("address")
        if mode == "pilot-recovery" and addr in PILOT_METADATA_DRIFT and addr not in seen:
            seen.add(addr)
            drift_problems = pilot_metadata_drift_problems(rc, planned)
            problems.extend(drift_problems)
            if not drift_problems:
                accepted_metadata_drift.append(addr)
        else:
            problems.append(f"drift detected outside Terraform: {addr}")
    report = _classify(plan, problems, lambda rc: _check_pilot_values(rc, problems, owner))
    if mode == "pilot":
        _compare(report, pilot_addresses(), set(), set(), problems)
    else:
        _compare(report, {DATASET_POLICY}, pilot_addresses() - {DATASET_POLICY}, set(),
                 problems)
    # prior_state legitimately holds the identity module's project-policy data source (the
    # Owner binding), so the pilot scan covers everything the plan would change or configure.
    _forbid(plan, PILOT_FORBIDDEN, problems, PILOT_SCAN_KEYS)
    if mode == "pilot-recovery":
        report["accepted_metadata_drift"] = sorted(accepted_metadata_drift)
    report.update(mode=mode, dataset_owner_approved=not dataset_owner_problems(owner),
                  problems=problems, ok=not problems)
    return report


def check_plan(plan: dict, mode: str, iam: Path = IAM, dataset_owner: object = None,
               packet_id: object = None, bucket: object = None) -> dict:
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    if mode in PILOT_MODES:
        return _check_pilot(plan, mode, dataset_owner)
    return _check_bootstrap(plan, mode, iam, packet_id, bucket)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--dataset-owner", default=None,
                        help="pilot modes: the approved user: dataset owner (never printed)")
    parser.add_argument("--packet-id", default=None,
                        help="loader-access: the approved M7B packet ID (m7p-<24 hex>)")
    parser.add_argument("--bucket", default=None,
                        help="loader-access: the approved pilot bucket (never printed)")
    args = parser.parse_args(argv)
    try:
        plan = json.load(sys.stdin)
        if not isinstance(plan, dict):
            raise ValueError("plan JSON is not an object")
    except ValueError as exc:
        print(json.dumps({"ok": False, "problems": [f"invalid plan JSON: {exc}"]}))
        return 2
    report = check_plan(plan, args.mode, dataset_owner=args.dataset_owner,
                        packet_id=args.packet_id, bucket=args.bucket)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
