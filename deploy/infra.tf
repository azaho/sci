terraform {
  required_version = "= 1.16.6"
  cloud {}
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "= 5.28.0"
    }
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "= 2.106.0"
    }
    supabase = {
      source  = "supabase/supabase"
      version = "= 1.11.0"
    }
  }
}

# Credentials use provider environment variables. HCP state uses
# TF_TOKEN_app_terraform_io, TF_CLOUD_ORGANIZATION, and TF_WORKSPACE.
provider "cloudflare" {}
provider "digitalocean" {}
provider "supabase" {}
provider "cloudflare" {
  alias     = "zone"
  api_token = var.cloudflare_zone_api_token
}

variable "cloudflare_account_id" { type = string }
variable "archive_zone_id" { type = string }
variable "archive_hostname" { type = string }
variable "temporary_bucket_name" { type = string }
variable "archive_bucket_name" { type = string }
variable "supabase_organization_id" { type = string }
variable "supabase_project_ref" { type = string }
variable "supabase_project_name" { type = string }
variable "supabase_region" { type = string }
variable "instance_url" {
  type = string
  validation {
    condition     = can(regex("^https://[^?#]+$", var.instance_url))
    error_message = "The hosted instance URL must use HTTPS with no query or fragment."
  }
}
variable "supabase_db_password" {
  type      = string
  sensitive = true
}
variable "project_name" {
  type    = string
  default = "sci"
}
variable "publisher_region" {
  type    = string
  default = "nyc"
}
variable "publisher_size" {
  type    = string
  default = "apps-s-1vcpu-1gb"
}
variable "source_repository" {
  type    = string
  default = "https://github.com/azaho/sci.git"
}
variable "source_branch" {
  type    = string
  default = "main"
}
variable "r2_location" {
  type    = string
  default = "enam"
}
variable "deploy_publisher" {
  type        = bool
  default     = false
  description = "Enable after migrations and bucket-scoped runtime credentials exist."
}
variable "publisher_database_url" {
  type      = string
  sensitive = true
  default   = ""
}
variable "r2_access_key_id" {
  type      = string
  sensitive = true
  default   = ""
}
variable "r2_secret_access_key" {
  type      = string
  sensitive = true
  default   = ""
}
variable "writes_enabled" {
  type    = bool
  default = false
}
variable "r2_size_enforcement" {
  type    = string
  default = "unverified"
  validation {
    condition     = contains(["unverified", "verified", "limited-pilot"], var.r2_size_enforcement)
    error_message = "Use unverified, verified, or the operator-approved limited-pilot setting."
  }
}
variable "manage_archive_headers" {
  type        = bool
  default     = false
  description = "Manage the zone response-header ruleset only after reviewing existing zone rules."
}
variable "cloudflare_zone_api_token" {
  type      = string
  sensitive = true
  default   = null
}

locals {
  archive_url = "https://${var.archive_hostname}/publications"
  runtime = {
    INSTANCE_URL         = var.instance_url
    ARCHIVE_URL          = local.archive_url
    TEMPORARY_BUCKET     = cloudflare_r2_bucket.temporary.name
    ARCHIVE_BUCKET       = cloudflare_r2_bucket.archive.name
    S3_ENDPOINT_URL      = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"
    S3_REGION            = "auto"
    SCI_WRITES_ENABLED   = tostring(var.writes_enabled)
    SCI_BIND_UPLOAD_SIZE = tostring(var.r2_size_enforcement == "verified")
  }
  runtime_secrets = {
    PUBLISHER_DATABASE_URL = var.publisher_database_url
    R2_ACCESS_KEY_ID       = var.r2_access_key_id
    R2_SECRET_ACCESS_KEY   = var.r2_secret_access_key
  }
}

import {
  to = supabase_project.existing
  id = var.supabase_project_ref
}
resource "supabase_project" "existing" {
  organization_id   = var.supabase_organization_id
  name              = var.supabase_project_name
  region            = var.supabase_region
  database_password = var.supabase_db_password
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [database_password, instance_size]
  }
}
resource "supabase_settings" "catalog" {
  project_ref = supabase_project.existing.id
  api = jsonencode({
    db_schema            = "catalog_v1"
    db_extra_search_path = "public,extensions"
    max_rows             = 100
  })
}

resource "cloudflare_r2_bucket" "temporary" {
  account_id = var.cloudflare_account_id
  name       = var.temporary_bucket_name
  location   = var.r2_location
  lifecycle { prevent_destroy = true }
}
resource "cloudflare_r2_bucket" "archive" {
  account_id = var.cloudflare_account_id
  name       = var.archive_bucket_name
  location   = var.r2_location
  lifecycle { prevent_destroy = true }
}
# No temporary-bucket age lifecycle: committed finalizations must retain sources.
resource "cloudflare_r2_managed_domain" "temporary" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.temporary.name
  enabled     = false
}
resource "cloudflare_r2_managed_domain" "archive" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.archive.name
  enabled     = false
}
resource "cloudflare_r2_custom_domain" "archive" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.archive.name
  domain      = var.archive_hostname
  zone_id     = var.archive_zone_id
  enabled     = true
  min_tls     = "1.2"
}
resource "cloudflare_r2_bucket_lock" "archive" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.archive.name
  rules = [{
    id        = "protect-publications"
    enabled   = true
    prefix    = "publications/"
    condition = { type = "Indefinite" }
  }]
}
resource "cloudflare_r2_bucket_cors" "archive" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.archive.name
  rules = [{
    id              = "public-reads"
    allowed         = { origins = ["*"], methods = ["GET", "HEAD"], headers = ["Range"] }
    expose_headers  = ["Content-Type", "Content-Length", "ETag", "Content-Disposition"]
    max_age_seconds = 3600
  }]
}
resource "cloudflare_r2_bucket_cors" "temporary" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.temporary.name
  rules = [{
    id = "signed-uploads"
    allowed = {
      origins = ["*"], methods = ["PUT"], headers = ["Content-Type", "Content-Length"]
    }
    expose_headers  = ["ETag"]
    max_age_seconds = 600
  }]
}
resource "cloudflare_ruleset" "archive_headers" {
  count       = var.manage_archive_headers ? 1 : 0
  provider    = cloudflare.zone
  zone_id     = var.archive_zone_id
  name        = "SCI archive delivery"
  kind        = "zone"
  phase       = "http_response_headers_transform"
  description = "Protect original publication delivery."
  rules = [{
    ref         = "sci_archive_nosniff"
    action      = "rewrite"
    expression  = "http.host eq \"${var.archive_hostname}\""
    description = "Serve signed objects without content sniffing."
    enabled     = true
    action_parameters = {
      headers = {
        "x-content-type-options" = { operation = "set", value = "nosniff" }
      }
    }
  }]
}

resource "digitalocean_project" "sci" {
  name        = var.project_name
  description = "SCI publication service. Archive and database are external."
  purpose     = "Web Application"
  environment = "Production"
}
resource "digitalocean_app" "publisher" {
  count      = var.deploy_publisher ? 1 : 0
  project_id = digitalocean_project.sci.id
  spec {
    name   = "${var.project_name}-publisher"
    region = var.publisher_region
    service {
      name               = "publisher"
      source_dir         = "/"
      dockerfile_path    = "server/Dockerfile"
      http_port          = 8000
      instance_count     = 1
      instance_size_slug = var.publisher_size
      git {
        repo_clone_url = var.source_repository
        branch         = var.source_branch
      }
      health_check {
        http_path             = "/health"
        initial_delay_seconds = 20
        period_seconds        = 15
        timeout_seconds       = 10
        failure_threshold     = 6
      }
      dynamic "env" {
        for_each = local.runtime
        content {
          key   = env.key
          value = env.value
          scope = "RUN_TIME"
          type  = "GENERAL"
        }
      }
      dynamic "env" {
        for_each = nonsensitive(toset(keys(local.runtime_secrets)))
        content {
          key   = env.value
          value = local.runtime_secrets[env.value]
          scope = "RUN_TIME"
          type  = "SECRET"
        }
      }
    }
    ingress {
      rule {
        component { name = "publisher" }
        match {
          path { prefix = "/" }
        }
      }
    }
  }
  lifecycle {
    precondition {
      condition     = var.publisher_database_url != "" && var.r2_access_key_id != "" && var.r2_secret_access_key != ""
      error_message = "Create the dedicated publisher role and bucket-scoped R2 runtime token first."
    }
    precondition {
      condition     = !var.writes_enabled || var.r2_size_enforcement != "unverified"
      error_message = "Test R2 size enforcement or explicitly approve a limited pilot before enabling writes."
    }
  }
}

output "publisher_app_id" { value = try(digitalocean_app.publisher[0].id, null) }
output "publisher_url" { value = try(digitalocean_app.publisher[0].default_ingress, null) }
output "catalog_url" { value = "https://${supabase_project.existing.id}.supabase.co/rest/v1" }
output "archive_url" { value = local.archive_url }
