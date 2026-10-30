"""Offline JSON IaC policy/schema checks, not a provider or cloud validation substitute."""

import json
from pathlib import Path

import pytest

from financial_crime_ml.analytics.contracts import COLUMNS, GRAINS, bigquery_schemas
from financial_crime_ml.data_generation.v2.contracts import MAX_AMOUNT_MINOR
from financial_crime_ml.ingestion.foundation.contracts import (
    CURATED_FIELDS,
    ENTITY_SCHEMAS,
    LINEAGE_FIELDS,
)

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infrastructure"


def module(name):
    return json.loads((INFRA / f"terraform/modules/{name}/main.tf.json").read_text())


def test_iac_inventory_is_bounded_to_approved_services():
    allowed = {
        "identity": {"google_service_account", "google_project_iam_custom_role",
                     "google_project_iam_member"},
        "storage": {"google_storage_bucket", "google_storage_bucket_iam_policy"},
        "bigquery": {"google_bigquery_dataset", "google_bigquery_table",
                     "google_bigquery_dataset_iam_policy", "google_bigquery_table_iam_member"},
    }
    for name, types in allowed.items():
        assert set(module(name)["resource"]) == types
    main = (INFRA / "terraform/main.tf").read_text()
    assert 'version = "~> 7.31.0"' in main
    assert 'required_version = ">= 1.9, < 2.0"' in main  # Cross-variable input validation.
    assert "impersonate_service_account = var.provisioner_service_account" in main
    assert 'source     = "./modules/identity"' in main
    assert "google_project_service" not in main


def test_runner_permissions_are_explicit_and_do_not_administer_resources():
    roles = module("identity")["resource"]["google_project_iam_custom_role"]
    expected = {
        "jobs": {"bigquery.jobs.create", "serviceusage.services.use"},
        "objects": {"storage.buckets.get", "storage.objects.create",
                    "storage.objects.get", "storage.objects.list"},
        "tables": {"bigquery.tables.get", "bigquery.tables.getData", "bigquery.tables.updateData"},
        "metadata": {"bigquery.datasets.get"},
        "readers": {"bigquery.tables.get", "bigquery.tables.getData"},
    }
    assert set(roles) == set(expected)
    for name, values in expected.items():
        assert set(roles[name]["permissions"]) == values
    project_bindings = module("identity")["resource"]["google_project_iam_member"]
    assert set(project_bindings) == {"jobs"}
    assert project_bindings["jobs"]["role"] == "${google_project_iam_custom_role.jobs.name}"
    for name in ("identity", "storage", "bigquery"):
        text = json.dumps(module(name))
        for forbidden in ("allUsers", "allAuthenticatedUsers", "roles/owner", "roles/editor",
                          "roles/bigquery.admin", "roles/storage.admin", "private_key"):
            assert forbidden not in text


def test_bucket_and_dataset_policies_remove_default_local_access():
    storage = module("storage")["resource"]
    policy = storage["google_storage_bucket_iam_policy"]["runner"]["policy_data"]
    assert "var.object_role" in policy and "serviceAccount:${var.runner_email}" in policy
    bq = module("bigquery")["resource"]
    policy = bq["google_bigquery_dataset_iam_policy"]["metadata"]["policy_data"]
    assert "var.metadata_role" in policy
    assert "access" not in bq["google_bigquery_dataset"]["foundation"]
    binding = bq["google_bigquery_table_iam_member"]["runner"]
    assert binding["for_each"] == "${var.schemas}"
    assert binding["role"] == (
        '${contains(["curated_transactions", "customers", "accounts"], each.key)'
        " ? var.reader_role : var.table_role}"
    )


def test_data_resources_are_private_versioned_and_protected():
    bucket = module("storage")["resource"]["google_storage_bucket"]["artifacts"]
    assert bucket["uniform_bucket_level_access"] is True
    assert bucket["public_access_prevention"] == "enforced"
    assert bucket["force_destroy"] is False
    assert bucket["versioning"]["enabled"] is True
    assert bucket["soft_delete_policy"]["retention_duration_seconds"] == 604800
    assert bucket["lifecycle"]["prevent_destroy"] is True
    assert "lifecycle_rule" not in bucket  # No unapproved automatic data deletion.
    dataset = module("bigquery")["resource"]["google_bigquery_dataset"]["foundation"]
    table = module("bigquery")["resource"]["google_bigquery_table"]["products"]
    assert dataset["location"] == bucket["location"] == "${var.region}"
    assert dataset["delete_contents_on_destroy"] is False
    assert dataset["lifecycle"]["prevent_destroy"] is True
    assert table["deletion_protection"] is True
    assert table["lifecycle"]["prevent_destroy"] is True
    assert "expiration_time" not in table


@pytest.mark.parametrize("table, fields", [
    ("curated_transactions", CURATED_FIELDS),
    ("customers", (*ENTITY_SCHEMAS["customers"], *LINEAGE_FIELDS)),
    ("accounts", (*ENTITY_SCHEMAS["accounts"], *LINEAGE_FIELDS)),
])
def test_source_schemas_exactly_match_m3_with_lossless_money_and_times(table, fields):
    schemas = json.loads((INFRA / "schemas/v1/source_tables.json").read_text())
    schema = schemas[table]
    assert [f["name"] for f in schema] == list(fields)
    assert all(f["mode"] == "REQUIRED" for f in schema)
    types = {f["name"]: f["type"] for f in schema}
    for field in fields:
        if field in ("amount_minor", "source_row"):
            assert types[field] == "INT64"
        elif field.endswith("_time") or field in ("transaction_timestamp", "onboarded_at",
                                                   "opened_at"):
            assert types[field] == "TIMESTAMP"
        else:
            assert types[field] == "STRING"
    assert "is_suspicious" not in types


def test_seven_target_schemas_and_sql_align_without_ctas_privileges():
    source = json.loads((INFRA / "schemas/v1/source_tables.json").read_text())
    marts = json.loads((ROOT / "sql/analytics/v1/bigquery.schemas.json").read_text())
    assert marts == json.loads(json.dumps(bigquery_schemas()))
    assert len(source) + len(marts["tables"]) == 7
    sql = (INFRA / "sql/marts.v1.insert.proposed.sql").read_text()
    assert "CREATE TABLE" not in sql
    assert "BEGIN TRANSACTION;" in sql and sql.endswith("COMMIT TRANSACTION;\n")
    assert sql.count("DELETE FROM") == sql.count("INSERT INTO") == 4
    for table, columns in COLUMNS.items():
        assert f'INSERT INTO `PROJECT.DATASET.{table}` ({", ".join(columns)})' in sql
        assert "currency" in GRAINS[table]
    assert "FLOAT64" not in sql
    assert "DATE(t.transaction_timestamp, 'UTC')" in sql


def test_bounded_pilot_controls_do_not_claim_hard_caps():
    controls = json.loads((INFRA / "pilot_controls.json").read_text())
    assert controls["max_transactions"] == 5000
    assert controls["max_total_upload_bytes"] <= 10 * 1024**2
    assert controls["max_query_bytes_billed"] <= 100 * 1024**2
    assert controls["max_jobs_per_approved_execution"] == 20
    assert controls["budget_is_spending_cap"] is False
    assert "not_terraform_enforced" in controls["enforcement"]
    # Worst-case per-currency sum remains exact and within signed INT64.
    assert controls["max_transactions"] * MAX_AMOUNT_MINOR < 2**63 - 1


def test_provisioner_and_cleanup_candidates_separate_data_and_delete_access():
    provisioner = json.loads((INFRA / "iam/provisioner.role.json").read_text())
    cleanup = json.loads((INFRA / "iam/cleanup.role.json").read_text())
    permissions = set(provisioner["includedPermissions"])
    assert len(permissions) == len(provisioner["includedPermissions"])
    assert "resourcemanager.projects.setIamPolicy" in permissions  # Explicit privileged bootstrap.
    assert "bigquery.tables.create" in permissions
    assert "storage.buckets.setIamPolicy" in permissions
    for permission in permissions:
        assert "*" not in permission
        assert not permission.endswith((".delete", ".undelete", ".getData", ".updateData"))
        assert not permission.startswith(("storage.objects.", "iam.serviceAccountKeys."))
        assert permission not in {"bigquery.jobs.create", "serviceusage.services.enable",
                                  "iam.serviceAccounts.getAccessToken"}
    assert set(cleanup["includedPermissions"]) == {
        "iam.roles.delete", "iam.serviceAccounts.delete", "storage.buckets.delete",
        "storage.objects.delete", "bigquery.datasets.delete", "bigquery.tables.delete",
    }
    assert permissions.isdisjoint(cleanup["includedPermissions"])


def test_root_hcl_parses_and_environment_names_are_isolated():
    # Use an installed parser only. It is optional; never install tooling from a test.
    hcl2 = pytest.importorskip("hcl2", reason="Optional offline HCL parser unavailable")
    root = INFRA / "terraform"
    for path in [*root.glob("*.tf"), *root.glob("environments/*.example")]:
        with path.open() as handle:
            assert hcl2.load(handle)
    text = (root / "main.tf").read_text()
    assert 'identity_prefix = "${var.prefix}-${var.environment}"' in text
    assert "prefix     = local.identity_prefix" in text
    assert 'dataset_id    = "${replace(var.prefix, "-", "_")}_${var.environment}"' in text
    variables = (root / "variables.tf").read_text()
    assert 'endswith(var.provisioner_service_account, "@${var.project_id}' in variables
    assert "${var.prefix}-${var.environment}-runner@" in variables
    assert "{4,17}" in variables  # 18 base + 5 environment + 7 runner = 30 SA chars.
    for environment in ("dev", "test"):
        example = (root / f"environments/{environment}.tfvars.example").read_text()
        assert f'environment                 = "{environment}"' in example


def test_root_wiring_and_inventory_count():
    # Count for_each expansions rather than mistaking resource blocks for instances.
    schemas = json.loads((INFRA / "schemas/v1/source_tables.json").read_text())
    schemas.update({k: v["fields"] for k, v in bigquery_schemas()["tables"].items()})
    count = 0
    for name in ("identity", "storage", "bigquery"):
        for instances in module(name)["resource"].values():
            for resource in instances.values():
                if "for_each" in resource:
                    assert resource["for_each"] == "${var.schemas}"
                    count += len(schemas)
                else:
                    count += 1
    assert count == 25
    main = (INFRA / "terraform/main.tf").read_text()
    for resource in ("project_id", "region", "labels"):
        assert resource in main
    # All modules must receive required inputs; child modules cannot silently default identity.
    for name in ("storage", "bigquery"):
        assert all("default" not in spec for spec in module(name)["variable"].values())
