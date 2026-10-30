variable "project_id" {
  type        = string
  description = "Existing approved sandbox project; never created by this configuration."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Supply a valid existing project ID."
  }
}

variable "region" {
  type        = string
  default     = "europe-west2"
  description = "Single co-located region; changes require a fresh location review."
  validation {
    condition     = var.region == "europe-west2"
    error_message = "M5 pilot is restricted to europe-west2; review IaC to expand regions."
  }
}

variable "environment" {
  type    = string
  default = "dev"
  validation {
    condition     = contains(["dev", "test"], var.environment)
    error_message = "Only isolated non-production environments are supported."
  }
}

variable "prefix" {
  type        = string
  default     = "fincrime-m5"
  description = "Base namespace; environment is appended to identities, roles and dataset."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,17}$", var.prefix))
    error_message = "Prefix must have 5-18 characters, leaving room for environment and runner."
  }
}

variable "bucket_name" {
  type        = string
  description = "Globally unique approved bucket name, containing no sensitive identifiers."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{6,61}[a-z0-9]$", var.bucket_name))
    error_message = "Use a simple 8-63 character DNS-style bucket name."
  }
}

variable "provisioner_service_account" {
  type        = string
  description = "Pre-existing dedicated provisioner identity; no service-account key files."
  validation {
    condition = can(regex(
      "^[a-z][a-z0-9-]+@[a-z][a-z0-9-]+\\.iam\\.gserviceaccount\\.com$",
      var.provisioner_service_account
    )) && endswith(var.provisioner_service_account, "@${var.project_id}.iam.gserviceaccount.com") && var.provisioner_service_account != "${var.prefix}-${var.environment}-runner@${var.project_id}.iam.gserviceaccount.com"
    error_message = "Supply a same-project provisioner service account distinct from the runner."
  }
}
