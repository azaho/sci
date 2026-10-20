# sci

SCI is a small publication system for research agents and humans. A
publication is a signed folder: text (`README.md`), metadata and citations
(`sci.json`), an optional headline figure, and optional supporting files.
Servers verify signatures and their key whitelist, keep the exact signed
bytes in object storage, and expose a read-only catalog. Anyone can pull a
publication and verify it independently.

Requirements are in [DESIGN.md](DESIGN.md), release checks in
[TEST_PLAN.md](TEST_PLAN.md), and the account setup in [SETUP.md](SETUP.md).
The SCI software is licensed under Apache-2.0 ([LICENSE](LICENSE)).
Publications use the fixed terms in
[`sci/publication_LICENSE.txt`](sci/publication_LICENSE.txt): CC BY 4.0 for
text, figures, and data, and MIT for code.

## Contents

| Path | What it is |
| --- | --- |
| `sci/` | Python package and `sci` command: validation, packaging, signing, verification, push, pull. Shared by the CLI and the server. |
| `server/` | Publishing service (`app.py`), PostgreSQL access and migrations (`database.py`), object storage (`storage.py`), SQL (`schema.sql`, `catalog.sql`, `admin.sql`), `Dockerfile`. |
| `web/` | Static website (no build step). Reads the instance descriptor, the catalog, and the archive directly. |
| `tests/` | Unit and integration tests. |
| `deploy/compose.yaml` | Complete local installation. |
| `deploy/infra.tf` | Hosted infrastructure (R2, Supabase, DigitalOcean App Platform). |
| `.github/workflows/deploy.yml` | Tests and hosted deployment operations. |

## Install the CLI

```sh
pip install "git+https://github.com/azaho/sci"   # distribution name: sci-publish; command: sci
sci --help                                         # the complete authoring and publishing guide
```

The PyPI name `sci` belongs to another project, so the distribution is
called `sci-publish`. Only `cryptography` is required.

## Publish in five commands

```sh
sci server https://sci.example.org     # instance URL; saved in ~/.sci/config.json
sci keygen                             # ~/.sci/key.pem and key.pub.pem; prints your public key
# ask the server administrator to enable the printed public key
sci sign ./result                      # writes LICENSE, files.zip, manifest.json, manifest.json.sig
sci verify ./result
sci push ./result                      # prints the publication URL
sci pull <UUID> --output ./copy        # anyone, any machine, no key needed
```

To mirror a publication to another instance, pull it and push the folder
with `--server`; do not sign it again.

## Local installation (Docker Compose)

```sh
docker compose -f deploy/compose.yaml up -d --build
docker compose -f deploy/compose.yaml down        # stop; data volumes are kept
```

Startup waits for PostgreSQL, applies pending migrations, creates missing
buckets, and writes the local `sci-server.json`. Existing data is never
reset. Services on the host:

| Service | URL |
| --- | --- |
| Website and descriptor | http://localhost:8000/ (instance URL) |
| Publishing API | http://localhost:8080 |
| Catalog (PostgREST) | http://localhost:3000 |
| Archive (RustFS, S3-compatible) | http://localhost:9000/sci-publications/publications |
| PostgreSQL | localhost:5432, user `sci_admin`, database `sci` |

```sh
sci server http://localhost:8000/
sci keygen
docker compose -f deploy/compose.yaml exec db \
  psql -U sci_admin -d sci -c "select * from sci.enable_key('<public key from keygen>')"
sci sign ./result && sci push ./result
```

Data lives in the named volumes `sci-local_db` and `sci-local_archive`. It
survives `down`, restarts, and image rebuilds. **Destructive reset**, only
when you mean it: `docker compose -f deploy/compose.yaml down --volumes`.
A second instance on the same machine needs another project name and other
ports, for example
`SCI_WEB_PORT=8100 SCI_API_PORT=8180 SCI_CATALOG_PORT=3100 SCI_STORAGE_PORT=9100 SCI_DB_PORT=5532 docker compose -p sci-lab2 -f deploy/compose.yaml up -d`.
Default passwords in the Compose file are for a machine-local installation
only; override them with the `SCI_*_PASSWORD` variables.

RustFS is S3-compatible but is not R2. Presigned upload limits, conditional
copies, and locks must also be tested against real R2 (see "Open items").

## Whitelist administration

Run these as the database administrator: the Supabase SQL editor (role
`postgres`) for the hosted instance, or `psql` as `sci_admin` locally. The
key is the unpadded base64url value printed by `sci keygen` (the manifest
JWK `x`). Changes need no restart.

```sql
select * from sci.enable_key('m6V-mVNXkl8gI4poxqa78XvZu7cOOso8-xv41xeLSfQ');
select * from sci.disable_key('m6V-mVNXkl8gI4poxqa78XvZu7cOOso8-xv41xeLSfQ');
select * from sci.list_keys();
```

The publishing role can read the whitelist but cannot change it. Public
roles cannot see it. Disabling a key blocks uncommitted uploads at once;
publications whose finalization was already committed still finish.

## Reading the catalog

Clients read `<instance>/sci-server.json`, then query `catalog_url` with
`Accept-Profile: catalog_v1` (and `apikey: <supabase_publishable_key>` for
Supabase). `sci --help` lists working examples. The views are
`publications`, `citations`, and `citation_counts`, plus
`GET /rpc/normalize_citation_target?target=...`. Full-text search uses the
`search_document` column with configuration `english`:
`search_document=wfts(english).<words>`. Feeds order by
`accepted_at.desc,uuid.desc`; page with
`or=(accepted_at.lt.<ts>,and(accepted_at.eq.<ts>,uuid.lt.<uuid>))`.
Pages hold at most 100 rows.

## Operations and recovery

| Situation | Action |
| --- | --- |
| Interrupted finalization | Automatic. The service's background worker (every 60 s) resumes committed `finalizing` publications, expires abandoned uploads, and deletes their temporary objects. It never deletes sources of an unfinished committed publication. |
| Terminal failure (`failed`) | The UUID stays reserved. The author signs again to get a new UUID. Remaining objects are left for manual cleanup. |
| `operator_attention` error in logs | A destination object or acceptance record conflicts with the committed record. Nothing is overwritten. Compare the objects with the SQL record and reconcile by hand. |
| SQL lost, archive intact | Start the service with `SCI_WRITES_ENABLED=false`. Restore the latest SQL backup if there is one. Then rebuild the catalog: `SCI_ADMIN_DATABASE_URL=... python -m server.app rebuild-catalog --dry-run`, then without `--dry-run`. Restored identities are disabled; re-enable keys explicitly. Enable writes only after failed-UUID reservations and the whitelist are reconciled. |
| Remove a publication | Administrator action: disable the R2 lock rule, delete `publications/<uuid>/`, delete the SQL row (`delete from sci.publications where uuid = ...` as administrator), restore the lock. |

Back up PostgreSQL (whitelist, upload outcomes, failed reservations) and the
public bucket separately. Terraform state is not a backup.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[server,test]'
pytest -q
```

Unit tests need nothing else. Integration tests start real services and
skip when a binary is missing: PostgreSQL comes from the `pgserver` wheel;
set `SCI_TEST_RUSTFS` and `SCI_TEST_POSTGREST` to RustFS 1.0.3 and PostgREST
12.2.12 binaries (the CI workflow downloads both).

## Open items

- **R2 upload size enforcement.** Not yet tested against real R2. Presigned
  URLs carry no size-binding header by default; after testing, set
  `SCI_PRESIGN_CONTENT_LENGTH=true` to sign `Content-Length`. The server
  always checks sizes and hashes after upload.
- **Legacy corpus import** waits for the signed import-attestation format.

## Hosted deployment

The hosted installation is described by two files:

| File | Purpose |
| --- | --- |
| `deploy/infra.tf` | Terraform for Cloudflare R2, the existing Supabase project, and DigitalOcean App Platform. State is stored in HCP Terraform. |
| `deploy/.terraform.lock.hcl` | Provider lockfile (cloudflare 5.28.0, digitalocean 2.106.0, supabase 1.11.0; linux_amd64, darwin_arm64, darwin_amd64). Commit it; regenerate with `terraform -chdir=deploy providers lock -platform=linux_amd64 -platform=darwin_arm64 -platform=darwin_amd64` after changing provider versions. |
| `.github/workflows/deploy.yml` | Tests on every push and pull request to `main`, plus the manual deployment operations. |

### Resources managed by Terraform

| Resource | Notes |
| --- | --- |
| `supabase_project.sci` | The existing project, adopted through an `import` block. `prevent_destroy`. Name, region, organization, size, and password changes are ignored, so the project is never renamed, resized, or replaced. |
| `supabase_settings.sci` | Data API: exposes only `catalog_v1`, extra search path `public, extensions`, `max_rows = 100`. |
| `cloudflare_r2_bucket.temp` | Private temporary uploads (default `sci-temp`). `prevent_destroy`. Its `r2.dev` domain is explicitly disabled. Lifecycle: abort incomplete multipart uploads after 1 day; **no** age-based object deletion (the publisher cleans up; committed finalizations need their sources). |
| `cloudflare_r2_bucket.public` | Publication archive (default `sci-publications`). `prevent_destroy`. CORS: `GET`/`HEAD` from any origin, request headers `Range` and conditional headers, exposes `ETag`, `Content-Length`, `Content-Type`, `Content-Range`, `Accept-Ranges`, `Last-Modified`. |
| `cloudflare_r2_bucket_lock.public` | Indefinite lock on prefix `publications/`: published objects cannot be overwritten or deleted while the rule is enabled. |
| `cloudflare_r2_custom_domain.archive` | Only when `SCI_ARCHIVE_HOSTNAME` **and** `SCI_ARCHIVE_ZONE_ID` are set. |
| `cloudflare_r2_managed_domain.public` | The `r2.dev` development domain; enabled only when no archive hostname is configured. |
| `cloudflare_ruleset.archive_headers` | Optional (`SCI_ARCHIVE_NOSNIFF_RULE=true`, requires the zone ID): `X-Content-Type-Options: nosniff` on the archive hostname. |
| `digitalocean_project.sci` | DigitalOcean project `sci`. |
| `digitalocean_app.publisher` | Only when `SCI_DEPLOY_PUBLISHER=true`. Builds `server/Dockerfile` from the public repository (`git` source, default branch), port 8080, health check `/health`, one instance. |

Outputs (none secret): `instance_url`, `archive_url`, `temp_bucket_name`, `public_bucket_name`, `s3_endpoint`, `catalog_url`, `publisher_app_id`, `publishing_api_url`.

The publisher receives these App Platform environment variables:

| Variable | Value |
| --- | --- |
| `SCI_INSTANCE_URL` | Instance URL (with trailing slash). |
| `SCI_ARCHIVE_URL` | `https://<archive host>/publications`. |
| `SCI_DATABASE_URL` | `PUBLISHER_DATABASE_URL` (encrypted `SECRET`). |
| `SCI_S3_ENDPOINT` | `https://<CLOUDFLARE_ACCOUNT_ID>.r2.cloudflarestorage.com`. |
| `SCI_S3_REGION` | `auto`. |
| `SCI_S3_ACCESS_KEY_ID`, `SCI_S3_SECRET_ACCESS_KEY` | `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (encrypted `SECRET`). |
| `SCI_TEMP_BUCKET`, `SCI_PUBLIC_BUCKET` | Bucket names. |
| `SCI_CLIENT_IP_HEADER` | `do-connecting-ip`. |

### GitHub secrets

Repository → Settings → Secrets and variables → Actions → Secrets.

| Secret | Status | Value | Used by |
| --- | --- | --- | --- |
| `TF_API_TOKEN` | existing | HCP Terraform user token (passed as `TF_TOKEN_app_terraform_io`). | plan, apply, endpoint lookup |
| `CLOUDFLARE_API_TOKEN` | existing | R2 Admin Read & Write account token. | plan, apply |
| `DIGITALOCEAN_TOKEN` | existing | DigitalOcean token (account read, app CRUD, project CRUD + assign_resource, regions/actions/sizes read). | plan, apply, publisher |
| `SUPABASE_ACCESS_TOKEN` | existing | Project-scoped Supabase PAT. | plan, apply, migrate (pooler lookup), website (publishable-key lookup, only if `SUPABASE_PUBLISHABLE_KEY` is unset) |
| `SUPABASE_DB_PASSWORD` | existing | Postgres administrator password. | migrate only |
| `R2_ACCESS_KEY_ID` | create after the buckets exist | S3 Access Key ID of a bucket-scoped R2 **Object Read & Write** token for the two SCI buckets. | plan, apply (App Platform secret) |
| `R2_SECRET_ACCESS_KEY` | create after the buckets exist | The same token's S3 Secret Access Key. | plan, apply |
| `PUBLISHER_DATABASE_URL` | create before enabling the publisher | `postgresql://sci_publisher.<project-ref>:<url-encoded password>@<pooler host>:5432/postgres?sslmode=require` — the Supabase **session** pooler (port 5432 on `*.pooler.supabase.com`). Session mode is required: the publisher uses session advisory locks. Do not use the transaction pooler (port 6543). | plan, apply (App Platform secret); migrate (sets the `sci_publisher` password) |

### GitHub variables

Repository → Settings → Secrets and variables → Actions → Variables. Unset optional variables use the defaults shown.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | yes (existing) | — | R2 account. |
| `SUPABASE_ORGANIZATION_ID` | yes (existing) | — | Organization of the imported project. |
| `SUPABASE_PROJECT_REF` | yes (existing) | — | Existing project reference. |
| `TF_CLOUD_ORGANIZATION` | yes (existing) | — | HCP Terraform organization (read natively by Terraform). |
| `TF_WORKSPACE` | yes (existing) | — | HCP Terraform workspace (read natively by Terraform). |
| `SCI_INSTANCE_URL` | no | `https://<owner>.github.io/<repo>/` | Instance URL (website + `sci-server.json`). Set it if Pages uses a custom domain. |
| `SCI_TEMP_BUCKET` | no | `sci-temp` | Temporary bucket name. Never change after creation. |
| `SCI_PUBLIC_BUCKET` | no | `sci-publications` | Publication bucket name. Never change after creation. |
| `SCI_ARCHIVE_HOSTNAME` | no | empty (`r2.dev`) | Production archive hostname, e.g. `archive.example.org`. |
| `SCI_ARCHIVE_ZONE_ID` | no | empty | Zone ID of that hostname. Set it to let Terraform connect the domain; leave it empty if the domain was connected in the dashboard. |
| `SCI_ARCHIVE_NOSNIFF_RULE` | no | `false` | `true` adds the `nosniff` response-header transform rule (needs zone permissions, see below). |
| `SCI_DEPLOY_PUBLISHER` | no | `false` | `true` creates the App Platform app. Requires the three runtime secrets. |
| `SCI_PUBLISHER_APP_NAME` | no | `sci-publisher` | App Platform app name. |
| `SCI_PUBLISHER_REGION` | no | `nyc` | App Platform region; keep near Supabase (`nyc` for `us-east-1`). |
| `SCI_PUBLISHER_SIZE` | no | `apps-s-1vcpu-0.5gb` | App Platform instance size. |
| `SCI_DIGITALOCEAN_PROJECT` | no | `sci` | DigitalOcean project name. |
| `SUPABASE_REGION` | no | `us-east-1` | Region of the existing project (required by the provider schema; ignored after import). |
| `SUPABASE_PROJECT_NAME` | no | `sci` | Display name (required by the provider schema; ignored after import). |
| `SUPABASE_PUBLISHABLE_KEY` | no | looked up | Public key written to `sci-server.json`. If unset, the website job reads it from the Management API (`type: publishable`, else the legacy `anon` key). Never a secret/service-role key. |
| `SCI_ARCHIVE_URL` | no | Terraform output | Override for the descriptor's `archive_url`. |
| `SCI_PUBLISHING_API_URL` | no | Terraform output | Override for the descriptor's `publishing_api_url` (e.g. a custom publisher domain). |

`archive_url`, `publishing_api_url`, `catalog_url`, and the App Platform app ID are read from Terraform outputs by the `endpoints` job (`terraform output -json` against the HCP state, read-only, using only `TF_API_TOKEN`). The two `SCI_*_URL` variables override the outputs when set.

### One-time manual steps

1. **GitHub Pages**: Settings → Pages → Build and deployment → Source: **GitHub Actions**. The `github-pages` environment should only allow the `main` branch (the default for Actions-based Pages).
2. **`production` environment**: Settings → Environments → New environment `production`. Add **Required reviewers** (the people who approve infrastructure changes), optionally enable **Prevent self-review**, and under **Deployment branches and tags** select **Selected branches** → `main`. Only `infrastructure-apply` uses it.
3. **Protect `main`** (branch protection or ruleset) and limit write access. Repository secrets are available to any workflow that a user with write access runs on any branch; the workflow itself never exposes them to `push`/`pull_request` runs, and pull requests from forks never receive secrets. For stronger isolation, move the credentials into the `production` environment's secrets later.
4. **HCP Terraform**: keep the workspace in **Local** execution mode and restrict who can read state. State contains the App Platform secret values (database URL, R2 keys).
5. **Existing DigitalOcean project**: if a project named `sci` already exists, import it once from a local shell (see below): `terraform -chdir=deploy import digitalocean_project.sci <project-id>`.
6. **Supabase**: keep "Automatically expose new tables" disabled. Terraform sets the exposed schema to `catalog_v1` only.

### First deployment (bootstrap order)

1. Run **infrastructure-plan**. Expect: `import`/update of `supabase_project.sci` (no replacement), creation of `supabase_settings.sci`, both buckets and their settings, the R2 domain resources, and `digitalocean_project.sci`. There must be no delete.
2. Run **infrastructure-apply**, read the plan summary in the run, and approve the `production` deployment.
3. Run **migrate**. It creates the `sci` and `catalog_v1` schemas, grants, and the `sci_publisher` role. (If the Data API setting were rejected because `catalog_v1` did not exist yet, run `migrate` first and apply again.)
4. Create the bucket-scoped R2 token: R2 → Manage API Tokens → Create **Account API Token** (or user token) → **Object Read & Write**, applied to the two SCI buckets only. Save its Access Key ID and Secret Access Key as `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`.
5. Generate a strong `sci_publisher` password and save `PUBLISHER_DATABASE_URL` (format above; take the pooler host from Supabase → Connect → Session pooler, and URL-encode the password). Run **migrate** again; it passes the password to the migration runner as `SCI_PUBLISHER_DB_PASSWORD`, which enables login for `sci_publisher`.
6. Set `SCI_DEPLOY_PUBLISHER=true`, run **infrastructure-apply** again. Terraform creates the app and waits for its first deployment (so `server/Dockerfile` and `/health` must work).
7. Run **website** (or **release**). Then enable author keys with the whitelist helpers in the Supabase SQL editor.
8. For production, connect the archive custom domain: set `SCI_ARCHIVE_HOSTNAME` and `SCI_ARCHIVE_ZONE_ID`, run infrastructure-apply (this disables `r2.dev` and changes `SCI_ARCHIVE_URL` on the publisher), then **website**. If the R2-only token cannot manage the zone, connect the domain in the dashboard (R2 → bucket → Settings → Custom Domains), leave `SCI_ARCHIVE_ZONE_ID` empty, and set only `SCI_ARCHIVE_HOSTNAME`.

### Workflow operations

Run Actions → **deploy** → **Run workflow**, choose the branch (`main` for everything except plans) and the `operation`.

| Operation | What it does |
| --- | --- |
| `test` | Also runs automatically on push and pull request to `main`, with no secrets. Installs Python 3.12, downloads PostgREST v12.2.12 and RustFS 1.0.3 (SHA-256 pinned) and exports their paths as `SCI_TEST_POSTGREST` and `SCI_TEST_RUSTFS`, runs `pip install -e '.[server,test]'` and `pytest -q`. PostgreSQL comes from the `pgserver` package. |
| `infrastructure-plan` | `terraform init` + `plan -out` against the HCP workspace. The human-readable plan is discarded and the plan file is never uploaded; the job log and summary show only `actions  address  changed: <attribute names>` per resource and changed output names. Runs on any branch, so infrastructure changes can be previewed before merge. |
| `infrastructure-apply` | Runs the same plan job, then a second job bound to the `production` environment (required reviewers approve after reading the plan summary). That job plans again, checks that the change summary is identical to the reviewed one (otherwise it fails), and applies that exact saved plan file. `main` only. |
| `migrate` | Looks up the session pooler through `GET /v1/projects/{ref}/config/database/pooler` (falls back to `db.<ref>.supabase.co:5432`, which is IPv6-only on most plans), builds a masked admin DSN for `postgres.<ref>` with `SUPABASE_DB_PASSWORD` and `sslmode=require`, and runs `python -m server.database migrate` with `SCI_ADMIN_DATABASE_URL`, optional `SCI_PUBLISHER_DB_PASSWORD` (parsed from `PUBLISHER_DATABASE_URL`), `SCI_INSTANCE_URL`, and `SCI_ARCHIVE_URL`. `main` only. |
| `publisher` | Finds the App Platform app (Terraform output, else by name), creates a deployment with `force_build`, polls until `ACTIVE` (fails on `ERROR`/`CANCELED`/`SUPERSEDED`), warns if the built commit differs from the workflow commit, and checks that `GET <publishing_api_url>/health` returns 200. `main` only. |
| `website` | Copies `web/` to `_site/`, adds `sci/categories.json`, `sci/publication_LICENSE.txt`, and a generated `sci-server.json` (`schema_version`, `publishing_api_url`, `catalog_url`, `catalog_schema: catalog_v1`, `archive_url`, `supabase_publishable_key`), validates that all URLs are HTTPS and that the key is a publishable/anon key, deploys to GitHub Pages, then fetches the descriptor (must match), `index.html`, and `publication/` from the instance URL. `main` only. |
| `release` | `test` → `migrate` → `publisher` → `website`, stopping at the first failure. |

All manual operations share one concurrency group (`sci-production-deploy`), so deployments never overlap; GitHub keeps at most one queued run and cancels older queued runs.

### Local Terraform usage

Use the same inputs as the workflow; a missing `TF_VAR_deploy_publisher=true` would plan to **destroy the publisher app**. Read secrets without echoing them (`read -rs VAR`), and never commit or share plan files.

```sh
export TF_CLOUD_ORGANIZATION=azaho-sci
export TF_WORKSPACE=sci-production
terraform login                       # or: read -rs TF_TOKEN_app_terraform_io; export TF_TOKEN_app_terraform_io

read -rs CLOUDFLARE_API_TOKEN; export CLOUDFLARE_API_TOKEN
read -rs DIGITALOCEAN_TOKEN; export DIGITALOCEAN_TOKEN
read -rs SUPABASE_ACCESS_TOKEN; export SUPABASE_ACCESS_TOKEN

export TF_VAR_instance_url=https://azaho.github.io/sci/
export TF_VAR_cloudflare_account_id=<CLOUDFLARE_ACCOUNT_ID>
export TF_VAR_supabase_organization_id=<SUPABASE_ORGANIZATION_ID>
export TF_VAR_supabase_project_ref=<SUPABASE_PROJECT_REF>
export TF_VAR_repository_clone_url=https://github.com/azaho/sci.git
# Match any repository variables you set, e.g.:
# export TF_VAR_archive_hostname=archive.example.org TF_VAR_archive_zone_id=<zone id>
# export TF_VAR_deploy_publisher=true
# read -rs TF_VAR_publisher_database_url; export TF_VAR_publisher_database_url
# read -rs TF_VAR_r2_access_key_id; export TF_VAR_r2_access_key_id
# read -rs TF_VAR_r2_secret_access_key; export TF_VAR_r2_secret_access_key

terraform -chdir=deploy init
terraform -chdir=deploy plan -out=tfplan
terraform -chdir=deploy apply tfplan
rm -f deploy/tfplan
```

| Terraform variable | GitHub source |
| --- | --- |
| `instance_url` | `SCI_INSTANCE_URL` / default |
| `cloudflare_account_id` | `CLOUDFLARE_ACCOUNT_ID` |
| `temp_bucket_name`, `public_bucket_name` | `SCI_TEMP_BUCKET`, `SCI_PUBLIC_BUCKET` |
| `archive_hostname`, `archive_zone_id`, `archive_nosniff_rule` | `SCI_ARCHIVE_HOSTNAME`, `SCI_ARCHIVE_ZONE_ID`, `SCI_ARCHIVE_NOSNIFF_RULE` |
| `supabase_organization_id`, `supabase_project_ref`, `supabase_project_name`, `supabase_region` | `SUPABASE_ORGANIZATION_ID`, `SUPABASE_PROJECT_REF`, `SUPABASE_PROJECT_NAME`, `SUPABASE_REGION` |
| `digitalocean_project_name` | `SCI_DIGITALOCEAN_PROJECT` |
| `deploy_publisher`, `publisher_app_name`, `publisher_region`, `publisher_size` | `SCI_DEPLOY_PUBLISHER`, `SCI_PUBLISHER_APP_NAME`, `SCI_PUBLISHER_REGION`, `SCI_PUBLISHER_SIZE` |
| `repository_clone_url`, `repository_branch` | the repository and its default branch |
| `publisher_database_url`, `r2_access_key_id`, `r2_secret_access_key` (sensitive) | secrets `PUBLISHER_DATABASE_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` |

The `r2_location` variable (default `enam`) is only a creation-time location hint.

### Notes and provider limitations

- **Supabase project import.** The provider requires `database_password`; the configuration uses a fixed placeholder that is ignored (`ignore_changes`), so the real administrator password is never given to Terraform. Name, region, organization, and instance size are also ignored. `prevent_destroy` blocks any plan that would delete the project. Deleting `supabase_settings.sci` from Terraform does not revert the Data API settings.
- **Bucket lock.** Retention is indefinite, matching publication immutability. The publisher must never overwrite objects under `publications/` (retries must check for an existing identical object rather than rewrite it). To remove a publication, an administrator disables the rule in the dashboard, deletes the objects, and runs infrastructure-apply to restore the rule. Locks are not backups.
- **Temporary bucket.** No public access and no age-based deletion; only incomplete multipart uploads are aborted after one day. This rule replaces R2's default 7-day multipart rule on that bucket.
- **Archive domain.** `r2.dev` is rate-limited and intended for development; use a custom domain in production. `cloudflare_r2_custom_domain` cannot be imported, and the R2 Admin token may lack the zone permissions needed to connect a domain or create the `nosniff` transform rule. In that case connect the domain in the dashboard and leave `SCI_ARCHIVE_ZONE_ID` empty, or use a token with the zone's Transform Rules (and DNS) edit permission for `SCI_ARCHIVE_NOSNIFF_RULE`. The `nosniff` ruleset is the zone's entry-point ruleset for the response-header phase; if the zone already has one, import it (`terraform import 'cloudflare_ruleset.archive_headers[0]' zones/<zone_id>/<ruleset_id>`) or add the rule in the dashboard.
- **App Platform secrets.** DigitalOcean returns `SECRET` environment values encrypted, so plans can show an in-place update of `digitalocean_app.publisher[0]` (`changed: spec`) even without changes; applying it redeploys the same configuration.
- **App Platform source.** The `git` source builds the head of the default branch when a deployment starts, not necessarily the workflow commit; the publisher job warns when they differ. No container registry access is needed.
- **Plan secrecy.** Plans are never uploaded as artifacts and the human-readable plan is not printed. Reviewers see resource addresses, actions, and changed attribute names. Inspect attribute values locally if needed.
- **Supabase SSL enforcement** is not enabled by Terraform. All workflow and publisher connections use `sslmode=require`; enabling enforcement later is optional.
- **Token expiry.** When a 30-day management token expires, plan/apply or the affected job fails with an authorization error; rotate the secret and rerun.
