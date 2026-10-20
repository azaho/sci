# Hosted setup

This guide describes the online setup for a public SCI installation. It covers accounts, deployment credentials, infrastructure, the first release, and routine upkeep. It contains no installation-specific account names, resource IDs, domains, or credentials. Replace placeholders with your own values.

The system requirements are in [DESIGN.md](DESIGN.md). Release checks are in [TEST_PLAN.md](TEST_PLAN.md).

**Implementation status:** the application, local installation, Terraform configuration, and deployment workflow are implemented in this repository. Completing account and token setup does not mean the platform is deployed. Use [README.md](README.md) for local operation and [docs/HOSTED.md](docs/HOSTED.md) for the implemented configuration names, release order, runtime credentials, and remaining hosted checks.

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

1. Create the repository and retain administrator access for setup.
2. Keep `DESIGN.md`, `TEST_PLAN.md`, and this guide in the repository.
3. Choose an instance URL. A project site can use `https://<owner>.github.io/<repository>/`. If the owner's personal Pages site has a custom domain, project sites normally inherit it, for example `https://example.org/sci/`. See [GitHub's domain rules](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/about-custom-domains-and-github-pages#using-a-custom-domain-across-multiple-repositories).
4. Choose a separate archive hostname, such as `archive.example.org`, on a domain managed in Cloudflare.
5. Plan to use the DigitalOcean app's HTTPS hostname for the publisher initially. A custom publisher hostname is optional.

Enable Pages deployment from **GitHub Actions** when the website workflow is ready. The website and descriptor must support a project subpath, not just a domain root.

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
6. Leave bucket creation to Terraform.

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

## 7. Add the bootstrap secrets to GitHub

Open **Repository → Settings → Secrets and variables → Actions → New repository secret**.

For each secret, use the exact name below, paste the value directly from the provider or password manager, and click **Add secret**. Never put values in issues, chat, source files, screenshots, or command-line arguments.

These five names are the bootstrap configuration contract:

| GitHub Actions secret | Value to store | Purpose |
| --- | --- | --- |
| `TF_API_TOKEN` | HCP Terraform user API token | Authenticate to the Terraform state workspace. |
| `CLOUDFLARE_API_TOKEN` | R2 account API **Token value** | Create and configure R2 infrastructure. This is not an S3 Access Key ID or Secret Access Key. |
| `DIGITALOCEAN_TOKEN` | DigitalOcean personal access token | Manage the SCI App Platform deployment and project. |
| `SUPABASE_ACCESS_TOKEN` | Supabase scoped personal access token | Configure the existing project and perform authorized management operations. |
| `SUPABASE_DB_PASSWORD` | The existing project's PostgreSQL administrator password | Connect for database bootstrap and migrations. |

GitHub secret names do not automatically become environment variables. The workflow must explicitly pass them to the relevant tools. In particular, map `TF_API_TOKEN` to Terraform authentication, such as `TF_TOKEN_app_terraform_io`, and pass `SUPABASE_DB_PASSWORD` only to database administration jobs. See [GitHub Actions secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).

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

5. Review access, create the token, and save it as `SUPABASE_ACCESS_TOKEN`.

This is administrative access within the selected project, including SQL changes, access to API key secrets, and potentially destructive project operations. It is not a frontend key. Use the scoped token instead of a legacy token with full account access. The implemented workflow must test these permissions against its actual provider and CLI versions; add permissions only for a demonstrated requirement. See [Supabase personal access tokens](https://supabase.com/docs/guides/platform/personal-access-tokens).

### Database password

Save the password created in step 3 as `SUPABASE_DB_PASSWORD`. Do not create a second password merely to populate this secret. The administrator password is for bootstrap and migrations; the running publisher must use a separate database role.

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

The deployment will also need the instance URL, archive hostname and zone ID, bucket names, publisher region and size, and the public Supabase publishable key. Their workflow variable names must be documented when the deployment code is implemented. Do not substitute a Supabase secret or service-role key for the public publishable key.

At this point, account and credential bootstrap is complete. Secret presence confirms only that GitHub stored a value; it does not prove that the token is valid, correctly scoped, or unexpired.

## 9. Implement and provision the infrastructure

The following work remains after the browser setup:

1. Implement and test the application, shared publication validation, database migrations, and local Compose installation.
2. Implement `deploy/infra.tf`, pin provider versions, and generate the provider lockfile.
3. Configure the existing HCP workspace as the state backend. Import the existing Supabase project using the provider's documented import mechanism.
4. Protect persistent resources with Terraform lifecycle rules and available provider protections. Review the initial plan for unexpected replacement or deletion.
5. Create the private temporary R2 bucket and public publication bucket. Configure CORS, archive-domain delivery, response headers, and overwrite protection for published objects.
6. Configure cleanup to preserve temporary objects still required by committed finalizations. A blanket age-based deletion rule must not destroy recovery sources.
7. Create the DigitalOcean project and App Platform service from `server/Dockerfile`, near the database. Configure health checks and recovery execution.
8. Apply incremental SQL migrations. Create the private operational schema, the `catalog_v1` read interface, and separate administrator, publisher, and public read permissions.
9. Configure Supabase to expose only the approved catalog interface. Test grants and row policies through anonymous HTTP.

After the configuration exists, the equivalent infrastructure commands from the repository root are:

```sh
terraform -chdir=deploy init
terraform -chdir=deploy plan
terraform -chdir=deploy apply
```

Review the plan before approving apply. These commands do not perform the complete application release. Never run a production destroy operation as a routine way to stop service or reduce costs.

## 10. Create runtime credentials after provisioning

The five bootstrap secrets are not the complete runtime configuration.

Once the buckets exist, create a separate R2 **Object Read & Write** token restricted to the two SCI buckets. Store its Access Key ID and Secret Access Key in secret storage, then inject them into the publisher. This token must support the required upload, verification, copy, and cleanup operations without bucket-administration access.

Create a dedicated publisher database role with the permissions required for publication transactions and whitelist reads. It must not be able to administer the whitelist.

The proposed names for this later stage are:

| Secret | Intended value |
| --- | --- |
| `R2_ACCESS_KEY_ID` | Bucket-scoped runtime token's S3 Access Key ID. |
| `R2_SECRET_ACCESS_KEY` | The same token's S3 Secret Access Key. |
| `PUBLISHER_DATABASE_URL` | TLS connection string for the dedicated publisher database role. |

These names are reserved by this guide; the server's environment mapping and deployment workflow still need implementation. If supplied through GitHub Actions, store them as secrets under these names. Configure their runtime destinations as encrypted App Platform variables.

Use the documented Supabase connection endpoint appropriate to the publisher's network and connection model. Test connectivity and transaction/locking behavior. A connection string containing a password is a secret.

The author signing private key stays on the author's machine. Enable only the author's public key through the private whitelist administration helpers after migrations are installed.

## 11. Deploy and verify the first release

1. Implement `.github/workflows/deploy.yml` with separate operations for infrastructure changes, publisher releases, migrations, and website releases.
2. Require review of the Terraform plan before applying infrastructure changes. Use GitHub deployment controls where appropriate; keep credential-bearing jobs away from untrusted pull-request code.
3. Authenticate using the saved secrets and verify access without printing credentials or uploading private state as public artifacts.
4. Deploy the publisher and wait for readiness.
5. Publish `web/` and a generated `sci-server.json` through GitHub Pages.
6. Populate the descriptor with `schema_version`, `publishing_api_url`, `catalog_url`, `catalog_schema`, `archive_url`, and, for Supabase, `supabase_publishable_key`. `archive_url` ends at the `publications/` base path. Include no private credentials.
7. Test HTTPS, CORS, catalog profile/key headers, content types, download behavior, and browser rendering at the actual production URLs.
8. Test real R2 presigned PUT size enforcement and conditional copies. If R2 does not enforce the intended upload cap, resolve the upload-handler versus limited-pilot choice in `DESIGN.md` before launch.
9. Run sign → verify → push → browse → pull and compare exact signed bytes.
10. Complete `TEST_PLAN.md`, including interrupted finalization, whitelist enforcement, mirroring, database restoration, archive recovery, and preservation across redeployment.
11. Import existing research only after those checks pass and the import-attestation format is defined.

Test public reads while the publisher is stopped. Test pulling a known publication with the catalog unavailable. Neither case should depend on private deployment credentials.

## 12. Maintain the installation

### Credential expiration

The initial procedure above selects 30 days for all four management tokens. Confirm each token's actual expiration in its provider dashboard and record it in a private operations log. GitHub secret update timestamps are not token-expiration dates.

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

After total SQL loss, an archive rebuild can restore the public catalog but cannot restore publishing permissions or failed UUID reservations. Keep publishing disabled until operational SQL state is restored and reconciled.

Before production use, review whether the Supabase plan provides the availability and backups needed. Track R2 storage and request charges and DigitalOcean compute costs. Budget alerts notify; they do not necessarily cap spending.

When changing a domain, update DNS, CORS, the website configuration, and the generated descriptor, then rerun delivery checks. Preserve historical `instance_url` values in existing acceptance records.

Keep a private operations log containing credential owners and expiration dates, resource IDs, backup locations, restoration results, and rotation history. Store secret values in a password manager or secret store, not in that log or this public guide.
