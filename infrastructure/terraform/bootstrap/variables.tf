variable "project_id" {
  type        = string
  description = "Existing dedicated pilot project; never created by this configuration."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Supply a valid existing project ID."
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
  description = "Base namespace shared with the pilot root. Shorter than the pilot root limit so <prefix>-<env>-provisioner fits the 30-character account ID."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,12}$", var.prefix))
    error_message = "Prefix must have 5-13 characters: 13 + 5 (-test) + 12 (-provisioner) = 30."
  }
}

variable "operator_member" {
  type        = string
  description = "Exactly one named human operator (user:...) allowed to mint provisioner tokens."
  validation {
    condition     = can(regex("^user:[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.operator_member))
    error_message = "Only a single user: principal is allowed; no groups, domains, service accounts or public principals."
  }
}

variable "provisioner_access_enabled" {
  type        = bool
  default     = false
  description = "Grants time-bound provisioning and impersonation only when explicitly approved."
}

variable "access_expiry" {
  type        = string
  default     = null
  description = "UTC RFC 3339 expiry (e.g. 2026-11-02T18:00:00Z), after plan time and at most 8 hours later."
  validation {
    condition = (
      var.access_expiry == null ? !var.provisioner_access_enabled : (
        can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.access_expiry))
        ? timecmp(var.access_expiry, plantimestamp()) > 0 && timecmp(var.access_expiry, timeadd(plantimestamp(), "8h")) <= 0
        : false
      )
    )
    error_message = "When access is enabled, access_expiry must be a UTC timestamp later than plan time and no more than 8 hours after it."
  }
}

variable "token_audit_logging_enabled" {
  type        = bool
  default     = true
  description = "Enable ADMIN_READ/DATA_READ audit logs for token minting (Service Account Credentials), configured on the iam.googleapis.com auditConfig entry. Changes project auditConfigs; included in bootstrap approval."
}

# ---- M7C1 dedicated loader; everything below is off/null by default ----

variable "loader_identity_enabled" {
  type        = bool
  default     = false
  description = "Creates the keyless loader account and its three custom roles. Grants nothing by itself."
}

variable "loader_access_enabled" {
  type        = bool
  default     = false
  description = "Grants the six expiring loader grants for one approved M7D window. Requires the loader identity; never together with provisioner access."
  validation {
    condition     = !var.loader_access_enabled || (var.loader_identity_enabled && !var.provisioner_access_enabled)
    error_message = "Loader access requires loader_identity_enabled and must not overlap provisioner access."
  }
}

variable "loader_access_expiry" {
  type        = string
  default     = null
  description = "UTC RFC 3339 expiry of the loader grants, after plan time and at most 8 hours later. Null while loader access is disabled."
  validation {
    condition = (
      var.loader_access_expiry == null ? !var.loader_access_enabled : (
        var.loader_access_enabled && can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.loader_access_expiry))
        ? timecmp(var.loader_access_expiry, plantimestamp()) > 0 && timecmp(var.loader_access_expiry, timeadd(plantimestamp(), "8h")) <= 0
        : false
      )
    )
    error_message = "When loader access is enabled, loader_access_expiry must be a UTC timestamp later than plan time and no more than 8 hours after it; otherwise null."
  }
}

variable "loader_bucket_name" {
  type        = string
  default     = null
  description = "The pilot bucket (from the custody pilot inputs). Null while loader access is disabled."
  validation {
    condition = (
      var.loader_bucket_name == null ? !var.loader_access_enabled
      : var.loader_access_enabled && can(regex("^[a-z][a-z0-9-]{6,61}[a-z0-9]$", var.loader_bucket_name))
    )
    error_message = "loader_bucket_name must be the approved pilot bucket name while loader access is enabled, and null otherwise."
  }
}

variable "loader_object_prefix" {
  type        = string
  default     = null
  description = "The approved M7B packet prefix, m7/ingestion/m7p-<24 hex>/. Null while loader access is disabled."
  validation {
    condition = (
      var.loader_object_prefix == null ? !var.loader_access_enabled
      : var.loader_access_enabled && can(regex("^m7/ingestion/m7p-[0-9a-f]{24}/$", var.loader_object_prefix))
    )
    error_message = "loader_object_prefix must be m7/ingestion/m7p-<24 hex>/ while loader access is enabled, and null otherwise."
  }
}
