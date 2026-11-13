"""M6K offline IAM separation and least-privilege regression checks (ADR-003).

Static policy checks only: they do not prove cloud permission sufficiency or behaviour.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infrastructure"
TF = INFRA / "terraform"
BOOTSTRAP = TF / "bootstrap"


def role(name):
    return json.loads((INFRA / f"iam/{name}").read_text())


def permissions(name):
    values = role(name)["includedPermissions"]
    assert len(values) == len(set(values)), f"duplicate permission in {name}"
    return set(values)


def resource_blocks(text):
    return re.findall(r'^resource "([a-z0-9_]+)" "([a-z0-9_]+)"', text, re.M)


def block(text, kind, name):
    match = re.search(rf'^resource "{kind}" "{name}" \{{\n(.*?)^\}}', text, re.M | re.S)
    assert match, f"{kind}.{name} missing"
    return match.group(1)


# Permissions that administer identities, custom roles, project IAM, credentials, APIs or billing.
ADMIN_EXACT = {
    "resourcemanager.projects.setIamPolicy",
    "resourcemanager.projects.update",
    "resourcemanager.projects.delete",
    "iam.roles.create", "iam.roles.update", "iam.roles.delete", "iam.roles.undelete",
    "iam.serviceAccounts.create", "iam.serviceAccounts.update", "iam.serviceAccounts.delete",
    "iam.serviceAccounts.undelete", "iam.serviceAccounts.enable", "iam.serviceAccounts.disable",
    "iam.serviceAccounts.setIamPolicy", "iam.serviceAccounts.getIamPolicy",
    "iam.serviceAccounts.actAs", "iam.serviceAccounts.getAccessToken",
    "iam.serviceAccounts.getOpenIdToken", "iam.serviceAccounts.implicitDelegation",
    "iam.serviceAccounts.signBlob", "iam.serviceAccounts.signJwt",
    "serviceusage.services.enable", "serviceusage.services.disable",
}
ADMIN_PREFIXES = ("iam.serviceAccountKeys.", "billing.", "orgpolicy.", "logging.",
                  "resourcemanager.organizations.", "resourcemanager.folders.",
                  "iam.denypolicies.", "accesscontextmanager.")

EXPECTED_PROVISIONER = {
    "resourcemanager.projects.get", "resourcemanager.projects.getIamPolicy",
    "serviceusage.services.use", "iam.roles.get", "iam.serviceAccounts.get",
    *(f"storage.buckets.{p}" for p in ("create", "get", "update", "getIamPolicy", "setIamPolicy")),
    *(f"bigquery.datasets.{p}" for p in ("create", "get", "update", "getIamPolicy",
                                         "setIamPolicy")),
    *(f"bigquery.tables.{p}" for p in ("create", "get", "update", "getIamPolicy", "setIamPolicy")),
}


def test_provisioner_is_exactly_the_reviewed_resource_only_set():
    provisioner = permissions("provisioner.role.json")
    assert provisioner == EXPECTED_PROVISIONER
    assert len(provisioner) == 20
    assert provisioner.isdisjoint(ADMIN_EXACT)
    assert not any(p.startswith(ADMIN_PREFIXES) for p in provisioner)
    # No data-plane, job, list-enumeration or deletion rights.
    assert not any(p.endswith((".list", ".delete", ".getData", ".updateData")) for p in provisioner)
    assert not any(p.startswith(("storage.objects.", "bigquery.jobs.")) for p in provisioner)


def test_impersonation_role_is_token_minting_only():
    assert permissions("impersonate.role.json") == {"iam.serviceAccounts.getAccessToken"}


def test_runtime_roles_cannot_administer_or_escalate():
    runtime = role("runtime_roles.json")
    assert set(runtime) == {"jobs", "metadata", "objects", "readers", "tables"}
    for spec in runtime.values():
        values = set(spec["permissions"])
        assert values.isdisjoint(ADMIN_EXACT)
        assert not any(p.endswith(("IamPolicy", ".delete", ".create")) and
                       p not in {"bigquery.jobs.create", "storage.objects.create"} for p in values)


def test_cleanup_add_on_lists_versions_and_excludes_identity_and_data_reads():
    cleanup = permissions("cleanup.role.json")
    assert "storage.objects.list" in cleanup  # Required to enumerate noncurrent generations.
    assert cleanup.isdisjoint(permissions("provisioner.role.json"))
    assert cleanup.isdisjoint(ADMIN_EXACT)  # Identity/role removal belongs to bootstrap root.
    assert not any(p.endswith(("getData", "updateData", "setIamPolicy")) for p in cleanup)
    assert not any(p.startswith("bigquery.jobs.") for p in cleanup)


def test_pilot_root_cannot_create_identities_roles_or_project_iam():
    texts = [(TF / "main.tf").read_text()]
    texts += [p.read_text() for p in (TF / "modules").glob("*/main.tf.json")]
    combined = "\n".join(texts)
    for forbidden in ("google_project_iam_member", "google_project_iam_binding",
                      "google_project_iam_audit_config", "google_service_account_iam",
                      "google_service_account_key", "google_project_service",
                      "roles/owner", "roles/editor"):
        assert forbidden not in combined, forbidden
    # Custom roles and the runner account appear only as read-only data sources.
    identity = json.loads((TF / "modules/identity/main.tf.json").read_text())
    assert "resource" not in identity
    for name in ("storage", "bigquery"):
        resources = json.loads((TF / f"modules/{name}/main.tf.json").read_text())["resource"]
        assert not any(kind.startswith(("google_project_", "google_service_account"))
                       for kind in resources)


def test_pilot_root_verifies_bootstrap_prerequisites_and_detects_escalation():
    identity = json.loads((TF / "modules/identity/main.tf.json").read_text())["data"]
    policy = json.dumps(identity["google_project_iam_policy"]["project"]["lifecycle"])
    assert "var.provisioner_member" in policy  # Provisioner limited to its provision role.
    assert "condition.expression" in policy  # Provisioner binding must expire.
    assert "try(b.condition, null) == null" in policy  # Runner jobs membership is unconditional.
    provision = json.dumps(identity["google_project_iam_custom_role"]["provision"])
    assert "toset(var.provisioner_permissions)" in provision and "!self.deleted" in provision
    runner = identity["google_service_account"]["runner"]
    assert runner["lifecycle"]["postcondition"][0]["condition"] == "${!self.disabled}"
    main = (TF / "main.tf").read_text()
    assert 'file("${path.module}/../iam/runtime_roles.json")' in main
    assert 'file("${path.module}/../iam/provisioner.role.json")' in main
    assert 'provisioner_member      = "serviceAccount:${var.provisioner_service_account}"' in main


def test_bootstrap_inventory_is_additive_and_bounded():
    text = (BOOTSTRAP / "main.tf").read_text()
    assert resource_blocks(text) == [
        ("google_service_account", "runner"),
        ("google_service_account", "provisioner"),
        ("google_project_iam_custom_role", "runtime"),
        ("google_project_iam_custom_role", "provision"),
        ("google_project_iam_custom_role", "impersonate"),
        ("google_project_iam_member", "runner_jobs"),
        ("google_project_iam_member", "provisioner"),
        ("google_service_account_iam_member", "operator_impersonation"),
        ("google_project_iam_audit_config", "iamcredentials"),
        # M7C1: loader identity and access window; every one gated off by default.
        ("google_service_account", "loader"),
        ("google_project_iam_custom_role", "loader"),
        ("google_project_iam_member", "loader_jobs"),
        ("google_project_iam_member", "loader_objects"),
        ("google_bigquery_table_iam_member", "loader"),
        ("google_service_account_iam_member", "operator_loader_impersonation"),
    ]
    for forbidden in ("google_project_iam_policy", "google_project_iam_binding",
                      "google_service_account_key", "google_project_service",
                      "roles/owner", "roles/editor", "allUsers", "allAuthenticatedUsers",
                      "serviceAccountTokenCreator"):
        assert forbidden not in text
    for name in ("runner", "provisioner", "loader"):
        assert "prevent_destroy = true" in block(text, "google_service_account", name)
    # M6Q: Service Account Credentials Data Access logging is configured on the IAM service;
    # an independent iamcredentials.googleapis.com auditConfig is rejected by the API.
    audit = block(text, "google_project_iam_audit_config", "iamcredentials")
    assert 'service = "iam.googleapis.com"' in audit
    assert "iamcredentials.googleapis.com" not in text and "allServices" not in text


def test_bootstrap_never_runs_as_the_provisioner():
    text = (BOOTSTRAP / "main.tf").read_text()
    provider = re.search(r'^provider "google" \{\n(.*?)^\}', text, re.M | re.S).group(1)
    assert "impersonate" not in provider
    assert "credentials" not in provider and "access_token" not in provider
    pilot = (TF / "main.tf").read_text()
    assert "impersonate_service_account = var.provisioner_service_account" in pilot


def test_bootstrap_grants_expire_are_gated_and_account_scoped():
    text = (BOOTSTRAP / "main.tf").read_text()
    assert 'access_count    = var.provisioner_access_enabled ? 1 : 0' in text
    assert 'expression  = "request.time < timestamp(' in text
    for kind, name in (("google_project_iam_member", "provisioner"),
                       ("google_service_account_iam_member", "operator_impersonation")):
        body = block(text, kind, name)
        assert re.search(r"^\s*count\s+= local\.access_count$", body, re.M)
        assert "condition {" in body and "local.expiry_condition.expression" in body
    impersonation = block(text, "google_service_account_iam_member", "operator_impersonation")
    assert "service_account_id = google_service_account.provisioner.name" in impersonation
    assert "role               = google_project_iam_custom_role.impersonate.name" in impersonation
    provisioner = block(text, "google_project_iam_member", "provisioner")
    assert "role    = google_project_iam_custom_role.provision.name" in provisioner
    variables = (BOOTSTRAP / "variables.tf").read_text()
    assert re.search(r'variable "provisioner_access_enabled" \{[^}]*default     = false', variables)
    assert 'timeadd(plantimestamp(), "8h")' in variables
    assert '"^user:[^@\\\\s]+@' in variables  # Single human operator principal only.


@pytest.mark.parametrize("environment", ["dev", "test"])
def test_bootstrap_identity_names_fit_and_match_pilot_root(environment):
    variables = (BOOTSTRAP / "variables.tf").read_text()
    assert "{4,12}" in variables  # Max 13-character prefix.
    longest = f"{'a' * 13}-{environment}-provisioner"
    assert len(longest) <= 30
    assert len(f"bankpilot-{environment}-provisioner") <= 30
    bootstrap = (BOOTSTRAP / "main.tf").read_text()
    identity = (TF / "modules/identity/main.tf.json").read_text()
    # Pilot module prefix is "<prefix>-<env>", matching bootstrap identity_prefix.
    assert 'account_id   = "${local.identity_prefix}-runner"' in bootstrap
    assert '"account_id": "${var.prefix}-runner"' in identity
    assert 'role_id     = "${local.role_prefix}_provision"' in bootstrap
    assert '"role_id": "${replace(var.prefix, \\"-\\", \\"_\\")}_provision"' in identity


def test_bootstrap_inputs_are_placeholders_and_lockfile_matches():
    example = (BOOTSTRAP / "environments/dev.tfvars.example").read_text()
    assert "PLACEHOLDERS ONLY" in example
    assert "provisioner_access_enabled  = false" in example
    assert "@example.com" in example
    lock = ".terraform.lock.hcl"
    assert (BOOTSTRAP / lock).read_bytes() == (TF / lock).read_bytes()
    for root in (TF, BOOTSTRAP):
        assert not list(root.glob("*.tfstate*")) and not list(root.glob("*.tfvars"))


# ---------------------------------------------------------------- M7C1 loader

LOADER_EXPECTED = {
    "loader_jobs": {"bigquery.jobs.create"},
    "loader_objects": {"storage.objects.create", "storage.objects.get"},
    "loader_tables": {"bigquery.tables.get", "bigquery.tables.getData",
                      "bigquery.tables.updateData"},
}


def test_loader_roles_are_exactly_the_reviewed_minimum():
    loader = role("loader_roles.json")
    assert {k: set(v["permissions"]) for k, v in loader.items()} == LOADER_EXPECTED
    every = set().union(*LOADER_EXPECTED.values())
    assert every.isdisjoint(ADMIN_EXACT)
    assert not any(p.startswith(ADMIN_PREFIXES) for p in every)
    # No overwrite/delete, enumeration, schema/metadata change, table creation, IAM or
    # dataset rights; no service-usage, export or bucket permission.
    assert every.isdisjoint({"storage.objects.delete", "storage.objects.list",
                             "storage.objects.update", "bigquery.tables.create",
                             "bigquery.tables.update", "bigquery.tables.delete",
                             "bigquery.tables.export", "bigquery.jobs.get", "bigquery.jobs.list",
                             "bigquery.jobs.update", "bigquery.jobs.delete"})
    assert not any(p.startswith(("storage.buckets.", "bigquery.datasets.", "serviceusage.",
                                 "iam.", "resourcemanager.")) for p in every)
    assert not any("IamPolicy" in p for p in every)
    for spec in loader.values():
        assert len(spec["permissions"]) == len(set(spec["permissions"]))
        assert spec["title"] and spec["description"]


def test_loader_does_not_widen_provisioner_runner_or_impersonation():
    assert permissions("provisioner.role.json") == EXPECTED_PROVISIONER
    assert permissions("impersonate.role.json") == {"iam.serviceAccounts.getAccessToken"}
    assert set(role("runtime_roles.json")) == {"jobs", "metadata", "objects", "readers", "tables"}
    text = (BOOTSTRAP / "main.tf").read_text()
    for kind, name in (("google_project_iam_member", "loader_jobs"),
                       ("google_project_iam_member", "loader_objects"),
                       ("google_bigquery_table_iam_member", "loader"),
                       ("google_service_account_iam_member", "operator_loader_impersonation")):
        body = block(text, kind, name)
        assert "google_service_account.runner" not in body
        assert "google_service_account.provisioner" not in body
        assert "member  = google_service_account.loader[0].member" in body or \
            "member     = google_service_account.loader[0].member" in body or \
            "member             = var.operator_member" in body


def test_loader_identity_and_access_are_gated_off_by_default():
    text = (BOOTSTRAP / "main.tf").read_text()
    assert "loader_identity_count = var.loader_identity_enabled ? 1 : 0" in text
    assert "loader_access_count   = var.loader_access_enabled ? 1 : 0" in text
    assert re.search(r"^\s*count\s+= local\.loader_identity_count$",
                     block(text, "google_service_account", "loader"), re.M)
    assert "for_each    = var.loader_identity_enabled ? local.loader_roles : {}" in \
        block(text, "google_project_iam_custom_role", "loader")
    for kind, name in (("google_project_iam_member", "loader_jobs"),
                       ("google_project_iam_member", "loader_objects"),
                       ("google_service_account_iam_member", "operator_loader_impersonation")):
        body = block(text, kind, name)
        assert re.search(r"^\s*count\s+= local\.loader_access_count$", body, re.M)
        assert "condition {" in body
    tables = block(text, "google_bigquery_table_iam_member", "loader")
    # M7D correction: BigQuery rejected conditional table bindings; the table members are
    # unconditional and are ended by the loader-revoke apply (see the incident document).
    assert "for_each   = local.loader_tables" in tables and "condition" not in tables
    assert ('loader_tables         = var.loader_access_enabled ? toset(["accounts", '
            '"curated_transactions", "customers"]) : toset([])') in text
    variables = (BOOTSTRAP / "variables.tf").read_text()
    for name in ("loader_identity_enabled", "loader_access_enabled"):
        assert re.search(rf'variable "{name}" \{{[^}}]*default     = false', variables)
    for name in ("loader_access_expiry", "loader_bucket_name", "loader_object_prefix"):
        assert re.search(rf'variable "{name}" \{{\n  type        = string\n'
                         r'  default     = null', variables)
    example = (BOOTSTRAP / "environments/dev.tfvars.example").read_text()
    assert "loader_identity_enabled     = false" in example
    assert "loader_access_enabled       = false" in example


def test_loader_variables_validate_expiry_overlap_bucket_and_prefix():
    variables = (BOOTSTRAP / "variables.tf").read_text()
    assert ("!var.loader_access_enabled || (var.loader_identity_enabled && "
            "!var.provisioner_access_enabled)") in variables
    assert variables.count('timeadd(plantimestamp(), "8h")') == 2  # provisioner and loader
    assert 'timecmp(var.loader_access_expiry, plantimestamp()) > 0' in variables
    assert '"^m7/ingestion/m7p-[0-9a-f]{24}/$"' in variables
    assert '"^[a-z][a-z0-9-]{6,61}[a-z0-9]$", var.loader_bucket_name' in variables


def test_loader_grants_target_only_approved_scopes():
    text = (BOOTSTRAP / "main.tf").read_text()
    # Never the dataset (authoritative policy in the pilot root) or bucket-level IAM.
    for forbidden in ("google_bigquery_dataset_iam", "google_storage_bucket_iam",
                      "google_storage_managed_folder_iam", "google_bigquery_dataset_access",
                      "google_service_account_key", "google_project_iam_binding"):
        assert forbidden not in text
    assert "loader_dataset_id     = local.role_prefix" in text
    objects = (r'expression  = "request.time < timestamp(\"${local.loader_expiry}\") && '
               r'resource.type == \"storage.googleapis.com/Object\" && '
               r'resource.name.startsWith(\"projects/_/buckets/${coalesce(var.loader_bucket_name,'
               r' "unset")}/objects/${coalesce(var.loader_object_prefix, "unset/")}\")"')
    assert objects in text
    assert 'expression  = "request.time < timestamp(\\"${local.loader_expiry}\\")"' in text
    impersonation = block(text, "google_service_account_iam_member",
                          "operator_loader_impersonation")
    assert "service_account_id = google_service_account.loader[0].name" in impersonation
    assert "role               = google_project_iam_custom_role.impersonate.name" in impersonation
    account = block(text, "google_service_account", "loader")
    assert 'account_id   = "${local.identity_prefix}-loader"' in account
    assert len(f"{'a' * 13}-test-loader") <= 30
