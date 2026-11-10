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
  project                     = var.project_id
  region                      = var.region
  impersonate_service_account = var.provisioner_service_account
}

locals {
  identity_prefix = "${var.prefix}-${var.environment}"
  labels = {
    environment = var.environment
    application = "financial-data-foundation"
    data_class  = "synthetic"
    managed_by  = "terraform"
  }
  source_schemas = jsondecode(file("${path.module}/../schemas/v1/source_tables.json"))
  mart_contract  = jsondecode(file("${path.module}/../../sql/analytics/v1/bigquery.schemas.json"))
  schemas = merge(local.source_schemas, {
    for name, value in local.mart_contract.tables : name => value.fields
  })
  # Single source of truth shared with the separately governed bootstrap root (M6K, ADR-003).
  runtime_roles           = jsondecode(file("${path.module}/../iam/runtime_roles.json"))
  provisioner_permissions = jsondecode(file("${path.module}/../iam/provisioner.role.json")).includedPermissions
}

# Read-only verification of bootstrapped identity prerequisites. This root never creates
# service accounts or custom roles and never writes project IAM policy.
module "identity" {
  source                  = "./modules/identity"
  project_id              = var.project_id
  prefix                  = local.identity_prefix
  runtime_roles           = local.runtime_roles
  provisioner_permissions = local.provisioner_permissions
  provisioner_member      = "serviceAccount:${var.provisioner_service_account}"
}

module "storage" {
  source       = "./modules/storage"
  project_id   = var.project_id
  bucket_name  = var.bucket_name
  region       = var.region
  labels       = local.labels
  runner_email = module.identity.runner_email
  object_role  = module.identity.object_role
}

module "bigquery" {
  source        = "./modules/bigquery"
  project_id    = var.project_id
  dataset_id    = "${replace(var.prefix, "-", "_")}_${var.environment}"
  region        = var.region
  labels        = local.labels
  schemas       = local.schemas
  runner_email  = module.identity.runner_email
  reader_role   = module.identity.reader_role
  table_role    = module.identity.table_role
  metadata_role = module.identity.metadata_role
}

output "inventory" {
  value = {
    bucket          = module.storage.bucket_name
    dataset         = module.bigquery.dataset_id
    tables          = sort(keys(local.schemas))
    runner          = module.identity.runner_email
    provisioner     = var.provisioner_service_account
    region          = var.region
    environment     = var.environment
    identity_prefix = local.identity_prefix
    jobs_role       = module.identity.jobs_role
    deployment_note = "IaC only; identities, custom roles and project IAM are bootstrap-managed; no workloads, API activation or budget resources managed"
  }
}
