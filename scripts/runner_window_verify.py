#!/usr/bin/env python3
"""M8 runner window: independent verification (second check beside bootstrap_plan_check.py).

Written separately from the repository checker and never imports it: every expected value is
re-derived here from infrastructure/terraform/bootstrap/main.tf and infrastructure/iam/*.json.
Standard library only; offline; prints counts and verdicts only (the operator is redacted).

  plan  --mode access|revoke --plan-json J [--expiry EXP] --bootstrap-tfvars T
        The saved bootstrap plan: exactly one create (access) or delete (revoke) of the
        operator's token-minting grant on the runner account; the 15 other resources no-op.
  iam   --phase window|revoked --capture DIR [--expiry EXP] --bootstrap-tfvars T
        A read-only capture by scripts/runner_window_collect.sh: the runner account policy,
        the runner's own grants (unchanged), the loader/provisioner accounts (no grants), and
        a live token-mint attempt (window: succeeds; revoked: PERMISSION_DENIED).

Exit 0 = everything exactly as approved; 1 = problems; 2 = unreadable input.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROJECT = "ebank-pilot-dev-261031-k7m4"
ROLES = f"projects/{PROJECT}/roles/bankpilot_dev_"
RUNNER = f"bankpilot-dev-runner@{PROJECT}.iam.gserviceaccount.com"
LOADER = f"bankpilot-dev-loader@{PROJECT}.iam.gserviceaccount.com"
PROVISIONER = f"bankpilot-dev-provisioner@{PROJECT}.iam.gserviceaccount.com"
GRANT = "google_service_account_iam_member.operator_runner_impersonation[0]"
UNCHANGED = {
    "google_project_iam_audit_config.iamcredentials[0]",
    "google_project_iam_custom_role.impersonate", "google_project_iam_custom_role.provision",
    *(f'google_project_iam_custom_role.loader["{k}"]'
      for k in ("loader_jobs", "loader_objects", "loader_tables")),
    *(f'google_project_iam_custom_role.runtime["{k}"]'
      for k in ("jobs", "metadata", "objects", "readers", "tables")),
    "google_project_iam_member.runner_jobs", "google_service_account.loader[0]",
    "google_service_account.provisioner", "google_service_account.runner"}
DRIFT_OK = {"google_project_iam_member.runner_jobs",
            "google_project_iam_audit_config.iamcredentials[0]"}
FORBIDDEN = ("allUsers", "allAuthenticatedUsers", "google_project_iam_binding",
             "google_project_iam_policy", "google_service_account_key", "roles/owner",
             "roles/editor", "roles/iam.serviceAccountTokenCreator",
             "roles/iam.serviceAccountUser")
EXPIRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
INPUTS = ("customers", "accounts", "curated_transactions")
MARTS = ("transaction_daily", "customer_daily", "account_daily", "operations_daily")
DENIED_RE = re.compile(r"PERMISSION_DENIED|iam\.serviceAccounts\.getAccessToken", re.I)


class InputError(Exception):
    pass


def tfvar(path: Path, name: str) -> str:
    try:
        text = path.read_text()
    except OSError:
        raise InputError(f"{path.name}: unreadable") from None
    found = re.findall(rf'^{name} *= *"([^"\n]*)" *$', text, re.M)
    if len(found) != 1 or not re.fullmatch(r"user:[^@\s]+@[^@\s]+\.[^@\s]+", found[0]):
        raise InputError(f"{path.name}: {name} must be one user: principal")
    return found[0]


def condition(expiry: str) -> dict:
    """Re-derived from main.tf local runner_expiry_condition (and checked against its text)."""
    description = "M8 time-bound runner token minting; revoke explicitly after use"
    text = (REPO / "infrastructure/terraform/bootstrap/main.tf").read_text()
    if description not in text or '"runner-expires-${replace(' not in text:
        raise InputError("main.tf no longer renders the reviewed runner condition")
    return {"title": f"runner-expires-{expiry.replace(':', '-')}", "description": description,
            "expression": f'request.time < timestamp("{expiry}")'}


def _true_leaves(node: object, path: tuple = ()) -> set[tuple] | None:
    if node is True:
        return {path}
    if node is False:
        return set()
    if isinstance(node, (dict, list)):
        out: set[tuple] = set()
        for k, v in (node.items() if isinstance(node, dict) else enumerate(node)):
            sub = _true_leaves(v, (*path, k))
            if sub is None:
                return None
            out |= sub
        return out
    return None


def verify_plan(plan: dict, mode: str, operator: str, expiry: str | None) -> dict:
    problems: list[str] = []
    if plan.get("errored") is not False:
        problems.append("plan errored or errored flag missing")
    if mode == "access":
        if not (isinstance(expiry, str) and EXPIRY_RE.match(expiry)):
            raise InputError("--expiry YYYY-MM-DDTHH:MM:SSZ is required for access")
        want_after = {"service_account_id": f"projects/{PROJECT}/serviceAccounts/{RUNNER}",
                      "role": ROLES + "impersonate", "member": operator,
                      "condition": [condition(expiry)]}
    changes = plan.get("resource_changes")
    if not isinstance(changes, list):
        raise InputError("resource_changes missing")
    seen: dict[str, object] = {}
    for rc in changes:
        addr, change = rc.get("address"), rc.get("change") or {}
        actions = change.get("actions")
        if addr in seen:
            problems.append(f"duplicate address {addr}")
        seen[addr] = actions
        if rc.get("mode", "managed") != "managed":
            problems.append(f"{addr}: data source")
        if addr in UNCHANGED:
            if actions != ["no-op"]:
                problems.append(f"{addr}: {actions} (must be no-op)")
        elif addr == GRANT and mode == "access":
            unknown = _true_leaves(change.get("after_unknown") or {})
            if (actions != ["create"] or change.get("before") is not None
                    or change.get("after") != want_after
                    or unknown is None or not unknown <= {("id",), ("etag",)}):
                problems.append(f"{addr}: not exactly the approved create")
        elif addr == GRANT and mode == "revoke":
            before = change.get("before") or {}
            cond = (before.get("condition") or [{}])[0]
            if (actions != ["delete"] or change.get("after") is not None
                    or before.get("service_account_id")
                    != f"projects/{PROJECT}/serviceAccounts/{RUNNER}"
                    or before.get("role") != ROLES + "impersonate"
                    or before.get("member") != operator
                    or len(before.get("condition") or []) != 1
                    or not str(cond.get("title", "")).startswith("runner-expires-")):
                problems.append(f"{addr}: not exactly the approved delete")
        else:
            problems.append(f"{addr}: unexpected resource {actions}")
    drift = plan.get("resource_drift") or []
    removed = [d for d in drift if d.get("address") == GRANT
               and (d.get("change") or {}).get("actions") == ["delete"]]
    for d in drift:
        addr, ch = d.get("address"), d.get("change") or {}
        if d in removed and mode == "revoke":
            continue
        before, after = ch.get("before"), ch.get("after")
        if not (addr in DRIFT_OK and ch.get("actions") == ["update"]
                and isinstance(before, dict) and isinstance(after, dict)
                and set(before) == set(after)
                and all(before[k] == after[k] for k in before if k != "etag")
                and before.get("etag") and after.get("etag")
                and before["etag"] != after["etag"]):
            problems.append(f"unexpected drift: {addr}")
    addrs = [d.get("address") for d in drift]
    if len(addrs) != len(set(addrs)):
        problems.append("duplicate drift entries")
    for addr in sorted(UNCHANGED - set(seen)):
        problems.append(f"{addr}: missing from plan")
    want_grant = mode == "access" or not removed
    if want_grant and GRANT not in seen:
        problems.append(f"{GRANT}: missing from plan")
    if removed and GRANT in seen:
        problems.append(f"{GRANT}: both removed outside Terraform and planned")
    if [k for k, v in (plan.get("output_changes") or {}).items()
            if (v or {}).get("actions") != ["no-op"]]:
        problems.append("output changes present")
    v = plan.get("variables") or {}
    want_vars = {"project_id": PROJECT, "operator_member": operator,
                 "provisioner_access_enabled": False, "access_expiry": None,
                 "loader_identity_enabled": True, "loader_access_enabled": False,
                 "loader_access_expiry": None, "loader_bucket_name": None,
                 "loader_object_prefix": None, "token_audit_logging_enabled": True,
                 "runner_access_enabled": mode == "access",
                 "runner_access_expiry": expiry if mode == "access" else None}
    bad = sorted(k for k, w in want_vars.items() if k not in v or v[k].get("value") != w)
    if bad:
        problems.append(f"variables differ: {bad}")
    hours = None
    if mode == "access":
        try:
            planned = datetime.fromisoformat(str(plan["timestamp"]).replace("Z", "+00:00"))
            end = datetime.strptime(str(expiry), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            hours = round((end - planned).total_seconds() / 3600, 3)
            if not planned < end <= planned + timedelta(hours=8):
                problems.append("expiry must be after the plan timestamp and at most 8 h later")
        except (KeyError, ValueError):
            problems.append("plan timestamp missing or invalid")
    text = json.dumps(plan)
    problems += [f"forbidden token present: {b}" for b in FORBIDDEN if b in text]
    counts = {a: sum(x == [a] for x in seen.values()) for a in ("create", "delete", "no-op")}
    want_counts = {"create": int(mode == "access"), "delete": int(mode == "revoke"
                                                                   and not removed),
                   "no-op": 15}
    if counts != want_counts or len(seen) != sum(counts.values()):
        problems.append(f"counts {counts} != {want_counts}")
    return {"mode": mode, "counts": counts, "already_removed": bool(removed),
            "window_hours": hours, "problems": problems, "ok": not problems}


# --------------------------------------------------------------------------- live capture
def _load(capture: Path, name: str) -> object:
    try:
        return json.loads((capture / name).read_text())
    except (OSError, ValueError):
        raise InputError(f"{name}: missing or not JSON") from None


def _bindings(policy: object, label: str, problems: list[str]) -> list[dict]:
    if not isinstance(policy, dict) or set(policy) - {"version", "etag", "bindings",
                                                      "auditConfigs"}:
        problems.append(f"{label}: not an IAM policy")
        return []
    bindings = policy.get("bindings") or []
    return [b for b in bindings if isinstance(b, dict)]


def verify_capture(capture: Path, phase: str, operator: str, expiry: str | None) -> dict:
    problems: list[str] = []
    checks: dict[str, str] = {}

    def check(name: str, found: list[str]) -> None:
        checks[name] = "PASS" if not found else "FAIL"
        problems.extend(f"{name}: {p}" for p in found)

    # R1: the runner account policy: exactly the approved grant in the window, empty after.
    found: list[str] = []
    runner_policy = _bindings(_load(capture, "runner_sa_policy.json"), "runner policy", found)
    if phase == "window":
        if not (isinstance(expiry, str) and EXPIRY_RE.match(expiry)):
            raise InputError("--expiry is required for the window phase")
        want = [{"role": ROLES + "impersonate", "members": [operator],
                 "condition": condition(expiry)}]
        if runner_policy != want:
            found.append("not exactly the operator's expiring impersonate binding")
    elif runner_policy:
        found.append(f"{len(runner_policy)} binding(s) remain after revocation")
    check("R1 runner account policy", found)
    # R2: the runner's own grants are unchanged (project: only runner -> jobs besides Owner).
    found = []
    project = _bindings(_load(capture, "project_policy.json"), "project policy", found)
    runner_member = f"serviceAccount:{RUNNER}"
    roles = sorted((b.get("role"), tuple(b.get("members") or []), bool(b.get("condition")))
                   for b in project)
    if [r for r in roles if r[0] != "roles/owner"] != [(ROLES + "jobs", (runner_member,),
                                                        False)]:
        found.append("project bindings are not exactly Owner + runner jobs")
    if any(r[0] == "roles/owner" and (len(r[1]) != 1 or not r[1][0].startswith("user:")
                                      or r[2]) for r in roles):
        found.append("Owner binding is not a single unconditional user")
    if any("TokenCreator" in str(r[0]) or "serviceAccountUser" in str(r[0]) for r in roles):
        found.append("project holds a token-creator or actAs role")
    check("R2 project policy unchanged", found)
    found = []
    for table in (*INPUTS, *MARTS):
        role = ROLES + ("readers" if table in INPUTS else "tables")
        policy = _bindings(_load(capture, f"table_{table}_policy.json"), table, found)
        if policy != [{"role": role, "members": [runner_member]}]:
            found.append(f"{table}: not exactly the runner's {role.rsplit('_', 1)[-1]} binding")
    check("R3 runner table grants unchanged", found)
    found = []
    runtime = json.loads((REPO / "infrastructure/iam/runtime_roles.json").read_text())
    for key, spec in runtime.items():
        role_doc = _load(capture, f"role_{key}.json")
        if not isinstance(role_doc, dict) or sorted(role_doc.get("includedPermissions")
                                                    or []) != \
                sorted(spec["permissions"]) or role_doc.get("deleted"):
            found.append(f"bankpilot_dev_{key}: permissions differ from runtime_roles.json")
    check("R4 runner custom roles unchanged", found)
    found = []
    account = _load(capture, "runner_sa.json")
    keys = _load(capture, "runner_sa_keys.json")
    if not isinstance(account, dict) or account.get("email") != RUNNER or account.get(
            "disabled"):
        found.append("runner account missing, wrong or disabled")
    if not isinstance(keys, dict) or keys.get("keys"):
        found.append("runner has user-managed keys")
    check("R5 runner keyless", found)
    found = []
    for name in ("loader_sa_policy.json", "provisioner_sa_policy.json"):
        if _bindings(_load(capture, name), name, found):
            found.append(f"{name}: has bindings (must stay revoked)")
    check("R6 loader and provisioner stay revoked", found)
    found = []
    try:
        rc = int((capture / "runner_mint.rc").read_text().strip())
        err = (capture / "runner_mint.err").read_text()
    except (OSError, ValueError):
        raise InputError("runner_mint.rc/.err missing") from None
    if phase == "window" and rc != 0:
        found.append("operator could not mint a runner token inside the window")
    if phase == "revoked" and (rc == 0 or not DENIED_RE.search(err)):
        found.append("runner token mint was not denied with PERMISSION_DENIED")
    if re.search(r"ya29\.", err):
        found.append("a token appears in the captured output")
    check(f"R7 live token mint {'allowed' if phase == 'window' else 'denied'}", found)
    found = []
    try:
        addresses = [a for a in (capture / "state_list.txt").read_text().splitlines() if a]
        state_rc = int((capture / "state_list.rc").read_text().strip())
    except (OSError, ValueError):
        raise InputError("state_list.txt/.rc missing") from None
    want_state = UNCHANGED | ({GRANT} if phase == "window" else set())
    if state_rc != 0 or set(addresses) != want_state or len(addresses) != len(want_state):
        found.append(f"state holds {len(addresses)} addresses; expected {len(want_state)}")
    check(f"R8 bootstrap state ({len(want_state)} addresses)", found)
    return {"phase": phase, "checks": checks, "problems": problems, "ok": not problems}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--mode", choices=("access", "revoke"), required=True)
    p.add_argument("--plan-json", type=Path, required=True)
    p.add_argument("--expiry")
    p.add_argument("--bootstrap-tfvars", type=Path, required=True)
    i = sub.add_parser("iam")
    i.add_argument("--phase", choices=("window", "revoked"), required=True)
    i.add_argument("--capture", type=Path, required=True)
    i.add_argument("--expiry")
    i.add_argument("--bootstrap-tfvars", type=Path, required=True)
    args = ap.parse_args(argv)
    operator = ""
    try:
        operator = tfvar(args.bootstrap_tfvars, "operator_member")
        if args.command == "plan":
            try:
                plan = json.loads(args.plan_json.read_text())
            except (OSError, ValueError):
                raise InputError("plan JSON unreadable") from None
            if not isinstance(plan, dict):
                raise InputError("plan JSON is not an object")
            report = verify_plan(plan, args.mode, operator, args.expiry)
        else:
            report = verify_capture(args.capture, args.phase, operator, args.expiry)
    except InputError as exc:
        print(json.dumps({"ok": False, "problems": [str(exc)]}))
        return 2
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        print(json.dumps({"ok": False, "problems": [f"malformed input: {type(exc).__name__}"]}))
        return 2
    text = json.dumps(report, indent=2)
    for secret in (operator, operator.partition(":")[2]):
        if secret:
            text = text.replace(secret, "<operator>")
    print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
