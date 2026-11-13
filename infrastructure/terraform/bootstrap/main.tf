# M6K separately governed IAM bootstrap root (ADR-003). REVIEW ONLY: never planned or applied
# without explicit approval of the exact inputs, saved plan and execution window.
#
# Executed by the bootstrap administrator (the project Owner) with a short-lived access token
# supplied for one session. It deliberately does NOT impersonate the provisioner: administrative
# permissions (custom roles, service accounts, project IAM) and ordinary resource provisioning
# are never held by the same active identity. Uses its own state, separate from the pilot root.
terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.31.0"
    }
  }
  # M6O: state lives only in ~/.secure/ebank-pilot-dev-261031-k7m4/state/<root>/, supplied by
  # scripts/terraform_custody.py via -backend-config. The /dev/null sentinel makes any init
  # without it fail closed, so repository-local state is never the default.
  backend "local" {
    path          = "/dev/null/ebank-custody-init-required/terraform.tfstate"
    workspace_dir = "/dev/null/ebank-custody-init-required/workspaces"
  }
}

provider "google" {
  project = var.project_id
}

locals {
  identity_prefix = "${var.prefix}-${var.environment}"
  role_prefix     = replace(local.identity_prefix, "-", "_")
  runtime_roles   = jsondecode(file("${path.module}/../../iam/runtime_roles.json"))
  provision_role  = jsondecode(file("${path.module}/../../iam/provisioner.role.json"))
  impersonate     = jsondecode(file("${path.module}/../../iam/impersonate.role.json"))
  access_count    = var.provisioner_access_enabled ? 1 : 0
  expiry_condition = {
    title       = "expires-${replace(coalesce(var.access_expiry, "unset"), ":", "-")}"
    description = "M6K time-bound access; remove explicitly after expiry"
    expression  = "request.time < timestamp(\"${coalesce(var.access_expiry, "1970-01-01T00:00:00Z")}\")"
  }
}

# M7C1 dedicated loader (keyless). Two independent switches, both off by default:
#   loader_identity_enabled  durable account + three custom roles; grants nothing by itself.
#   loader_access_enabled    six expiring grants for one M7D window; revoke by setting false.
locals {
  loader_roles          = jsondecode(file("${path.module}/../../iam/loader_roles.json"))
  loader_identity_count = var.loader_identity_enabled ? 1 : 0
  loader_access_count   = var.loader_access_enabled ? 1 : 0
  loader_dataset_id     = local.role_prefix
  loader_tables         = var.loader_access_enabled ? toset(["accounts", "curated_transactions", "customers"]) : toset([])
  loader_expiry         = coalesce(var.loader_access_expiry, "1970-01-01T00:00:00Z")
  loader_expiry_condition = {
    title       = "loader-expires-${replace(coalesce(var.loader_access_expiry, "unset"), ":", "-")}"
    description = "M7C1 time-bound loader access; revoke explicitly after use"
    expression  = "request.time < timestamp(\"${local.loader_expiry}\")"
  }
  # Cloud Storage evaluates resource.name as projects/_/buckets/<bucket>/objects/<object>.
  loader_object_condition = {
    title       = "loader-objects-expires-${replace(coalesce(var.loader_access_expiry, "unset"), ":", "-")}"
    description = "M7C1 time-bound packet-prefix object access; revoke explicitly after use"
    expression  = "request.time < timestamp(\"${local.loader_expiry}\") && resource.type == \"storage.googleapis.com/Object\" && resource.name.startsWith(\"projects/_/buckets/${coalesce(var.loader_bucket_name, "unset")}/objects/${coalesce(var.loader_object_prefix, "unset/")}\")"
  }
}

# ---- Durable identities and role definitions (no keys are ever created) ----

resource "google_service_account" "runner" {
  project      = var.project_id
  account_id   = "${local.identity_prefix}-runner"
  display_name = "Synthetic financial batch runner"
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_service_account" "provisioner" {
  project      = var.project_id
  account_id   = "${local.identity_prefix}-provisioner"
  display_name = "Synthetic financial pilot provisioner"
  description  = "Keyless; impersonated for pilot resource provisioning only"
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_iam_custom_role" "runtime" {
  for_each    = local.runtime_roles
  project     = var.project_id
  role_id     = "${local.role_prefix}_${each.key}"
  title       = each.value.title
  stage       = "GA"
  permissions = each.value.permissions
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_iam_custom_role" "provision" {
  project     = var.project_id
  role_id     = "${local.role_prefix}_provision"
  title       = local.provision_role.title
  description = local.provision_role.description
  stage       = local.provision_role.stage
  permissions = local.provision_role.includedPermissions
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_iam_custom_role" "impersonate" {
  project     = var.project_id
  role_id     = "${local.role_prefix}_impersonate"
  title       = local.impersonate.title
  description = local.impersonate.description
  stage       = local.impersonate.stage
  permissions = local.impersonate.includedPermissions
  lifecycle {
    prevent_destroy = true
  }
}

# ---- Additive IAM members only; never authoritative project policy/binding resources ----

# Runtime prerequisite formerly written by the provisioner (project setIamPolicy removed).
resource "google_project_iam_member" "runner_jobs" {
  project = var.project_id
  role    = google_project_iam_custom_role.runtime["jobs"].name
  member  = google_service_account.runner.member
}

# Time-bound provisioning access; disabled by default. Revoke by setting
# provisioner_access_enabled = false under an approved apply, or by the runbook procedure.
resource "google_project_iam_member" "provisioner" {
  count   = local.access_count
  project = var.project_id
  role    = google_project_iam_custom_role.provision.name
  member  = google_service_account.provisioner.member
  condition {
    title       = local.expiry_condition.title
    description = local.expiry_condition.description
    expression  = local.expiry_condition.expression
  }
}

# Token minting on the provisioner account resource only, for one named operator.
resource "google_service_account_iam_member" "operator_impersonation" {
  count              = local.access_count
  service_account_id = google_service_account.provisioner.name
  role               = google_project_iam_custom_role.impersonate.name
  member             = var.operator_member
  condition {
    title       = local.expiry_condition.title
    description = local.expiry_condition.description
    expression  = local.expiry_condition.expression
  }
}

# Records GenerateAccessToken calls (Data Access logs are off by default). M6Q: Service Account
# Credentials API Data Access logging is configured on iam.googleapis.com; the API rejects a
# separate auditConfig for the IAM Credentials service (failed in the first bootstrap apply).
# Scoped to the IAM service entry only; no all-services entry and no other service is touched.
# The address keeps its M6K name so the recovery plan adds exactly this one instance.
resource "google_project_iam_audit_config" "iamcredentials" {
  count   = var.token_audit_logging_enabled ? 1 : 0
  project = var.project_id
  service = "iam.googleapis.com"
  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
}

# ---- M7C1 loader identity (durable once created; grants nothing on its own) ----

resource "google_service_account" "loader" {
  count        = local.loader_identity_count
  project      = var.project_id
  account_id   = "${local.identity_prefix}-loader"
  display_name = "Synthetic financial ingestion loader"
  description  = "Keyless; impersonated only for an approved M7D ingestion window"
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_iam_custom_role" "loader" {
  for_each    = var.loader_identity_enabled ? local.loader_roles : {}
  project     = var.project_id
  role_id     = "${local.role_prefix}_${each.key}"
  title       = each.value.title
  description = each.value.description
  stage       = "GA"
  permissions = each.value.permissions
  lifecycle {
    prevent_destroy = true
  }
}

# ---- M7C1 loader access window: six additive, expiring grants; never permanent ----

# Project is the only scope bigquery.jobs.create has; only the expiry can narrow it.
resource "google_project_iam_member" "loader_jobs" {
  count   = local.loader_access_count
  project = var.project_id
  role    = google_project_iam_custom_role.loader["loader_jobs"].name
  member  = google_service_account.loader[0].member
  condition {
    title       = local.loader_expiry_condition.title
    description = local.loader_expiry_condition.description
    expression  = local.loader_expiry_condition.expression
  }
}

# Project-level member narrowed by condition to the packet prefix in the pilot bucket. Not a
# bucket member: the pilot root's bucket policy is authoritative and would remove it.
resource "google_project_iam_member" "loader_objects" {
  count   = local.loader_access_count
  project = var.project_id
  role    = google_project_iam_custom_role.loader["loader_objects"].name
  member  = google_service_account.loader[0].member
  condition {
    title       = local.loader_object_condition.title
    description = local.loader_object_condition.description
    expression  = local.loader_object_condition.expression
  }
}

# Additive members on exactly the three existing input tables; the pilot root's runner members
# on the same tables are separate and unaffected. Never the dataset (authoritative policy).
resource "google_bigquery_table_iam_member" "loader" {
  for_each   = local.loader_tables
  project    = var.project_id
  dataset_id = local.loader_dataset_id
  table_id   = each.key
  role       = google_project_iam_custom_role.loader["loader_tables"].name
  member     = google_service_account.loader[0].member
  condition {
    title       = local.loader_expiry_condition.title
    description = local.loader_expiry_condition.description
    expression  = local.loader_expiry_condition.expression
  }
}

# Token minting on the loader account resource only, for the same single named operator.
resource "google_service_account_iam_member" "operator_loader_impersonation" {
  count              = local.loader_access_count
  service_account_id = google_service_account.loader[0].name
  role               = google_project_iam_custom_role.impersonate.name
  member             = var.operator_member
  condition {
    title       = local.loader_expiry_condition.title
    description = local.loader_expiry_condition.description
    expression  = local.loader_expiry_condition.expression
  }
}

output "bootstrap_inventory" {
  value = {
    runner_email      = google_service_account.runner.email
    provisioner_email = google_service_account.provisioner.email
    runtime_roles     = { for k, v in google_project_iam_custom_role.runtime : k => v.name }
    provision_role    = google_project_iam_custom_role.provision.name
    impersonate_role  = google_project_iam_custom_role.impersonate.name
    access_enabled    = var.provisioner_access_enabled
    access_expiry     = var.access_expiry
    note              = "Bootstrap administrator only; provisioner never holds these administrative rights"
  }
}
