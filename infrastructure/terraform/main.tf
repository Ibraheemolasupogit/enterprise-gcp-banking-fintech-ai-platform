terraform {
  required_version = ">= 1.6, < 2.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.31.0"
    }
  }
  # Local state for offline preparation. Approve a secured state location before deployment.
}

provider "google" {
  project                     = var.project_id
  region                      = var.region
  impersonate_service_account = var.provisioner_service_account
}

locals {
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
}

module "identity" {
  source     = "./modules/identity"
  project_id = var.project_id
  prefix     = var.prefix
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
    deployment_note = "IaC only; no workloads, API activation or budget resources managed"
  }
}
