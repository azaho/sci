# SCI hosted infrastructure: Cloudflare R2, Supabase, and DigitalOcean App Platform.
#
# State lives in HCP Terraform. The `cloud` block is configured from the
# environment: TF_CLOUD_ORGANIZATION, TF_WORKSPACE, and TF_TOKEN_app_terraform_io.
# Provider credentials come from CLOUDFLARE_API_TOKEN, DIGITALOCEAN_TOKEN, and
# SUPABASE_ACCESS_TOKEN. Inputs are TF_VAR_* variables; see README.md.
#
# Persistent resources (the Supabase project and both buckets) carry
# prevent_destroy. Never remove it to "stop" the service. Plans and state can
# contain secrets: do not commit or upload them.

terraform {
  required_version = ">= 1.13.0, < 2.0.0"

  cloud {}

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.28"
    }
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.106"
    }
    supabase = {
      source  = "supabase/supabase"
      version = "~> 1.11"
    }
  }
}

provider "cloudflare" {}
provider "digitalocean" {}
provider "supabase" {}

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

variable "instance_url" {
  description = "Public instance URL (GitHub Pages site serving web/ and sci-server.json)."
  type        = string
  validation {
    condition     = can(regex("^https://[^/?#]+(/[^?#]*)?$", var.instance_url))
    error_message = "instance_url must be an absolute https:// URL without query or fragment."
  }
}

variable "cloudflare_account_id" {
  description = "Cloudflare account ID that owns R2."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_account_id))
    error_message = "cloudflare_account_id must be a 32-character hexadecimal account ID."
  }
}

variable "temp_bucket_name" {
  description = "Private bucket for temporary uploads."
  type        = string
  default     = "sci-temp"
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.temp_bucket_name))
    error_message = "Bucket names use 3-63 lowercase letters, digits, and hyphens."
  }
}

variable "public_bucket_name" {
  description = "Public bucket holding the publication archive (publications/<uuid>/...)."
  type        = string
  default     = "sci-publications"
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.public_bucket_name))
    error_message = "Bucket names use 3-63 lowercase letters, digits, and hyphens."
  }
}

variable "r2_location" {
  description = "R2 location hint, honored only when a bucket is first created (enam = eastern North America)."
  type        = string
  default     = "enam"
}

variable "archive_hostname" {
  description = "Optional custom archive hostname, e.g. archive.example.org. Empty uses the r2.dev development domain. Without archive_zone_id the domain must be connected to the bucket manually."
  type        = string
  default     = ""
  nullable    = false
}

variable "archive_zone_id" {
  description = "Cloudflare zone ID containing archive_hostname. When set, Terraform connects the custom domain (the token needs access to that zone)."
  type        = string
  default     = ""
  nullable    = false
}

variable "archive_nosniff_rule" {
  description = "Create a zone response-header transform rule adding X-Content-Type-Options: nosniff on the archive hostname. Needs a token with zone Transform Rules edit permission."
  type        = bool
  default     = false
}

variable "supabase_organization_id" {
  description = "Supabase organization ID/slug that owns the existing project."
  type        = string
}

variable "supabase_project_ref" {
  description = "Reference of the existing Supabase project (imported, never created)."
  type        = string
  validation {
    condition     = can(regex("^[a-z]{20}$", var.supabase_project_ref))
    error_message = "supabase_project_ref must be the 20-letter project reference."
  }
}

variable "supabase_project_name" {
  description = "Display name of the existing Supabase project. Changes are ignored after import."
  type        = string
  default     = "sci"
}

variable "supabase_region" {
  description = "Region of the existing Supabase project. Informational after import; the region cannot change."
  type        = string
  default     = "us-east-1"
}

variable "digitalocean_project_name" {
  description = "DigitalOcean project grouping the SCI resources."
  type        = string
  default     = "sci"
}

variable "deploy_publisher" {
  description = "Create the App Platform publisher. Keep false until the runtime secrets exist."
  type        = bool
  default     = false
}

variable "publisher_app_name" {
  description = "App Platform app name (unique within the DigitalOcean team)."
  type        = string
  default     = "sci-publisher"
}

variable "publisher_region" {
  description = "App Platform region slug, near the Supabase region (nyc for us-east-1)."
  type        = string
  default     = "nyc"
}

variable "publisher_size" {
  description = "App Platform instance size slug."
  type        = string
  default     = "apps-s-1vcpu-0.5gb"
}

variable "repository_clone_url" {
  description = "Public HTTPS clone URL App Platform builds the publisher from."
  type        = string
  default     = "https://github.com/azaho/sci.git"
}

variable "repository_branch" {
  description = "Branch App Platform builds from."
  type        = string
  default     = "main"
}

variable "publisher_database_url" {
  description = "TLS DSN for role sci_publisher through the Supabase session pooler."
  type        = string
  default     = ""
  sensitive   = true
  nullable    = false
}

variable "r2_access_key_id" {
  description = "Bucket-scoped R2 Object Read & Write token: S3 access key ID."
  type        = string
  default     = ""
  sensitive   = true
  nullable    = false
}

variable "r2_secret_access_key" {
  description = "Bucket-scoped R2 Object Read & Write token: S3 secret access key."
  type        = string
  default     = ""
  sensitive   = true
  nullable    = false
}

locals {
  use_custom_domain    = var.archive_hostname != ""
  manage_custom_domain = local.use_custom_domain && var.archive_zone_id != ""
  instance_url         = endswith(var.instance_url, "/") ? var.instance_url : "${var.instance_url}/"
  s3_endpoint          = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"
  archive_origin = (
    local.use_custom_domain
    ? "https://${var.archive_hostname}"
    : "https://${cloudflare_r2_managed_domain.public.domain}"
  )
  archive_url = "${local.archive_origin}/publications"
  catalog_url = "https://${var.supabase_project_ref}.supabase.co/rest/v1"
}

# ---------------------------------------------------------------------------
# Supabase: the existing project, imported, and its Data API settings
# ---------------------------------------------------------------------------

import {
  to = supabase_project.sci
  id = var.supabase_project_ref
}

resource "supabase_project" "sci" {
  organization_id = var.supabase_organization_id
  name            = var.supabase_project_name
  region          = var.supabase_region

  # Required by the provider schema but used only when creating a project.
  # This project is imported and the attribute is ignored below, so the real
  # administrator password is never given to Terraform or stored in state.
  database_password = "unused-imported-project-placeholder"

  lifecycle {
    prevent_destroy = true
    ignore_changes = [
      database_password,
      instance_size,
      name,
      region,
      organization_id,
    ]
  }
}

# Data API (PostgREST): expose only the versioned public catalog schema.
# The schema itself, its views, and grants come from the SQL migrations.
resource "supabase_settings" "sci" {
  project_ref = supabase_project.sci.id

  api = jsonencode({
    db_schema            = "catalog_v1"
    db_extra_search_path = "public, extensions"
    max_rows             = 100
  })
}

# ---------------------------------------------------------------------------
# Cloudflare R2
# ---------------------------------------------------------------------------

resource "cloudflare_r2_bucket" "temp" {
  account_id    = var.cloudflare_account_id
  name          = var.temp_bucket_name
  location      = var.r2_location
  storage_class = "Standard"

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [location]
  }
}

resource "cloudflare_r2_bucket" "public" {
  account_id    = var.cloudflare_account_id
  name          = var.public_bucket_name
  location      = var.r2_location
  storage_class = "Standard"

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [location]
  }
}

# The temporary bucket is never public: keep its r2.dev domain disabled.
resource "cloudflare_r2_managed_domain" "temp" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.temp.name
  enabled     = false
}

# Abandoned multipart uploads are aborted. There is deliberately no age-based
# object deletion: committed finalizations may still need their temporary
# sources, and the publisher deletes temporary objects itself.
resource "cloudflare_r2_bucket_lifecycle" "temp" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.temp.name

  rules = [{
    id      = "abort-incomplete-multipart-uploads"
    enabled = true
    conditions = {
      prefix = ""
    }
    abort_multipart_uploads_transition = {
      condition = {
        type    = "Age"
        max_age = 86400
      }
    }
  }]
}

# Browsers and agents read the archive directly (README, figures, ZIPs).
resource "cloudflare_r2_bucket_cors" "public" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.public.name

  rules = [{
    id = "public-read"
    allowed = {
      origins = ["*"]
      methods = ["GET", "HEAD"]
      headers = ["Range", "If-Match", "If-None-Match", "If-Modified-Since", "If-Range"]
    }
    expose_headers  = ["ETag", "Content-Length", "Content-Type", "Content-Range", "Accept-Ranges", "Last-Modified"]
    max_age_seconds = 86400
  }]
}

# Published objects cannot be overwritten or deleted while this rule is
# enabled. Retention is indefinite: publications are immutable, and removal is
# an exceptional administrator action (disable the rule, delete, re-apply).
resource "cloudflare_r2_bucket_lock" "public" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.public.name

  rules = [{
    id      = "publications-immutable"
    enabled = true
    prefix  = "publications/"
    condition = {
      type = "Indefinite"
    }
  }]
}

# Production archive: custom domain on a Cloudflare zone in the same account.
# This resource cannot be imported. If the domain was connected in the
# dashboard (e.g. because the R2 token lacks zone access), leave
# archive_zone_id empty: the hostname is still used for archive_url.
resource "cloudflare_r2_custom_domain" "archive" {
  count = local.manage_custom_domain ? 1 : 0

  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.public.name
  domain      = var.archive_hostname
  zone_id     = var.archive_zone_id
  enabled     = true
  min_tls     = "1.2"
}

# Development archive: the r2.dev domain, enabled only without a custom domain.
resource "cloudflare_r2_managed_domain" "public" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.public.name
  enabled     = !local.use_custom_domain
}

# Optional X-Content-Type-Options: nosniff on archive responses. This is the
# zone's entry-point ruleset for the response-header phase; if the zone
# already has one, import it or add the rule there instead.
resource "cloudflare_ruleset" "archive_headers" {
  count = local.manage_custom_domain && var.archive_nosniff_rule ? 1 : 0

  zone_id     = var.archive_zone_id
  name        = "SCI archive response headers"
  description = "Security headers for the SCI publication archive"
  kind        = "zone"
  phase       = "http_response_headers_transform"

  rules = [{
    ref         = "sci_archive_nosniff"
    description = "X-Content-Type-Options: nosniff on the SCI archive"
    expression  = "(http.host eq \"${var.archive_hostname}\")"
    action      = "rewrite"
    enabled     = true
    action_parameters = {
      headers = {
        "X-Content-Type-Options" = {
          operation = "set"
          value     = "nosniff"
        }
      }
    }
  }]
}

# ---------------------------------------------------------------------------
# DigitalOcean App Platform: the publishing service
# ---------------------------------------------------------------------------

resource "digitalocean_project" "sci" {
  name        = var.digitalocean_project_name
  description = "SCI publishing service"
  purpose     = "Web Application"
  environment = "Production"
}

resource "digitalocean_app" "publisher" {
  count = var.deploy_publisher ? 1 : 0

  project_id = digitalocean_project.sci.id

  spec {
    name   = var.publisher_app_name
    region = var.publisher_region

    service {
      name               = "publisher"
      instance_count     = 1
      instance_size_slug = var.publisher_size
      http_port          = 8080
      source_dir         = "/"
      dockerfile_path    = "server/Dockerfile"

      # Public repository, built by App Platform; no registry access needed.
      git {
        repo_clone_url = var.repository_clone_url
        branch         = var.repository_branch
      }

      health_check {
        http_path             = "/health"
        initial_delay_seconds = 10
        period_seconds        = 10
        timeout_seconds       = 5
        success_threshold     = 1
        failure_threshold     = 6
      }

      env {
        key   = "SCI_INSTANCE_URL"
        value = local.instance_url
        scope = "RUN_TIME"
        type  = "GENERAL"
      }
      env {
        key   = "SCI_ARCHIVE_URL"
        value = local.archive_url
        scope = "RUN_TIME"
        type  = "GENERAL"
      }
      env {
        key   = "SCI_S3_ENDPOINT"
        value = local.s3_endpoint
        scope = "RUN_TIME"
        type  = "GENERAL"
      }
      env {
        key   = "SCI_S3_REGION"
        value = "auto"
        scope = "RUN_TIME"
        type  = "GENERAL"
      }
      env {
        key   = "SCI_TEMP_BUCKET"
        value = cloudflare_r2_bucket.temp.name
        scope = "RUN_TIME"
        type  = "GENERAL"
      }
      env {
        key   = "SCI_PUBLIC_BUCKET"
        value = cloudflare_r2_bucket.public.name
        scope = "RUN_TIME"
        type  = "GENERAL"
      }
      env {
        key   = "SCI_CLIENT_IP_HEADER"
        value = "do-connecting-ip"
        scope = "RUN_TIME"
        type  = "GENERAL"
      }
      env {
        key   = "SCI_DATABASE_URL"
        value = var.publisher_database_url
        scope = "RUN_TIME"
        type  = "SECRET"
      }
      env {
        key   = "SCI_S3_ACCESS_KEY_ID"
        value = var.r2_access_key_id
        scope = "RUN_TIME"
        type  = "SECRET"
      }
      env {
        key   = "SCI_S3_SECRET_ACCESS_KEY"
        value = var.r2_secret_access_key
        scope = "RUN_TIME"
        type  = "SECRET"
      }
    }

    ingress {
      rule {
        component {
          name = "publisher"
        }
        match {
          path {
            prefix = "/"
          }
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition = (
        var.publisher_database_url != "" &&
        var.r2_access_key_id != "" &&
        var.r2_secret_access_key != ""
      )
      error_message = "deploy_publisher requires publisher_database_url, r2_access_key_id, and r2_secret_access_key."
    }
  }
}

# ---------------------------------------------------------------------------
# Outputs (no secrets)
# ---------------------------------------------------------------------------

output "instance_url" {
  description = "Instance URL serving web/ and sci-server.json."
  value       = local.instance_url
}

output "archive_url" {
  description = "Public base URL of the publications/ directory (descriptor archive_url)."
  value       = local.archive_url
}

output "temp_bucket_name" {
  value = cloudflare_r2_bucket.temp.name
}

output "public_bucket_name" {
  value = cloudflare_r2_bucket.public.name
}

output "s3_endpoint" {
  description = "R2 S3 API endpoint used by the publisher."
  value       = local.s3_endpoint
}

output "catalog_url" {
  description = "Supabase Data API base (descriptor catalog_url)."
  value       = local.catalog_url
}

output "publisher_app_id" {
  description = "App Platform app ID, or null when the publisher is not deployed."
  value       = var.deploy_publisher ? digitalocean_app.publisher[0].id : null
}

output "publishing_api_url" {
  description = "Publisher HTTPS URL (descriptor publishing_api_url), or null when not deployed."
  value       = var.deploy_publisher ? digitalocean_app.publisher[0].live_url : null
}
