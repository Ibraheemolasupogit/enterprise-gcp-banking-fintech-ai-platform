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
