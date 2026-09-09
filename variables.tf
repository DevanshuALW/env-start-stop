variable "client_name" {
  description = "Client or project label. Used only in tags."
  type        = string
}

variable "name_prefix" {
  description = "Prefix for Lambda, IAM, secrets, and schedule names. Letters, numbers, hyphens. Example: acme-np-start-stop"
  type        = string
  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9-]{1,39}$", var.name_prefix))
    error_message = "name_prefix must be 2-40 chars, start with alphanumeric, then alphanumeric or hyphen. IAM/Lambda names have a 64-char limit."
  }
}

variable "environment" {
  description = "Must not be prod. This stack starts and stops databases and apps."
  type        = string
  default     = "non-prod"
  validation {
    condition     = !contains(["prod", "production", "prd"], lower(var.environment))
    error_message = "Do not use this stack against prod."
  }
}

variable "aws_region" {
  description = "Region where the Lambda and nightly schedules live."
  type        = string
}

variable "aws_account_id" {
  description = "AWS account that owns the Lambda, EKS clusters, and RDS."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit account id."
  }
}

variable "slack_channel_id" {
  description = "Slack channel where STOP/START results are posted."
  type        = string
  validation {
    condition     = can(regex("^[CG][A-Z0-9]+$", var.slack_channel_id))
    error_message = "slack_channel_id must look like C… or G… (open the channel, copy the link)."
  }
}

variable "slack_commands" {
  description = "Slash commands that open the form."
  type        = list(string)
  default     = ["/env-start-stop"]
  validation {
    condition     = length(var.slack_commands) > 0 && alltrue([for c in var.slack_commands : startswith(c, "/")])
    error_message = "slack_commands must be a non-empty list of slash commands, e.g. [\"/env-start-stop\"]."
  }
}

variable "modal_title" {
  description = "Slack modal title. Slack hard-limit is 24 characters."
  type        = string
  default     = "Env start/stop"
  validation {
    condition     = length(var.modal_title) >= 1 && length(var.modal_title) <= 24
    error_message = "modal_title must be 1-24 characters (Slack limit)."
  }
}

variable "argocd_namespace" {
  type    = string
  default = "argocd"
}

variable "rbac_group" {
  description = "Kubernetes group on the EKS access entry and RoleBinding. Must match."
  type        = string
  default     = "start-stop-automation"
}

variable "schedule_timezone" {
  description = "IANA timezone for nightly jobs. Example: Asia/Calcutta"
  type        = string
  default     = "Asia/Calcutta"
}

variable "lambda_timeout" {
  type    = number
  default = 900
}

variable "lambda_memory" {
  type    = number
  default = 512
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the Lambda."
  type        = number
  default     = 30
}

variable "secret_recovery_days" {
  description = "Days Slack secrets stay recoverable after destroy."
  type        = number
  default     = 7
}

variable "regions" {
  description = "Clusters this Lambda may talk to. key is the Slack value (short, no spaces)."
  type = list(object({
    key        = string
    label      = string
    short_name = string
    aws_region = string
    cluster    = string
  }))
  validation {
    condition     = length(var.regions) > 0 && length(var.regions) == length(distinct([for r in var.regions : r.key]))
    error_message = "regions must be non-empty and each key must be unique."
  }
}

variable "targets" {
  description = "One Slack row each. parent is the Argo app-of-apps name. rds may be empty."
  type = list(object({
    key        = string
    region_key = string
    namespace  = string
    parent     = optional(string, "")
    rds        = optional(list(string), [])
    apps       = list(string)
  }))
  validation {
    condition     = length(var.targets) > 0 && length(var.targets) == length(distinct([for t in var.targets : t.key]))
    error_message = "targets must be non-empty and each key must be unique."
  }
}

variable "defaults" {
  description = "region_key => target keys pre-selected when that region is picked in Slack."
  type        = map(list(string))
  default     = {}
}

variable "schedules" {
  description = "Nightly jobs. Empty map = Slack only, no clock."
  type = map(object({
    cron   = string
    action = string
    phase  = optional(string, "")
  }))
  default = {
    stop = {
      cron   = "cron(0 23 * * ? *)"
      action = "STOP"
    }
    start_rds = {
      cron   = "cron(40 5 * * ? *)"
      action = "START"
      phase  = "rds"
    }
    start_apps = {
      cron   = "cron(0 6 * * ? *)"
      action = "START"
      phase  = "apps"
    }
  }
  validation {
    condition = alltrue([
      for s in values(var.schedules) : contains(["STOP", "START"], s.action)
    ])
    error_message = "schedules.*.action must be STOP or START."
  }
}
