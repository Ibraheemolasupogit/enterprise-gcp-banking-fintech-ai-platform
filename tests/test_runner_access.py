"""M8: temporary operator-to-runner token minting (bootstrap root), checked two ways.

Synthetic ``terraform show -json`` documents and synthetic read-only IAM captures only; no
Terraform, gcloud or network access.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest
import test_loader_plan_check as lpc

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "infrastructure/terraform/bootstrap"
_spec = importlib.util.spec_from_file_location("runner_window_verify",
                                               ROOT / "scripts/runner_window_verify.py")
rwv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rwv)
bpc = lpc.bpc

P, R, OPERATOR, PLANNED = lpc.P, lpc.R, lpc.OPERATOR, lpc.PLANNED
EXPIRY = "2026-11-20T12:00:00Z"  # 3 h window
RUNNER_SA = f"projects/{P}/serviceAccounts/{bpc.RUNNER_EMAIL}"
GRANT = bpc.RUNNER_IMPERSONATION


def grant(actions, expiry=EXPIRY, **override):
    values = {"service_account_id": RUNNER_SA, "role": f"{R}impersonate", "member": OPERATOR,
              "condition": [bpc.runner_expiry_condition(expiry)]}
    if actions != ["create"]:
        values.update(id="i", etag="BwX")
    values.update(override)
    rc = lpc._rc(GRANT, "google_service_account_iam_member", "operator_runner_impersonation",
                 values, actions, index=0)
    if actions == ["create"]:
        rc["change"]["after_unknown"] = {"condition": [{}], "etag": True, "id": True}
    return rc


def variables(mode):
    v = lpc.variables("loader-revoke")
    v.update(runner_access_enabled={"value": mode == "runner-access"},
             runner_access_expiry={"value": EXPIRY if mode == "runner-access" else None})
    return v


def plan(mode):
    changes = [*lpc.existing(), *lpc.identity(["no-op"]),
               grant(["create"] if mode == "runner-access" else ["delete"])]
    return {"format_version": "1.2", "timestamp": PLANNED, "errored": False,
            "variables": variables(mode), "resource_changes": changes}


def check(p, mode):
    return bpc.check_plan(p, mode)


def independent(p, mode, expiry=EXPIRY):
    return rwv.verify_plan(p, "access" if mode == "runner-access" else "revoke", OPERATOR,
                           expiry if mode == "runner-access" else None)


def g(p):
    return lpc.by(p, GRANT)["change"]


# --------------------------------------------------------------------------- Terraform
def _resource(text: str, name: str) -> str:
    match = re.search(rf'resource "google_service_account_iam_member" "{name}" {{\n(.*?)\n}}',
                      text, re.S)
    assert match, name
    return match.group(1)


def test_terraform_grant_is_off_by_default_expiring_and_on_the_runner_only():
    main = (BOOTSTRAP / "main.tf").read_text()
    body = _resource(main, "operator_runner_impersonation")
    assert "count              = local.runner_access_count" in body
    assert "service_account_id = google_service_account.runner.name" in body
    assert "role               = google_project_iam_custom_role.impersonate.name" in body
    assert "member             = var.operator_member" in body
    assert "expression  = local.runner_expiry_condition.expression" in body
    assert 'runner_access_count = var.runner_access_enabled ? 1 : 0' in main
    variables = (BOOTSTRAP / "variables.tf").read_text()
    block = variables[variables.index('variable "runner_access_enabled"'):]
    assert "default     = false" in block.split("variable", 2)[1]
    assert "!var.provisioner_access_enabled && !var.loader_access_enabled" in block
    expiry = variables[variables.index('variable "runner_access_expiry"'):]
    assert "default     = null" in expiry and 'timeadd(plantimestamp(), "8h")' in expiry
    # The impersonate role is still token minting only: no actAs, no key or role admin.
    role = json.loads((ROOT / "infrastructure/iam/impersonate.role.json").read_text())
    assert role["includedPermissions"] == ["iam.serviceAccounts.getAccessToken"]


def test_runner_own_permissions_are_untouched_by_m8():
    main = (BOOTSTRAP / "main.tf").read_text()
    # Only one M8 resource, and nothing else in either root references the new switch.
    assert main.count("var.runner_access_enabled") == 1
    assert len(re.findall(r'resource "google_service_account_iam_member"', main)) == 3
    pilot = "".join(p.read_text() for p in (ROOT / "infrastructure/terraform").rglob("*.tf*")
                    if "bootstrap" not in p.parts)
    assert "runner_access" not in pilot
    runtime = json.loads((ROOT / "infrastructure/iam/runtime_roles.json").read_text())
    assert runtime["jobs"]["permissions"] == ["bigquery.jobs.create", "serviceusage.services.use"]


# --------------------------------------------------------------------------- valid plans
@pytest.mark.parametrize("mode,creates,deletes", [("runner-access", 1, 0),
                                                  ("runner-revoke", 0, 1)])
def test_valid_runner_plans_pass_both_checkers(mode, creates, deletes):
    p = plan(mode)
    report = check(p, mode)
    assert report["ok"], report["problems"]
    assert (report["creates"], report["unchanged"], report["deletes"]) == (creates, 15, deletes)
    other = independent(p, mode)
    assert other["ok"], other["problems"]
    if mode == "runner-access":
        assert report["window_hours"] == 3.0 and report["runner_access_expiry"] == EXPIRY


def test_revoke_accepts_a_grant_already_removed_outside_terraform():
    p = plan("runner-revoke")
    removed = p["resource_changes"].pop()
    removed["change"].update(actions=["delete"], after=None)
    p["resource_drift"] = [removed]
    assert check(p, "runner-revoke")["already_removed"] == [GRANT]
    assert check(p, "runner-revoke")["ok"]
    assert independent(p, "runner-revoke")["ok"]


def test_strict_etag_drift_is_accepted_by_both():
    for mode in ("runner-access", "runner-revoke"):
        p = plan(mode)
        p["resource_drift"] = [lpc._runner_drift()]
        assert check(p, mode)["ok"] and independent(p, mode)["ok"], mode


# --------------------------------------------------------------------------- adversarial
def _set_var(name, value):
    return lambda p: p["variables"].__setitem__(name, {"value": value})


ACCESS_BAD = {
    "grant on the provisioner": lambda p: g(p)["after"].update(
        service_account_id=f"projects/{P}/serviceAccounts/{bpc.PROVISIONER_EMAIL}"),
    "grant on the loader": lambda p: g(p)["after"].update(
        service_account_id=f"projects/{P}/serviceAccounts/{bpc.LOADER_EMAIL}"),
    "token creator role": lambda p: g(p)["after"].update(
        role="roles/iam.serviceAccountTokenCreator"),
    "actAs role": lambda p: g(p)["after"].update(role="roles/iam.serviceAccountUser"),
    "provision role": lambda p: g(p)["after"].update(role=f"{R}provision"),
    "another user": lambda p: g(p)["after"].update(member="user:other@example.invalid"),
    "a group": lambda p: g(p)["after"].update(member="group:ops@example.invalid"),
    "the runner itself": lambda p: g(p)["after"].update(
        member=f"serviceAccount:{bpc.RUNNER_EMAIL}"),
    "no condition": lambda p: g(p)["after"].update(condition=[]),
    "other expiry": lambda p: g(p)["after"].update(
        condition=[bpc.runner_expiry_condition("2026-11-20T15:00:00Z")]),
    "loader condition title": lambda p: g(p)["after"].update(
        condition=[bpc.loader_expiry_condition(EXPIRY)]),
    "two conditions": lambda p: g(p)["after"]["condition"].append(
        bpc.runner_expiry_condition(EXPIRY)),
    "unknown member": lambda p: g(p).update(
        after_unknown={"member": True, "id": True, "etag": True}),
    "unknown condition": lambda p: g(p).update(
        after_unknown={"condition": [{"expression": True}], "id": True}),
    "window over 8 h": lambda p: (_set_var("runner_access_expiry", "2026-11-20T17:30:00Z")(p),
                                  g(p)["after"].update(condition=[bpc.runner_expiry_condition(
                                      "2026-11-20T17:30:00Z")])),
    "expiry before plan": lambda p: (_set_var("runner_access_expiry", "2026-11-20T08:00:00Z")(
        p), g(p)["after"].update(condition=[bpc.runner_expiry_condition(
            "2026-11-20T08:00:00Z")])),
    "expiry null": _set_var("runner_access_expiry", None),
    "access flag off": _set_var("runner_access_enabled", False),
    "provisioner access on": _set_var("provisioner_access_enabled", True),
    "loader access on": _set_var("loader_access_enabled", True),
    "loader expiry set": _set_var("loader_access_expiry", EXPIRY),
    "access expiry set": _set_var("access_expiry", EXPIRY),
    "other operator variable": _set_var("operator_member", "user:x@example.invalid"),
    "grant is an update": lambda p: g(p).update(actions=["update"], before=g(p)["after"]),
    "grant missing": lambda p: p["resource_changes"].pop(),
    "duplicate grant": lambda p: p["resource_changes"].append(
        copy.deepcopy(p["resource_changes"][-1])),
    "loader grant created too": lambda p: p["resource_changes"].append(
        lpc.grants(["create"])[2]),
    "provisioner grant created too": lambda p: p["resource_changes"].append(lpc._rc(
        bpc.PROVISIONER_GRANT, "google_project_iam_member", "provisioner",
        {"project": P, "role": f"{R}provision", "member": lpc.PROVISIONER,
         "condition": [bpc.expiry_condition(EXPIRY)]}, ["create"], index=0)),
    "runner jobs grant changed": lambda p: lpc.by(p, bpc.RUNNER_GRANT)["change"].update(
        actions=["update"]),
    "runner role widened": lambda p: lpc.by(
        p, 'google_project_iam_custom_role.runtime["jobs"]')["change"].update(
        actions=["update"]),
    "unrelated drift": lambda p: p.update(resource_drift=[lpc._runner_drift(change_role=True)]),
    "plan errored": lambda p: p.update(errored=True),
    "data source": lambda p: p["resource_changes"].append({
        "address": "data.google_iam_policy.x", "mode": "data", "type": "google_iam_policy",
        "name": "x", "change": {"actions": ["read"], "before": None, "after": {}}}),
}

REVOKE_BAD = {
    "grant kept": lambda p: g(p).update(actions=["no-op"], after=g(p)["before"]),
    "grant re-created": lambda p: g(p).update(actions=["create"], before=None,
                                              after=g(p)["before"]),
    "runner flag still on": _set_var("runner_access_enabled", True),
    "runner expiry still set": _set_var("runner_access_expiry", EXPIRY),
    "runner variables absent": lambda p: [p["variables"].pop(k) for k in bpc.RUNNER_OFF],
    "deletes the loader identity": lambda p: lpc.by(p, bpc.LOADER_ACCOUNT)["change"].update(
        actions=["delete"], after=None),
    "deletes the runner jobs grant": lambda p: lpc.by(p, bpc.RUNNER_GRANT)["change"].update(
        actions=["delete"], after=None),
    "deleted grant not the runner one": lambda p: g(p)["before"].update(
        service_account_id=f"projects/{P}/serviceAccounts/{bpc.LOADER_EMAIL}"),
    "extra create": lambda p: p["resource_changes"].append(grant(["create"])),
}


@pytest.mark.parametrize("name", ACCESS_BAD)
def test_adversarial_runner_access_plans_are_rejected_by_both(name):
    p = plan("runner-access")
    ACCESS_BAD[name](p)
    assert not check(p, "runner-access")["ok"], name
    assert not independent(p, "runner-access")["ok"], name


@pytest.mark.parametrize("name", REVOKE_BAD)
def test_adversarial_runner_revoke_plans_are_rejected_by_both(name):
    p = plan("runner-revoke")
    REVOKE_BAD[name](p)
    assert not check(p, "runner-revoke")["ok"], name
    assert not independent(p, "runner-revoke")["ok"], name


@pytest.mark.parametrize("mode", ["access", "revoke", "loader-access", "loader-revoke",
                                  "loader-recovery", "loader-identity"])
def test_every_other_mode_rejects_the_runner_grant_or_switch(mode):
    base = lpc.plan(mode) if mode.startswith("loader-") and mode != "loader-recovery" else None
    if mode == "loader-recovery":
        base = lpc.plan("loader-revoke")
        base["resource_changes"] = [rc for rc in base["resource_changes"]
                                    if rc["change"]["actions"] == ["no-op"]]
    if mode in ("access", "revoke"):
        base = plan("runner-revoke")
        base["resource_changes"].pop()
        base["variables"].update(provisioner_access_enabled={"value": mode == "access"},
                                 access_expiry={"value": EXPIRY if mode == "access" else None})
    with_grant = copy.deepcopy(base)
    with_grant["resource_changes"].append(grant(["create"]))
    assert not lpc.check(with_grant, mode)["ok"], mode
    switched = copy.deepcopy(base)
    switched["variables"]["runner_access_enabled"] = {"value": True}
    assert not lpc.check(switched, mode)["ok"], mode


def test_older_plans_without_runner_variables_still_pass():
    p = lpc.plan("loader-access")
    assert "runner_access_enabled" not in p["variables"]
    assert lpc.check(p, "loader-access")["ok"]


def test_independent_verifier_requires_the_reviewed_terraform_condition(monkeypatch, tmp_path):
    main = tmp_path / "infrastructure/terraform/bootstrap/main.tf"
    main.parent.mkdir(parents=True)
    main.write_text((BOOTSTRAP / "main.tf").read_text().replace(
        "M8 time-bound runner token minting", "M8 runner token minting"))
    monkeypatch.setattr(rwv, "REPO", tmp_path)
    with pytest.raises(rwv.InputError):
        rwv.condition(EXPIRY)


def test_cli_redacts_the_operator(tmp_path):
    j, t = tmp_path / "plan.json", tmp_path / "bootstrap.tfvars"
    j.write_text(json.dumps(plan("runner-access")))
    t.write_text(f'operator_member = "{OPERATOR}"\n')
    out = subprocess.run(["python3", "-I", str(ROOT / "scripts/runner_window_verify.py"),
                          "plan", "--mode", "access", "--plan-json", str(j), "--expiry", EXPIRY,
                          "--bootstrap-tfvars", str(t)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout
    assert "named-operator" not in out.stdout


# --------------------------------------------------------------------------- live capture
RUNNER_MEMBER = f"serviceAccount:{bpc.RUNNER_EMAIL}"


def capture(tmp_path: Path, phase: str) -> Path:
    d = tmp_path / phase
    d.mkdir()

    def put(name, value):
        (d / name).write_text(value if isinstance(value, str) else json.dumps(value))

    binding = {"role": f"{R}impersonate", "members": [OPERATOR],
               "condition": rwv.condition(EXPIRY)}
    put("runner_sa_policy.json", {"version": 3, "etag": "e",
                                  **({"bindings": [binding]} if phase == "window" else {})})
    put("project_policy.json", {"version": 3, "etag": "e", "bindings": [
        {"role": "roles/owner", "members": ["user:owner@example.invalid"]},
        {"role": f"{R}jobs", "members": [RUNNER_MEMBER]}]})
    for table in (*rwv.INPUTS, *rwv.MARTS, *rwv.SCORES):
        role = f"{R}{'readers' if table in rwv.INPUTS else 'tables'}"
        put(f"table_{table}_policy.json", {"version": 1, "etag": "e",
                                           "bindings": [{"role": role,
                                                         "members": [RUNNER_MEMBER]}]})
    runtime = json.loads((ROOT / "infrastructure/iam/runtime_roles.json").read_text())
    for key, spec in runtime.items():
        put(f"role_{key}.json", {"name": f"{R}{key}", "includedPermissions":
                                 list(spec["permissions"]), "stage": "GA"})
    put("runner_sa.json", {"email": bpc.RUNNER_EMAIL, "disabled": False})
    put("runner_sa_keys.json", {})
    put("loader_sa_policy.json", {"version": 1, "etag": "e"})
    put("provisioner_sa_policy.json", {"version": 1, "etag": "e"})
    put("runner_mint.rc", "0\n" if phase == "window" else "1\n")
    put("runner_mint.err", "" if phase == "window" else (
        "ERROR: (gcloud.auth.print-access-token) PERMISSION_DENIED: Permission "
        "'iam.serviceAccounts.getAccessToken' denied on resource (or it may not exist).\n"))
    addresses = sorted(rwv.UNCHANGED | ({GRANT} if phase == "window" else set()))
    put("state_list.txt", "\n".join(addresses) + "\n")
    put("state_list.rc", "0\n")
    return d


def evaluate(d, phase):
    return rwv.verify_capture(d, phase, OPERATOR, EXPIRY if phase == "window" else None)


@pytest.mark.parametrize("phase", ["window", "revoked"])
def test_clean_captures_pass(tmp_path, phase):
    report = evaluate(capture(tmp_path, phase), phase)
    assert report["ok"], report["problems"]
    assert set(report["checks"].values()) == {"PASS"} and len(report["checks"]) == 8


def _edit(name, change):
    def apply(d):
        path = d / name
        doc = json.loads(path.read_text())
        change(doc)
        path.write_text(json.dumps(doc))
    return apply


def _text(name, value):
    return lambda d: (d / name).write_text(value)


WINDOW_BAD = {
    "second runner binding": _edit("runner_sa_policy.json", lambda p: p["bindings"].append(
        {"role": f"{R}impersonate", "members": ["user:x@example.invalid"]})),
    "unconditional grant": _edit("runner_sa_policy.json",
                                 lambda p: p["bindings"][0].pop("condition")),
    "other expiry": _edit("runner_sa_policy.json", lambda p: p["bindings"][0].update(
        condition=rwv.condition("2026-11-20T18:00:00Z"))),
    "token creator granted": _edit("runner_sa_policy.json", lambda p: p["bindings"][0].update(
        role="roles/iam.serviceAccountTokenCreator")),
    "mint failed in window": _text("runner_mint.rc", "1\n"),
    "runner role widened": _edit("role_tables.json", lambda r: r["includedPermissions"].append(
        "bigquery.tables.delete")),
    "runner gained a project role": _edit("project_policy.json", lambda p: p["bindings"].append(
        {"role": f"{R}provision", "members": [RUNNER_MEMBER]})),
    "project token creator": _edit("project_policy.json", lambda p: p["bindings"].append(
        {"role": "roles/iam.serviceAccountTokenCreator", "members": [OPERATOR]})),
    "mart table policy changed": _edit("table_customer_daily_policy.json", lambda p: p[
        "bindings"].append({"role": f"{R}tables", "members": [lpc.LOADER]})),
    "input table writable": _edit("table_customers_policy.json", lambda p: p["bindings"][
        0].update(role=f"{R}tables")),
    # M9D: the score table policy must be exactly the runner's tables binding.
    "score table policy missing the runner": _edit(
        "table_transaction_risk_scores_policy.json", lambda p: p.update(bindings=[])),
    "score table extra member": _edit("table_transaction_risk_scores_policy.json", lambda p: p[
        "bindings"][0]["members"].append(f"serviceAccount:{bpc.PROVISIONER_EMAIL}")),
    "score table extra binding": _edit("table_transaction_risk_scores_policy.json", lambda p: p[
        "bindings"].append({"role": "roles/bigquery.dataViewer",
                            "members": ["user:x@example.invalid"]})),
    "score table reader role": _edit("table_transaction_risk_scores_policy.json", lambda p: p[
        "bindings"][0].update(role=f"{R}readers")),
    "score table editor role": _edit("table_transaction_risk_scores_policy.json", lambda p: p[
        "bindings"][0].update(role="roles/bigquery.dataEditor")),
    "score table conditional": _edit("table_transaction_risk_scores_policy.json", lambda p: p[
        "bindings"][0].update(condition={"expression": "true", "title": "x"})),
    "score table public": _edit("table_transaction_risk_scores_policy.json", lambda p: p[
        "bindings"][0]["members"].append("allAuthenticatedUsers")),
    "score table not a policy": _edit("table_transaction_risk_scores_policy.json",
                                      lambda p: p.update(extra=1)),
    "runner has a key": _edit("runner_sa_keys.json", lambda k: k.update(keys=[{"name": "k"}])),
    "runner disabled": _edit("runner_sa.json", lambda a: a.update(disabled=True)),
    "loader regained access": _edit("loader_sa_policy.json", lambda p: p.update(bindings=[
        {"role": f"{R}impersonate", "members": [OPERATOR]}])),
    "provisioner window open": _edit("provisioner_sa_policy.json", lambda p: p.update(bindings=[
        {"role": f"{R}impersonate", "members": [OPERATOR]}])),
    "grant missing from state": _text("state_list.txt", "\n".join(sorted(rwv.UNCHANGED))),
    "state list failed": _text("state_list.rc", "1\n"),
    "token in output": _text("runner_mint.err", "ya29.abcdef\n"),
}

REVOKED_BAD = {
    "binding remains": _edit("runner_sa_policy.json", lambda p: p.update(bindings=[
        {"role": f"{R}impersonate", "members": [OPERATOR]}])),
    "mint still works": lambda d: (_text("runner_mint.rc", "0\n")(d),
                                   _text("runner_mint.err", "")(d)),
    "mint failed for another reason": _text("runner_mint.err", "ERROR: network unreachable\n"),
    "grant still in state": _text("state_list.txt", "\n".join(sorted(rwv.UNCHANGED | {GRANT}))),
}


@pytest.mark.parametrize("name", WINDOW_BAD)
def test_window_capture_adversarial(tmp_path, name):
    d = capture(tmp_path, "window")
    WINDOW_BAD[name](d)
    assert not evaluate(d, "window")["ok"], name


@pytest.mark.parametrize("name", REVOKED_BAD)
def test_revoked_capture_adversarial(tmp_path, name):
    d = capture(tmp_path, "revoked")
    REVOKED_BAD[name](d)
    assert not evaluate(d, "revoked")["ok"], name


def test_window_capture_fails_as_revoked_and_vice_versa(tmp_path):
    assert not evaluate(capture(tmp_path, "window"), "revoked")["ok"]
    assert not evaluate(capture(tmp_path, "revoked"), "window")["ok"]


SCORE_BAD = [n for n in WINDOW_BAD if n.startswith("score table")]


@pytest.mark.parametrize("name", SCORE_BAD)
def test_score_table_violations_fail_only_r3_in_both_phases(tmp_path, name):
    for phase in ("window", "revoked"):
        sub = tmp_path / f"{phase}-{SCORE_BAD.index(name)}"
        sub.mkdir()
        d = capture(sub, phase)
        WINDOW_BAD[name](d)
        report = evaluate(d, phase)
        failed = sorted(k for k, v in report["checks"].items() if v == "FAIL")
        assert failed == ["R3 runner table grants unchanged"], (name, phase, failed)
        assert any("transaction_risk_scores" in p for p in report["problems"]), name


def test_r3_covers_the_seven_tables_and_the_score_table(tmp_path):
    assert rwv.INPUTS == ("customers", "accounts", "curated_transactions")
    assert rwv.MARTS == ("transaction_daily", "customer_daily", "account_daily",
                         "operations_daily")
    assert rwv.score_tables() == rwv.SCORES == ("transaction_risk_scores",)
    d = capture(tmp_path, "revoked")
    assert evaluate(d, "revoked")["ok"]
    policy = json.loads((d / "table_transaction_risk_scores_policy.json").read_text())
    assert policy["bindings"] == [{"role": f"{R}tables", "members": [RUNNER_MEMBER]}]
    collector = (ROOT / "scripts/runner_window_collect.sh").read_text()
    tables = re.search(r"^TABLES=\(([^)]*)\)", collector, re.M).group(1).split()
    assert tables == [*rwv.INPUTS, *rwv.MARTS, *rwv.SCORES]


def test_capture_without_the_score_table_fails_closed(tmp_path):
    """A pre-M9D capture (seven tables only) can no longer pass R3."""
    d = capture(tmp_path, "revoked")
    (d / "table_transaction_risk_scores_policy.json").unlink()
    with pytest.raises(rwv.InputError):
        evaluate(d, "revoked")


def test_score_table_is_rederived_from_the_contract(monkeypatch):
    monkeypatch.setattr(rwv, "SCORES", ("transaction_scores",))
    with pytest.raises(rwv.InputError):
        rwv.score_tables()


def test_missing_capture_file_fails_closed(tmp_path):
    d = capture(tmp_path, "revoked")
    (d / "runner_mint.rc").unlink()
    with pytest.raises(rwv.InputError):
        evaluate(d, "revoked")


def test_collector_is_read_only_and_never_stores_a_token():
    script = "\n".join(line for line in (ROOT / "scripts/runner_window_collect.sh").read_text()
                       .splitlines() if not line.lstrip().startswith("#"))
    subprocess.run(["bash", "-n", str(ROOT / "scripts/runner_window_collect.sh")], check=True)
    for forbidden in ("setIamPolicy", "add-iam-policy-binding", "-X DELETE", "-X PUT",
                      "-X PATCH", " apply", " plan ", "keys create", "echo \"$TOKEN",
                      "> \"$OUT/token"):
        assert forbidden not in script, forbidden
    assert re.search(r'--impersonate-service-account="\$RUNNER" \\\n\s+> /dev/null', script)
    assert script.count("-X POST") == 2  # getIamPolicy reads only
    assert "requestedPolicyVersion" in script and 'unset TOKEN' in script


def test_duplicate_resource_changes_are_rejected_in_every_bootstrap_mode():
    """M8 tightening: _classify used sets, so a duplicated entry used to be merged silently."""
    for mode in ("loader-access", "loader-revoke"):
        p = lpc.plan(mode)
        p["resource_changes"].append(copy.deepcopy(p["resource_changes"][-1]))
        report = lpc.check(p, mode)
        assert not report["ok"] and any("duplicate" in x for x in report["problems"]), mode
