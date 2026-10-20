# Hosted operations

Use [SETUP.md](../SETUP.md) for account and credential setup. The five bootstrap
secret names remain unchanged. The implementation adds the inputs below.

Do not place actual installation values in this document or in Git.

## Configuration

Keep these existing repository variables:

`CLOUDFLARE_ACCOUNT_ID`, `SUPABASE_ORGANIZATION_ID`, `SUPABASE_PROJECT_REF`,
`TF_CLOUD_ORGANIZATION`, and `TF_WORKSPACE`.

Add the following non-secret repository variables:

| Variable | Purpose |
| --- | --- |
| `INSTANCE_URL` | HTTPS website base, including the project subpath |
| `ARCHIVE_HOSTNAME` | Custom R2 archive hostname |
| `ARCHIVE_ZONE_ID` | Cloudflare zone for that hostname |
| `TEMPORARY_BUCKET` | Stable private R2 bucket name |
| `ARCHIVE_BUCKET` | Stable public R2 bucket name |
| `SUPABASE_PROJECT_NAME` | Name of the existing project; use its actual value |
| `SUPABASE_REGION` | Existing project region; do not change it to move the database |
| `SUPABASE_DB_HOST` | Administrator connection host |
| `SUPABASE_DB_PORT` | Administrator connection port; default `5432` |
| `SUPABASE_DB_USER` | Administrator user; a session pooler can need `postgres.PROJECT_REF` |
| `PUBLISHER_REGION` | DigitalOcean region; default `nyc` |
| `PUBLISHER_SIZE` | App Platform service size; default `apps-s-1vcpu-1gb` |
| `DEPLOY_PUBLISHER` | `false` for storage bootstrap; `true` after runtime credentials exist |
| `PUBLISHER_APP_ID` | Terraform's app ID output, after app creation |
| `PUBLISHING_API_URL` | App Platform HTTPS ingress URL |
| `CATALOG_URL` | Approved PostgREST endpoint, normally the Supabase project `/rest/v1` URL |
| `ARCHIVE_URL` | `https://ARCHIVE_HOSTNAME/publications` |
| `SUPABASE_PUBLISHABLE_KEY` | Public `sb_publishable_...` key |
| `SCI_WRITES_ENABLED` | Keep `false` until release checks and operational reconciliation pass |
| `R2_SIZE_ENFORCEMENT` | `unverified`, `verified`, or an explicit operator choice of `limited-pilot` |
| `MANAGE_ARCHIVE_HEADERS` | `true` only after reviewing the zone response-header ruleset |

Add these secrets at the indicated stage:

| Secret | Stage and purpose |
| --- | --- |
| `TF_PLAN_ENCRYPTION_KEY` | Before the infrastructure workflow: base64 of 32 random bytes, saved directly in secret storage |
| `R2_ACCESS_KEY_ID` | After bucket creation: bucket-scoped runtime S3 access key |
| `R2_SECRET_ACCESS_KEY` | Matching runtime S3 secret |
| `PUBLISHER_DATABASE_URL` | TLS connection URL for `sci_publisher`, with a direct connection or session pooling |
| `CLOUDFLARE_ZONE_API_TOKEN` | Only for managed response headers: a separate token with the required zone ruleset scope |

The plan key protects credential-bearing saved plans. Rotate it after pending
plan runs finish. It does not encrypt HCP state; protect the HCP workspace
separately. Encrypted plan artifacts expire after one day.

Do not use Supabase transaction pooling. The publisher holds session locks across
database commits and S3 operations. Use the direct endpoint or the session pooler
and verify connectivity from App Platform.

## First release

1. Configure Pages to use GitHub Actions.
2. Create the `production-infrastructure` GitHub environment. Set required
   reviewers and restrict deployment branches to `main`. The workflow checks
   that a required reviewer exists before it creates an apply job.
3. Create the `production` environment for database and publisher operations.
   Apply the review and branch policy required for the installation.
4. Keep `DEPLOY_PUBLISHER=false` and `SCI_WRITES_ENABLED=false`.
5. Run **Hosted release → infrastructure** from `main`. Review the plan log.
   The import block must identify the existing Supabase project. Reject an
   unexpected database replacement or bucket deletion.
6. Approve the apply job only after that review. It applies the exact encrypted
   plan from the preceding job.
7. Create the R2 runtime token restricted to the two SCI buckets. Save its two
   S3 values as runtime secrets. Do not use the infrastructure token at runtime.
8. Run **migrations**. This installs the private schema, public views, and roles.
   If `PUBLISHER_DATABASE_URL` is set, the job configures the dedicated role's
   password from that URL. It does not give the publisher whitelist write access.
9. Set `DEPLOY_PUBLISHER=true`, then run another reviewed infrastructure change.
   Keep writes disabled. Record the app ID and HTTPS URL in repository variables.
10. Configure archive response headers and catalog rate controls. Test them from
    an anonymous client. See the next section.
11. Run **storage-probe**. This writes small objects under unique `_sci-probe/`
    prefixes and removes them. It tests exact, changed, omitted, and repeated
    upload headers plus conditional copies. It does not change accepted records.
12. Set `R2_SIZE_ENFORCEMENT=verified` only if the R2 probe establishes enforcement.
    If it does not, decide between an upload handler and a limited pilot as
    required by the design. Do not label a limited pilot as having a hard cap.
13. Run **website**, then **verify**. Check the actual Pages project subpath.
14. Complete the hosted checks in `TEST_PLAN.md`, including a backup restore.
15. Enable a test author's public key. Set `SCI_WRITES_ENABLED=true` through a
    reviewed infrastructure change. Run sign → push → browse → pull and compare
    exact bytes before general use.

Terraform settings expose only `catalog_v1` and set the row limit to 100.
Migrations set public-role statement timeouts and deny write access. Verify that
the hosted PostgREST configuration keeps unrestricted aggregates disabled.

## Delivery and rate controls

The runtime sets JSON, PNG, ZIP, README, license, and signature content types.
ZIPs have `Content-Disposition: attachment`. Verify the actual public HTTP
responses; request headers alone are insufficient.

R2 S3 object metadata does not supply an arbitrary `X-Content-Type-Options`
response header. Add `X-Content-Type-Options: nosniff` on the archive hostname
through a Cloudflare response-header rule. The optional Terraform ruleset
implements that rule with a separately scoped zone token.

Cloudflare permits one entry ruleset for a zone phase. If the zone already has
response-header rules, do not replace them with the SCI-only ruleset. Add the SCI
rule to the existing managed configuration, or import and preserve all existing
rules before setting `MANAGE_ARCHIVE_HEADERS=true`. The R2 bootstrap token alone
does not grant general zone administration.

Use the installation's API edge controls to enforce request rate limits on the
public catalog. PostgreSQL page limits and statement timeouts are not request
rate limits. If the selected Supabase endpoint has no suitable rate control,
configure a rate-limited public endpoint in front of that Data API and set it as
`CATALOG_URL`. Keep the underlying catalog read-only. Do not add a Python catalog
proxy. Confirm anonymous profile/key-header CORS through the chosen endpoint
before enabling writes. Local Compose already applies nginx rate limits.

The publisher enforces a shared database-backed request limit. Configure the
ASGI trusted proxy addresses for App Platform before relying on per-client IP
separation. Without a trusted proxy configuration, requests behind one proxy
share its limit; untrusted forwarded headers are not accepted.

## Routine releases

- **publisher** requests an App Platform rebuild and waits for readiness. It
  checks the deployed source commit. A concurrent change to `main` can cause the
  revision check to fail; inspect the app and start a release for the new commit.
- **website** prepares and publishes only static output and public configuration.
- **migrations** applies new versioned migrations and preserves existing data.
- **infrastructure** is for resource/configuration changes and uses plan review.
- **verify** checks the static URLs, descriptor, and anonymous catalog read.

All hosted operations are explicit `workflow_dispatch` jobs on `main`. Neither
a pull request nor a push automatically creates cloud resources or deploys code.
The CI workflow has no cloud credentials.

## Runtime environment

The publisher reads:

`PUBLISHER_DATABASE_URL`, `S3_ENDPOINT_URL`, optional `S3_PUBLIC_ENDPOINT_URL`,
`S3_REGION`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `TEMPORARY_BUCKET`,
`ARCHIVE_BUCKET`, `INSTANCE_URL`, and `ARCHIVE_URL`.

Operational switches:

| Name | Default |
| --- | --- |
| `SCI_UPLOAD_SECONDS` | `900`, bounded to 30–3600 |
| `SCI_REQUESTS_PER_MINUTE` | `120` |
| `SCI_RECOVERY_SECONDS` | `30` |
| `SCI_BIND_UPLOAD_SIZE` | `false`; enable only after the real storage check |
| `SCI_WRITES_ENABLED` | `true` in application code; Terraform explicitly sets `false` for bootstrap |

The deployment does not put the administrator password or author private keys
in App Platform. Runtime values use encrypted app environment variables.

## Backups and recovery

Keep both:

- An operational PostgreSQL backup, including `sci_private`, the whitelist,
  uploads, failed reservations, and schema migration records.
- An independent copy of all archive objects, including acceptance records.

Use provider backups or `pg_dump` with a protected password file/environment.
Do not put connection passwords in command arguments, source control, public
artifacts, or this guide. For example, after configuring `PGHOST`, `PGPORT`,
`PGUSER`, `PGDATABASE`, and a mode-0600 `PGPASSFILE`:

```sh
pg_dump --format=custom --file=/private/backup/sci.dump
```

Restore into an isolated database with `pg_restore`. Reconcile archive acceptance
records, committed finalizations, and failed reservations. Test a complete pull
and compare bytes. A restored whitelist must have the same enabled/disabled
state. Do not infer permissions from archived signatures.

After total SQL loss, keep both the runtime write switch and
`sci_private.instance_state.writes_enabled` off. A catalog rebuild can restore
public reading, but it cannot reconstruct unpublished identities, pending upload
outcomes, or failed UUID reservations. Restore those from SQL backups and
reconcile them before enabling writes.

Storage locks protect against routine overwrite/deletion while enabled.
Administrators can remove them. Locks and Terraform state do not replace backups.
