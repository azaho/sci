# Hosted setup

This guide describes the online setup for a public SCI installation. It covers accounts, deployment credentials, infrastructure, the first release, and routine upkeep. It contains no installation-specific account names, resource IDs, domains, or credentials. Replace placeholders with your own values.

The system requirements are in [DESIGN.md](DESIGN.md). Release checks are in [TEST_PLAN.md](TEST_PLAN.md).

The application, Terraform configuration, and deployment workflow are included in the repository. This guide covers the account setup and first deployment using that workflow. Completing account and token setup does not mean the platform is deployed or its runtime credentials have been tested. See [README.md](README.md#hosted-deployment) for the full deployment configuration reference.

## Deployment sequence

Follow the numbered sections in order. This procedure uses a public GitHub repository, GitHub Pages at `https://sci.example.org/`, an R2 archive at `https://archive.sci.example.org/publications`, and the publisher's DigitalOcean HTTPS hostname. Replace `example.org` with a domain you control in Cloudflare. Configure these final URLs from the start.

| Stage | Action | Evidence before continuing |
| --- | --- | --- |
| Accounts and source | Prepare GitHub, Supabase, R2, DigitalOcean, and HCP Terraform (§§1–6). | Code is on `main`; tests pass; billing is ready; database and state workspace exist. |
| Credentials and configuration | Save six initial secrets, including the publisher database URL, and set the final URL variables (§§7–8). | Names and scopes match this guide; expiration dates are recorded privately. |
| Database and infrastructure | Run migrate once, then plan → reviewed apply with the publisher disabled (§9). | Schema and publisher login are ready; both buckets and the DigitalOcean project exist. |
| Archive and runtime storage access | Attach the archive domain and create its bucket-scoped S3 key pair (§10). | Archive HTTPS works; all eight secrets are saved. |
| Publisher and site | Enable the publisher; run plan → reviewed apply → website (§11). | App is active, health is good, and the website and descriptor use the final URLs. |
| Acceptance and upkeep | Enable an author key, test sign → push → pull, complete release checks, and establish backups and reminders (§§11–12). | Exact bytes survive the round trip; launch checks and upkeep are recorded. |

The two infrastructure applies are intentional: the buckets must exist before issuing their scoped runtime key, and the publisher is created only after its credentials and database are ready. Setup does not require token replacement, app recovery, endpoint overrides, or a later domain migration. Those procedures belong to maintenance or the troubleshooting appendix.

## 1. Understand the services

| Service | Responsibility |
| --- | --- |
| GitHub | Source repository, GitHub Actions, and the static website on GitHub Pages. |
| Supabase | PostgreSQL, operational state, and the read-only catalog through its Data API. |
| Cloudflare R2 | Original signed publications and private temporary uploads. |
| DigitalOcean App Platform | The Python publishing service and its recovery work. |
| HCP Terraform | Persistent Terraform state, locking, and state history. |

Readers use GitHub Pages, the catalog, and R2 directly. Publishing requires the Python service. Author signing keys are separate from all infrastructure credentials.

Set up the accounts and credentials first. Create the cloud resources through reviewed Terraform changes once the configuration is ready. If a resource already exists, import it where supported before Terraform manages it.

## 2. Prepare GitHub and choose URLs

1. Fork or copy the **complete implemented repository** into a public repository you administer. A repository containing only the design documents is insufficient.
2. Use `main` as the default branch for this procedure. Confirm `.github/workflows/deploy.yml`, `deploy/`, `server/`, `sci/`, and `web/` are present on it. Enable GitHub Actions if prompted for a fork, and run **Actions → deploy → Run workflow → operation: test**. Keep `DESIGN.md`, `TEST_PLAN.md`, and this guide with the code. Deployment tooling runs in Actions; you do not need local Terraform or Docker for this path.
3. Choose `https://sci.example.org/` as the final instance URL and `archive.sci.example.org` as the archive hostname, replacing the example domain with yours.
4. Use the DigitalOcean app's generated HTTPS hostname for the publisher. Terraform will supply it to the website automatically.

In **Settings → Pages → Build and deployment**, set **Source: GitHub Actions**. Save `sci.example.org` as the **Custom domain** before adding its DNS record in §4. Enable **Enforce HTTPS** when certificate provisioning makes it available.

Create a GitHub environment named **`production`** under **Settings → Environments**. Configure **Required reviewers** and restrict deployment branches to `main`. The infrastructure apply job waits for approval in this environment. Reviewers should read the plan summary from the same workflow run before approving. Keep the `github-pages` environment restricted to `main` as well.

Protect `main` and limit repository write access. The current workflow uses repository secrets, so the `production` environment's approval gate applies to infrastructure apply, not every operation that can use a secret.

Do not commit environment-specific configuration, passwords, tokens, private signing keys, SQL backups, Terraform state, or saved Terraform plans. Plans and state can contain secrets even when terminal output marks fields as sensitive. Commit `deploy/.terraform.lock.hcl` once generated; it is the provider dependency lockfile, not state.

## 3. Create the Supabase organization and project

1. Sign in at the [Supabase dashboard](https://supabase.com/dashboard).
2. Create an organization. Choose the organization type appropriate to its owner.
3. Start with **Free** for development and testing. Review [current pricing and limits](https://supabase.com/pricing) before public launch. Free projects can pause after inactivity and do not include automatic database backups.
4. Create a project with a stable name, such as `sci`.
5. Generate a strong database password and save it in a password manager. This is the PostgreSQL administrator password, not the dashboard login password.
6. Choose a specific region near the intended publisher. For example, a US East database can be paired with a nearby DigitalOcean region.
7. Keep **Enable Data API** enabled.
8. Disable **Automatically expose new tables**. Migrations will explicitly configure the public catalog schema, grants, and row-level security.
9. Leave the optional GitHub integration unconfigured for this workflow. The repository's deployment workflow will control migrations.
10. Create the project and wait for its status to become healthy.
11. Record the organization ID and project reference in the GitHub variables listed below.

The database already exists after this step. The first Terraform setup must import that project rather than create another one. Keep its region and identity stable.

## 4. Activate Cloudflare R2

1. Sign in at the [Cloudflare dashboard](https://dash.cloudflare.com).
2. Confirm the domain intended for the archive is available in the account.
3. Open **Storage & databases → R2 Object Storage**.
4. Review the subscription terms, payment method, free allowances, and usage charges, then activate R2.
5. Record the Cloudflare account ID as a GitHub variable.
6. In the domain's Cloudflare DNS zone, add a **CNAME** named `sci` targeting `<owner>.github.io`, with **DNS only** and **TTL Auto**. The target has no repository path. Keep any personal site's apex and `www` records unchanged.
7. Leave bucket creation to Terraform. The archive domain is attached after the publication bucket exists (§10).

R2 activation enables usage billing; the free allowance is not a hard spending cap. Review [R2 pricing](https://developers.cloudflare.com/r2/pricing/) and configure a budget alert.

The deployment will need two buckets: a **private temporary bucket** and a **public publication bucket**. Do not enable public access on the temporary bucket. Use a custom domain for the production archive; the `r2.dev` endpoint is intended for development. See [public bucket configuration](https://developers.cloudflare.com/r2/buckets/public-buckets/).

## 5. Prepare DigitalOcean

1. Sign in at the [DigitalOcean control panel](https://cloud.digitalocean.com).
2. Select the team that should own and pay for the installation.
3. Complete any required account verification and billing setup.
4. Check existing resources so the SCI setup does not accidentally take ownership of unrelated infrastructure.
5. Leave creation of the SCI project and App Platform app to Terraform.

SCI uses **App Platform**, not a new Droplet or a DigitalOcean managed database. Select the app's region near Supabase. Choose the service size after local tests and a review of [App Platform pricing](https://docs.digitalocean.com/products/app-platform/details/pricing/).

## 6. Create the Terraform state workspace

1. Create an account at [HCP Terraform](https://app.terraform.io).
2. Create an organization with a unique name. Choose **Personal** for a personal project or **Business** for company/institution ownership; the organization type may not be editable later.
3. Create a workspace with a stable name, such as `sci-production`.
4. Choose **CLI-Driven Workflow**.
5. In **Workspace → Settings → General**, select **Local (custom)** execution mode and save.
6. Keep state sharing restricted to explicitly selected workspaces; no other workspace needs access for the pilot.
7. Record the organization and workspace names as GitHub variables.

In local execution mode, Terraform runs in GitHub Actions or on an operator's machine. HCP Terraform stores state. Its workspace variables are not supplied to local runs, so credentials must come from the runner environment. Plan review and apply approval must be implemented in GitHub Actions; an HCP auto-apply setting does not enforce that review. See [workspace settings](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/settings).

Every deployment of this installation must use the same organization and workspace. Changing either can make Terraform see an empty state and propose duplicate infrastructure.

## 7. Save deployment and database credentials

Open **Repository → Settings → Secrets and variables → Actions → New repository secret**.

For each secret, use the exact name below, paste the value directly from the provider or password manager, and click **Add secret**. Never put values in issues, chat, source files, screenshots, or command-line arguments.

Save these six secrets before the first migration:

| GitHub Actions secret | Value to store | Purpose |
| --- | --- | --- |
| `TF_API_TOKEN` | HCP Terraform user API token | Authenticate to the Terraform state workspace. |
| `CLOUDFLARE_API_TOKEN` | R2 account API **Token value** | Create and configure R2 infrastructure. This is not an S3 Access Key ID or Secret Access Key. |
| `DIGITALOCEAN_TOKEN` | DigitalOcean personal access token | Manage the SCI App Platform deployment and project. |
| `SUPABASE_ACCESS_TOKEN` | Supabase scoped personal access token | Configure the existing project and perform authorized management operations. |
| `SUPABASE_DB_PASSWORD` | The existing project's PostgreSQL administrator password | Connect for database bootstrap and migrations. |
| `PUBLISHER_DATABASE_URL` | Session-pooler URL with a separate password for `sci_publisher` (instructions below). | Set the publisher role password during migration and configure the app. |

The included workflow already maps these secrets to the relevant tools, including `TF_API_TOKEN` to Terraform authentication and the administrator password to database migrations. No manual environment-variable wiring is needed.

### HCP Terraform token

1. Open **Account settings → Tokens → Create an API token**.
2. Use a descriptive name, such as `SCI GitHub Actions deployment`.
3. Choose a **30-day** lifetime for the initial setup.
4. Generate the token and save it as `TF_API_TOKEN`.

A user token carries the user's Terraform permissions. It is not inherently restricted to one workspace. Keep the user and organization access appropriate to the installation.

### Cloudflare infrastructure token

1. Open **R2 Object Storage → Manage API Tokens → Create Account API Token**.
2. Name it, for example, `SCI Terraform deployment`.
3. Select **Admin Read & Write**.
4. Choose a **30-day** lifetime.
5. Generate the token and save its **Token value** as `CLOUDFLARE_API_TOKEN`.

This permission can create, configure, and delete buckets and access their objects across the account. It is the infrastructure credential, not the publisher's runtime credential. R2 credentials do not provide general DNS administration. Any additional DNS or response-header configuration must use appropriately scoped zone access or be configured separately. See [R2 authentication](https://developers.cloudflare.com/r2/api/tokens/).

### DigitalOcean deployment token

1. Open **API → Tokens → Generate New Token**.
2. Name it, for example, `SCI deployment`.
3. Choose **30 days** and **Custom Scopes**.
4. Select the following permissions, including required dependencies shown by the provider:

| Resource | Permissions |
| --- | --- |
| `account` | `read` |
| `app` | `create`, `read`, `update`, `delete` |
| `project` | `create`, `read`, `update`, `delete`, `assign_resource` |
| `regions` | `read` |
| Required app dependencies | `actions:read`, `sizes:read` |

5. Generate the token and save it as `DIGITALOCEAN_TOKEN`.

These scopes apply across the selected team, not only to a future SCI app. App console access and Droplet modification permissions are not included. See [DigitalOcean token scopes](https://docs.digitalocean.com/reference/api/scopes/).

### Supabase deployment token

1. Open **Account → Access Tokens → Generate new token**.
2. Name it, for example, `SCI deployment`, and choose **30 days**.
3. Select **Project** resource access, then the intended organization and existing SCI project.
4. Use custom permissions:

| Permission | Access |
| --- | --- |
| Project Settings | Read-write |
| Database | Read-write |
| Database Config | Read-write |
| Migrations | Read-write |
| Data API Config | Read-write |
| Connection Pooling | Read |
| API Keys | Read |
| API Key Secrets | Read |
| Add-ons (under Infrastructure and delivery) | Read |

5. Leave all other permissions at **None**. Review access: the list above grants five read-write capabilities and four read capabilities on the selected project.
6. Create the token and save it as `SUPABASE_ACCESS_TOKEN`.

Include **Add-ons: Read** (`infra_add_ons_read`) even for a Free project: the pinned Terraform provider reads add-ons during import and refresh. These project-scoped permissions are administrative credentials, not a frontend key. See [Supabase personal access tokens](https://supabase.com/docs/guides/platform/personal-access-tokens) and the [provider's project resource](https://github.com/supabase/terraform-provider-supabase/blob/v1.11.0/internal/provider/project_resource.go).

### Database password

Save the password created in step 3 as `SUPABASE_DB_PASSWORD`. Do not create a second password merely to populate this secret. The administrator password is for bootstrap and migrations; the running publisher must use a separate database role.

### Publisher database URL

1. Generate a new, strong password for `sci_publisher` and save it in your password manager. A randomly generated 32-character password using letters and numbers avoids URL-encoding issues. Do not reuse the PostgreSQL administrator password.
2. Open the Supabase project's **Connect** dialog and select **Session pooler**. Copy the actual pooler host; do not infer it from the project's region.
3. Construct the following connection string, replacing all placeholders:

   ```text
   postgresql://sci_publisher.<project-ref>:<url-encoded-password>@<session-pooler-host>:5432/postgres?sslmode=require
   ```

4. Save the complete string as the GitHub Actions repository secret **`PUBLISHER_DATABASE_URL`**. If the password contains reserved URL characters, percent-encode the password component locally; never send it to an online encoding website.
5. Keep this secret saved for the migration in §9. That migration creates `sci_publisher` and sets its password in one run; the role does not need to exist when you construct the URL.

Use session pooling on port **5432**, not transaction pooling on port **6543**: the publisher uses session advisory locks. The dedicated role has publication permissions and whitelist reads; it must not administer the whitelist.

## 8. Add non-secret GitHub variables

Open **Repository → Settings → Secrets and variables → Actions → Variables → New repository variable**.

| Variable | Value |
| --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID shown on the R2 overview page. |
| `SUPABASE_ORGANIZATION_ID` | Organization ID/slug used by Supabase management APIs. |
| `SUPABASE_PROJECT_REF` | Existing project's reference, not its display name. |
| `TF_CLOUD_ORGANIZATION` | HCP Terraform organization name. |
| `TF_WORKSPACE` | The installation's HCP Terraform workspace name. |

These values are identifiers rather than credentials, but keep real installation values out of this general guide.

For this procedure, set `SCI_INSTANCE_URL=https://sci.example.org/` and `SCI_ARCHIVE_HOSTNAME=archive.sci.example.org` now. Keep `SCI_DEPLOY_PUBLISHER=false` until §11. Leave `SCI_ARCHIVE_ZONE_ID` unset, `SCI_ARCHIVE_NOSNIFF_RULE` unset or `false`, and both endpoint overrides unset. The archive domain will be connected in the dashboard, so the R2 administration token needs no broader zone permissions.

Set the existing Supabase project's actual name and region. Review the remaining defaults before the first apply:


| Variable | Default / use |
| --- | --- |
| `SCI_INSTANCE_URL` | Defaults to the repository's standard GitHub Pages URL. Set the actual URL when using a custom domain. |
| `SCI_TEMP_BUCKET` | `sci-temp`; private uploads. Keep stable after creation. |
| `SCI_PUBLIC_BUCKET` | `sci-publications`; publication archive. Keep stable after creation. |
| `SCI_DEPLOY_PUBLISHER` | `false` during initial provisioning; set to `true` only after the runtime secrets and database login are ready. |
| `SUPABASE_REGION` | `us-east-1`; set to the actual existing project's region. |
| `SUPABASE_PROJECT_NAME` | `sci`; set to the existing project's display name. Terraform ignores name/region changes after import. |
| `SCI_DIGITALOCEAN_PROJECT` | `sci`; use a distinct project name for a separate installation. |
| `SCI_PUBLISHER_APP_NAME` | `sci-publisher`; use a distinct app name for a separate installation. |
| `SCI_PUBLISHER_REGION` | `nyc`; choose near the database. |
| `SCI_PUBLISHER_SIZE` | `apps-s-1vcpu-0.5gb`; review current App Platform pricing before applying. |
| `SCI_ARCHIVE_HOSTNAME` | Custom archive hostname, such as `archive.example.org`. Empty uses the development `r2.dev` domain. |
| `SCI_ARCHIVE_ZONE_ID` | Set only when Terraform should connect the custom domain; leave empty when connecting it manually. |
| `SCI_ARCHIVE_NOSNIFF_RULE` | `false`; enabling the response-header rule requires additional zone permissions. |
| `SUPABASE_PUBLISHABLE_KEY` | Public frontend key; the website workflow looks it up when unset. Never use a secret or service-role key here. |
| `SCI_ARCHIVE_URL` | Normally unset; overrides the descriptor's archive URL from Terraform. Includes `/publications`. |
| `SCI_PUBLISHING_API_URL` | Normally unset; overrides the descriptor's publisher URL from Terraform. |

See [the complete variable reference](README.md#github-variables) for additional names and overrides. These are GitHub Actions **variables**, not secrets.

Use Terraform's endpoint outputs for this setup. The override variables are for separately designed configurations and are not needed here.

At this point, account and credential bootstrap is complete. Secret presence confirms only that GitHub stored a value; it does not prove that the token is valid, correctly scoped, or unexpired.

## 9. Initialize the database and provision infrastructure

Run operations from **Actions → deploy → Run workflow**, selecting branch **`main`** and the named **operation**. Pull requests run tests only. Pushes to `main` run tests and automatically publish the website after they pass; they do not run migrations, rebuild the publisher, or deploy infrastructure. Initial provisioning still follows the manual steps below.

1. Confirm the six secrets from §7 and variables from §8 are saved. Keep `SCI_DEPLOY_PUBLISHER=false`.
2. Run **`migrate`** and wait for success. It creates the schemas, catalog views, grants, and `sci_publisher` role, and sets the role's password from `PUBLISHER_DATABASE_URL`. Running it before Terraform configures the Data API ensures `catalog_v1` already exists. The workflow supports an empty Terraform workspace at this stage; endpoint outputs are not needed to create the schema and login.
3. Run **`infrastructure-plan`**. The configuration imports the existing Supabase project and creates its Data API settings, both R2 buckets and their settings, and the DigitalOcean project. It must not create the publisher or replace the database. With the final archive hostname already configured, the development `r2.dev` endpoint remains disabled.
4. Review resource identities and changes. There should be no unexpected deletion or replacement. The Supabase project and both buckets have `prevent_destroy`. If reusing an existing DigitalOcean project, import it as described in [README.md](README.md#hosted-deployment) before proceeding.
5. Run **`infrastructure-apply`**. Read this run's plan summary, then use **Review deployments** to approve the **`production`** job. Wait for apply to succeed.

The temporary bucket stays private. Published objects have an indefinite retention lock. Temporary cleanup must preserve sources needed by committed finalizations; do not add a blanket age-based object deletion rule.

The workflow prints resource actions and changed attribute names rather than raw plan values, and removes its saved plan file. HCP state contains sensitive values once runtime credentials reach App Platform. Never use a production destroy operation to stop service or reduce costs.

## 10. Connect the archive and create its runtime key

### Archive custom domain

1. In **R2 → publication bucket → Settings → Custom Domains → Add**, enter `archive.sci.example.org`, choose minimum TLS **1.2**, and connect it. Cloudflare creates the DNS record. Never connect a public domain to the temporary bucket.
2. Wait for the archive domain to become **Active**, then verify HTTPS independently. An empty bucket may return HTTP 404; certificate or handshake errors mean it is not ready.
3. Confirm `SCI_ARCHIVE_HOSTNAME` matches this hostname and `SCI_ARCHIVE_ZONE_ID` remains unset. No extra Terraform apply or website deployment is needed here; the final configuration was already set in §8.

The website uses GitHub Pages with Cloudflare DNS; the archive uses R2 through Cloudflare. DNS and certificate provisioning can take different amounts of time. Continue to §11 when archive HTTPS works and the website's DNS points to GitHub; check the site's certificate before testing the deployed website. See [GitHub's custom-domain procedure](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site) and [Cloudflare's public-bucket guide](https://developers.cloudflare.com/r2/buckets/public-buckets/).

### R2 runtime key pair

1. In Cloudflare, open **R2 Object Storage → Manage API Tokens → Create Account API Token**.
2. Use a descriptive name, such as `SCI publisher runtime`.
3. Select **Object Read & Write** and **Apply to specific buckets only**.
4. Select both `sci-temp` and `sci-publications`, or the actual names configured in section 8. Do not select all buckets.
5. Choose **30 days** for the pilot and record the expiration privately. This is a live service credential: publishing will stop working if it expires before rotation. Choose a different lifetime only with a corresponding rotation plan.
6. Create the token and save its S3 **Access Key ID** and **Secret Access Key** in your password manager.
7. In GitHub Actions repository secrets, add `R2_ACCESS_KEY_ID` with the Access Key ID and `R2_SECRET_ACCESS_KEY` with the Secret Access Key.

Use those two S3 fields, not Cloudflare's separate **Token value**. Keep `CLOUDFLARE_API_TOKEN` as the infrastructure administration credential. The runtime token does not need bucket-administration permissions.

The Access Key ID and Secret Access Key must come from the same token creation. Store both privately before leaving the success screen. SCI does not use this runtime token's separate **Token value**.

Terraform maps these GitHub secrets to encrypted App Platform runtime variables:

| GitHub secret | Publisher runtime variable |
| --- | --- |
| `R2_ACCESS_KEY_ID` | `SCI_S3_ACCESS_KEY_ID` |
| `R2_SECRET_ACCESS_KEY` | `SCI_S3_SECRET_ACCESS_KEY` |
| `PUBLISHER_DATABASE_URL` | `SCI_DATABASE_URL` |

These values also enter Terraform state, so restrict HCP state access. A successful plan verifies configuration, not that the R2 keys or publisher login work; test them after deployment.

The completed installation has **eight repository secrets**: `TF_API_TOKEN`, `CLOUDFLARE_API_TOKEN`, `DIGITALOCEAN_TOKEN`, `SUPABASE_ACCESS_TOKEN`, `SUPABASE_DB_PASSWORD`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `PUBLISHER_DATABASE_URL`. The R2 runtime token contributes two secret fields. The author's signing private key is not a deployment secret and must not be added to GitHub Actions.

## 11. Deploy and verify the first release

1. Confirm all eight secrets are saved, the migration succeeded, the final domains are configured, and archive HTTPS works.
2. Under **Actions → Variables**, set **`SCI_DEPLOY_PUBLISHER=true`**.
3. Run **`infrastructure-plan`**. After initial provisioning, the expected new resource is `digitalocean_app.publisher[0]`, plus its outputs. Investigate any unexpected changes.
4. Review the selected app size and [current DigitalOcean pricing](https://docs.digitalocean.com/products/app-platform/details/pricing/). Running a plan does not create the app; applying it starts the paid service.
5. Run **`infrastructure-apply`**, review that run's plan summary, and approve **`production`**. Wait for App Platform's initial build and deployment to succeed, then check the publisher's `/health` endpoint. Leave `SCI_DEPLOY_PUBLISHER=true` afterward; setting it back to `false` would propose deleting the app on the next apply.
6. Run **`website`** to publish `web/` and the generated `sci-server.json` through GitHub Pages at the final instance URL. In **Settings → Pages**, enable **Enforce HTTPS** when available. Verify HTTPS for `/`, `/publication/`, and `/sci-server.json`.
7. Check that the descriptor contains the actual HTTPS publisher, catalog, and archive URLs, `catalog_schema`, `schema_version`, and the public `supabase_publishable_key`. Its `archive_url` must end at the `publications/` base path. Include no private credentials.
8. Enable the author's public key and run the live publication test below. Keep the signing private key on the author's machine.
9. Complete [TEST_PLAN.md](TEST_PLAN.md), including HTTPS, CORS, catalog permissions, real R2 upload-size enforcement, conditional copies, interrupted finalization, whitelist enforcement, backup restoration, archive recovery, and preservation across redeployment. Resolve the upload-handler versus limited-pilot choice in `DESIGN.md` if real R2 does not enforce the intended upload cap.
10. Import existing research only after those checks pass and the import-attestation format is defined.

Choose the final instance URL before the first real publication: acceptance records retain their historical instance URL.

Test public reads while the publisher is stopped. Test pulling a known publication with the catalog unavailable. Neither case should depend on private deployment credentials.

### Check live readiness

Get the publisher URL from the App Platform dashboard or `sci-server.json`, then open its `/health` endpoint. A healthy writable installation reports:

```json
{
  "status": "ok",
  "version": "<deployed-version>",
  "writes_enabled": true,
  "database": "ok",
  "storage": {
    "<temporary-bucket-name>": "ok",
    "<publication-bucket-name>": "ok"
  }
}
```

HTTP 200 can also contain `status: degraded` and storage errors. The deployment deliberately avoids restart loops for R2 failures, so inspect the response body. These checks exercise database connectivity and authenticated storage reads; they do not prove uploads, conditional copies, retention locks, CORS, or download integrity. The live test below is still required.

### First live publication and verified download

Use deliberately public test content that may remain permanently in the archive. Signing accepts the publication's fixed reuse terms. Run this only after choosing the final instance URL and preparing the storage and permission checks in `TEST_PLAN.md`; a basic round trip is not the full release test.

On the author's machine, install Python **3.10 or newer**, clone the deployed repository, and run these commands from its root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
sci server https://sci.example.org/
sci keygen
```

Replace the example URL. Run `keygen` only if this author does not already have a signing key; it refuses to overwrite existing files. Back up the private key securely outside the repository and publication folder. The default directory is `~/.sci/`. If using an existing key, its public value is the `public_key.x` field of a signed manifest; do not copy the private key.

In **Supabase → SQL Editor**, using the administrator (`postgres`) role, enable the **unpadded base64url public key** printed by `keygen`:

```sql
select * from sci.enable_key('<public-key-base64url>');
select * from sci.list_keys();
```

Use neither the PEM file nor the key fingerprint as this argument. Whitelist changes apply without redeploying. Later, `select * from sci.disable_key('<public-key-base64url>');` disables new publishing by that author.

Create a new test folder outside the repository so it cannot accidentally be committed:

```sh
mkdir ../sci-live-check
cd ../sci-live-check
mkdir publication
cat > publication/sci.json <<'JSON'
{
  "title": "SCI deployment verification record",
  "abstract": "Public test content for checking signing, publication, and verified download. This record makes no research claim."
}
JSON
cat > publication/README.md <<'MARKDOWN'
# Deployment verification

This is intentionally public test content for this SCI installation.
MARKDOWN
sci sign ./publication
sci verify ./publication
sci push ./publication
```

Record the UUID and accepted publication URL. Open the website and that publication page; check that it appears without rebuilding the website. Then use the printed UUID:

```sh
sci pull <UUID> --output ./downloaded
sci verify ./downloaded
python - <<'PY'
import json
from pathlib import Path

source = Path("publication")
copy = Path("downloaded")
manifest = json.loads((source / "manifest.json").read_text())
names = set(manifest["files"]) | {"sci.json", "manifest.json", "manifest.json.sig"}
for name in sorted(names):
    if (source / name).read_bytes() != (copy / name).read_bytes():
        raise SystemExit(f"Bytes differ: {name}")
print(f"Exact bytes match for all {len(names)} signed publication files.")
PY
```

The `<UUID>` command placeholder must be replaced before running. `pull` validates the archive's `acceptance.json` but does not save it in the downloaded folder. It does not unpack supporting ZIP files. Keep the signed source and test result privately; repeat the full test plan with attachments, permission failures, recovery, and read-only availability before treating the installation as ready for broader use.

## 12. Maintain the installation

### Subsequent releases

After initial setup, each push to `main` automatically publishes the website after tests pass. **`release`** remains a manual operation that runs tests, migrations, a publisher rebuild, and website deployment. Use **`publisher`** for a publisher rebuild or **`website`** for a website-only release. Infrastructure or runtime-secret changes require a reviewed **`infrastructure-apply`**; neither rebuild operation applies them.

### Credential expiration

The initial procedure above selects 30 days for all four management tokens and the R2 runtime token. Confirm each token's actual expiration in its provider dashboard and record it in a private operations log. GitHub secret update timestamps are not token-expiration dates.

| Credential | When to update | Where to update |
| --- | --- | --- |
| `TF_API_TOKEN` | Before its selected expiration, or after revocation/compromise. | Generate a replacement in HCP Terraform; replace the GitHub secret. |
| `CLOUDFLARE_API_TOKEN` | Before its selected expiration, or after revocation/compromise. | Generate a replacement R2 administration token; replace the GitHub secret. |
| `DIGITALOCEAN_TOKEN` | Before its selected expiration, or when different scopes are required. | Generate a replacement in the same DigitalOcean team; replace the GitHub secret. |
| `SUPABASE_ACCESS_TOKEN` | Before its selected expiration, or when its project access must change. | Generate a replacement scoped token; replace the GitHub secret. |
| `SUPABASE_DB_PASSWORD` | During a planned administrator-password rotation or after exposure. | Change the database password and update the GitHub secret together. This guide sets no automatic expiry. |
| Runtime R2 key pair | Before the runtime token's selected expiration or after exposure. | Replace both key fields together in secret storage and the running publisher. |
| Publisher database credential | During planned role-password rotation or after exposure. | Update the database role and its secret connection string; redeploy/restart the publisher as required. |

Set reminders before expiration, for example seven days and one day beforehand. No reminder or automatic renewal is created merely by following this guide.

For token rotation:

1. Create a replacement with the same intended scope and record its new expiration privately.
2. Replace the secret under the same GitHub name. Update runtime secret copies where applicable.
3. Run a read-only credential check and a Terraform plan. For runtime credentials, also test a publication upload and verified pull.
4. Revoke the old token after the replacement works. If it is compromised, revoke it immediately and recover access through a trusted account.

Management-token expiration normally blocks future deployment or administration. Runtime credential expiration can stop publishing. Keep those credentials separate. Password rotation requires coordination because replacing a GitHub secret alone does not change the password accepted by PostgreSQL.

For this workflow, rotating the R2 runtime keys means replacing both GitHub secrets, then running and reviewing **`infrastructure-apply`** so the new pair reaches App Platform. A **`publisher`** rebuild alone does not copy changed GitHub secrets into the app specification. Verify a live upload and pull before revoking the old token.

For the publisher database password, update `PUBLISHER_DATABASE_URL`, run **`migrate`** to change the role password, then run the reviewed **`infrastructure-apply`** to update the app's connection string. Coordinate this as a maintenance window: the old app password can fail between those steps. Verify publisher health and a live publication afterward.

### Operational upkeep

| Cadence | Work |
| --- | --- |
| Before every release | Check credential validity, tests, migrations, resource identity, plan changes, and service readiness. |
| Weekly during the pilot | Check failed uploads/finalizations, cleanup, backup results, service health, storage growth, and usage charges. |
| Before token expiration | Rotate the affected credential and verify its replacement. |
| Monthly | Review access, billing alerts, provider limits, dependency/security updates, and pending Terraform changes. |
| Periodically and after major changes | Restore a database backup and archive copy in an isolated environment, then verify publications and operational state. |
| Before domain renewal | Confirm renewal, DNS ownership, HTTPS, and archive delivery. |

Keep SQL backups that include the whitelist, upload outcomes, and failed UUID reservations. Keep independent copies of the publication archive. Terraform state is neither backup.

**Backup automation is not installed by this workflow.** Before broader use, choose a database backup method and schedule, an independent private archive-copy destination, retention, access controls, and a restore procedure. Verify the actual Supabase plan's backup coverage instead of assuming the account includes it. Record successful backup and isolated restore runs. Retention locks in R2 and HCP state history do not replace these backups.

After total SQL loss, an archive rebuild can restore the public catalog but cannot restore publishing permissions or failed UUID reservations. Keep publishing disabled until operational SQL state is restored and reconciled.

Before production use, review whether the Supabase plan provides the availability and backups needed. Track R2 storage and request charges and DigitalOcean compute costs. Budget alerts notify; they do not necessarily cap spending.

When changing a domain, update DNS, CORS, the website configuration, and the generated descriptor, then rerun delivery checks. Preserve historical `instance_url` values in existing acceptance records.

Keep a private operations log containing credential owners and expiration dates, resource IDs, backup locations, restoration results, and rotation history. Store secret values in a password manager or secret store, not in that log or this public guide.

### Private handoff for the next session

Maintain this information outside the public repository. It lets a new operator resume without repeating account setup or guessing whether a stage completed:

- Repository URL, default branch, deployed commit, Pages settings, and environment approval policy.
- Cloudflare account/zone, both bucket names, archive domain status, and whether domains are dashboard-managed or Terraform-managed.
- Supabase organization/project reference and region; DigitalOcean team/project/app ID and endpoint; HCP organization/workspace and execution mode.
- Current non-secret repository variable values, including `SCI_DEPLOY_PUBLISHER`, final URLs, and any intentional endpoint overrides.
- The eight GitHub secret names, provider token names/scopes/expiration dates, owner, and password-manager entry references. **No secret values.**
- Last successful plan/apply, migration, publisher, recovery (if used), and website run links; pending approvals or failures.
- Latest health response and date, live publication test UUID and byte-comparison result, completed release checks, and unresolved limitations.
- Backup destination and schedule, last successful restore, domain renewal, credential reminders, and billing-alert owner.

Record “not done” for unfinished checks. A new session should verify current status against this inventory; the public guide does not track an installation's live state.

## Appendix: resuming or troubleshooting an existing installation

These are conditional recovery procedures, not steps in a fresh deployment.

### Resume an installation

For a **returning session**, first read this guide, the current deployment workflow, and the private operations handoff at the end of this guide. Inspect current repository variables, secret **names and update dates**, recent Actions runs, provider resource status, and token expiry. Do not ask anyone to paste secret values into chat.

Resume at the first incomplete stage. Do not create a second Supabase project or HCP workspace, overwrite an author's signing key, or reset `SCI_DEPLOY_PUBLISHER` to `false` on an existing deployment. A saved secret or a green historical workflow run is not evidence that the live service works now. Run a fresh plan before applying and stop to investigate unexpected replacement or deletion.

### Common setup failures

| Symptom | Next action |
| --- | --- |
| Supabase `infra_add_ons_read` permission error | Replace `SUPABASE_ACCESS_TOKEN` with the scoped permissions in §7, including Add-ons Read; rerun a plan before apply. |
| Health is HTTP 200 but storage is degraded, including signature errors | Check the token's account, expiry, and both bucket scopes. Replace the matching S3 key pair if uncertain; update both GitHub secrets and run reviewed `infrastructure-apply`. Inspect both bucket statuses, then test upload and pull. |
| Database login fails | Check session pooler host, role-qualified username, port 5432, and password encoding. Run `migrate` after saving `PUBLISHER_DATABASE_URL`, then apply to update the app. |
| Publisher works but Terraform proposes `replace_because_tainted` | Follow the guarded `publisher-recover` procedure below; then plan and apply again. |
| Website job has no publisher endpoint after failed creation | Repair the app/state and complete an apply to save endpoint outputs; rerun `website`. Do not invent an endpoint or create a duplicate app. |
| Domain resolves but HTTPS fails | Check provider certificate status and DNS target. Wait for certificate readiness, then rerun `website`; do not bypass TLS verification. |
| Domain works through public DNS but not locally | Check local negative DNS caching separately from authoritative/public DNS. Avoid repeatedly changing correct records. |
| Runtime secrets changed but behavior did not | `publisher` and `release` do not apply infrastructure. Run reviewed `infrastructure-apply` to update App Platform environment values. |

### Recover an app after a failed initial Terraform creation

If a corrected **`publisher`** release succeeds but a new plan still proposes replacing `digitalocean_app.publisher[0]` with reason **`replace_because_tainted`**, do not approve that replacement as a routine configuration change.

Run **`publisher-recover`** on `main` and approve the **`production`** gate. It checks that the named DigitalOcean app has an ACTIVE deployment, has no deployment in progress, matches the exact app ID recorded for the publisher resource, and passes its database health check. Only then does it clear that resource's taint flag. It does not recreate the app, rotate credentials, or export state. Storage degradation is reported separately and still needs investigation.

Next run **`infrastructure-plan`** again. Confirm the replacement has disappeared, then run and review **`infrastructure-apply`** to update configuration and save the endpoint outputs. Finish with **`website`**. Recovery is for this verified failed-creation case; do not use it to suppress replacement caused by an actual infrastructure change.
