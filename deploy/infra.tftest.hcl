mock_provider "cloudflare" {}
mock_provider "cloudflare" {
  alias = "zone"
}
mock_provider "digitalocean" {}
mock_provider "supabase" {}
override_resource {
  target = supabase_project.existing
  values = { id = "sci-test-project-ref" }
}

variables {
  cloudflare_account_id    = "test-account"
  archive_zone_id          = "test-zone"
  archive_hostname         = "archive.example.org"
  temporary_bucket_name    = "sci-test-temporary"
  archive_bucket_name      = "sci-test-public"
  supabase_organization_id = "test-organization"
  supabase_project_ref     = "sci-test-project-ref"
  supabase_project_name    = "sci-test"
  supabase_region          = "us-east-1"
  supabase_db_password     = "test-only-not-a-credential"
  instance_url             = "https://example.org/sci/"
}

run "storage_bootstrap" {
  command = plan
  assert {
    condition     = length(digitalocean_app.publisher) == 0
    error_message = "Storage bootstrap must not create a publisher without runtime credentials."
  }
  assert {
    condition     = cloudflare_r2_managed_domain.temporary.enabled == false
    error_message = "Temporary storage must stay private."
  }
  assert {
    condition     = cloudflare_r2_bucket_lock.archive.rules[0].condition.type == "Indefinite"
    error_message = "Published objects must have an indefinite storage lock."
  }
}

run "publisher_plan" {
  command = plan
  variables {
    deploy_publisher       = true
    publisher_database_url = "postgresql://sci_publisher:test-only@db.example.org/postgres?sslmode=require"
    r2_access_key_id       = "test-access-key"
    r2_secret_access_key   = "test-secret-key"
  }
  assert {
    condition     = length(digitalocean_app.publisher) == 1
    error_message = "The publisher must be selectable after bootstrap."
  }
}

run "unverified_storage_blocks_writes" {
  command = plan
  variables {
    deploy_publisher       = true
    publisher_database_url = "postgresql://sci_publisher:test-only@db.example.org/postgres?sslmode=require"
    r2_access_key_id       = "test-access-key"
    r2_secret_access_key   = "test-secret-key"
    writes_enabled         = true
  }
  expect_failures = [digitalocean_app.publisher]
}
