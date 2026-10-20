# Hosted setup

This guide describes the online setup for a public SCI installation. It covers accounts, deployment credentials, infrastructure, the first release, and routine upkeep. It contains no installation-specific account names, resource IDs, domains, or credentials. Replace placeholders with your own values.

The system requirements are in [DESIGN.md](DESIGN.md). Release checks are in [TEST_PLAN.md](TEST_PLAN.md).

The application, Terraform configuration, and deployment workflow are included in the repository. This guide covers the account setup and first deployment using that workflow. Completing account and token setup does not mean the platform is deployed or its runtime credentials have been tested. See [README.md](README.md#hosted-deployment) for the full deployment configuration reference.

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

In **Settings → Pages → Build and deployment**, set **Source: GitHub Actions**. The website and descriptor must support a project subpath, not just a domain root.

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
| Add-ons (under Infrastructure and delivery) | Read |

5. Leave all other permissions at **None**. Review access: the list above grants five read-write capabilities and four read capabilities on the selected project.
6. Create the token and save it as `SUPABASE_ACCESS_TOKEN`.

**Add-ons read is required by Terraform**, even for an existing Free project with no paid add-ons. The pinned Supabase provider reads the project's add-ons during import and refresh to determine its compute size. The API permission is `infra_add_ons_read`; in the dashboard choose **Infrastructure and delivery → Add-ons → Read**. Do not grant Add-ons read-write. Ignoring changes to `instance_size` does not skip the provider's read. See the [provider's project resource](https://github.com/supabase/terraform-provider-supabase/blob/v1.11.0/internal/provider/project_resource.go).

If a plan fails with `Unable to read project addons` and `Missing required permission: infra_add_ons_read`:

1. Create a replacement project-scoped token with all permissions in the table, including **Add-ons: Read**. Supabase token access cannot be changed after creation.
2. Edit the existing GitHub Actions secret **`SUPABASE_ACCESS_TOKEN`** and replace its value. Keep the name unchanged; do not place the token in repository variables.
3. Run the **deploy** workflow on `main` with operation **`infrastructure-plan`**. This validates the replacement without applying infrastructure changes. A different missing-permission error needs separate investigation.
4. Once the plan succeeds, revoke the old token and record the replacement's expiration privately. Only run **`infrastructure-apply`** after reviewing the plan.

Keep the existing project import and `prevent_destroy` configuration. Removing the project resource or granting full-account access is not necessary to fix this read-permission error. `prevent_destroy` protects Terraform plans while the resource configuration remains present; it is not a Supabase account-level deletion restriction.

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

The main optional deployment variables are:

| Variable | Default / use |
| --- | --- |
| `SCI_INSTANCE_URL` | Defaults to the repository's standard GitHub Pages URL. Set the actual URL when using a custom domain. |
| `SCI_TEMP_BUCKET` | `sci-temp`; private uploads. Keep stable after creation. |
| `SCI_PUBLIC_BUCKET` | `sci-publications`; publication archive. Keep stable after creation. |
| `SCI_DEPLOY_PUBLISHER` | `false` during initial provisioning; set to `true` only after the runtime secrets and database login are ready. |
| `SCI_PUBLISHER_REGION` | `nyc`; choose near the database. |
| `SCI_PUBLISHER_SIZE` | `apps-s-1vcpu-0.5gb`; review current App Platform pricing before applying. |
| `SCI_ARCHIVE_HOSTNAME` | Custom archive hostname, such as `archive.example.org`. Empty uses the development `r2.dev` domain. |
| `SCI_ARCHIVE_ZONE_ID` | Set only when Terraform should connect the custom domain; leave empty when connecting it manually. |
| `SCI_ARCHIVE_NOSNIFF_RULE` | `false`; enabling the response-header rule requires additional zone permissions. |
| `SUPABASE_PUBLISHABLE_KEY` | Public frontend key; the website workflow looks it up when unset. Never use a secret or service-role key here. |

See [the complete variable reference](README.md#github-variables) for additional names and overrides. These are GitHub Actions **variables**, not secrets.

At this point, account and credential bootstrap is complete. Secret presence confirms only that GitHub stored a value; it does not prove that the token is valid, correctly scoped, or unexpired.

## 9. Provision infrastructure and run the first migration

Run operations from **Actions → deploy → Run workflow**, selecting branch **`main`** and the named **operation**. Pushes and pull requests run tests; they do not deploy infrastructure.

1. Keep `SCI_DEPLOY_PUBLISHER` unset or `false`.
2. Run **`infrastructure-plan`**. Terraform uses the existing HCP workspace and the committed provider lockfile. The configuration imports the existing Supabase project; it must not replace it. The first plan should create the two R2 buckets and their settings, the Data API settings, and the DigitalOcean project. It should not create the publisher yet.
3. Check for unexpected deletes, replacements, or changes to unrelated resources. The Supabase project and both buckets have `prevent_destroy`. If the DigitalOcean project already exists, import it as described in [README.md](README.md#hosted-deployment).
4. Run **`infrastructure-apply`**. This run generates its own plan summary. Read that summary, then use **Review deployments** to approve the **`production`** job. Wait for apply to succeed.
5. Run **`migrate`**. It creates the database schemas, catalog views, grants, and the `sci_publisher` role. With `PUBLISHER_DATABASE_URL` absent, it leaves the publisher login password unchanged. If the Data API setting is rejected because `catalog_v1` does not exist yet, run the migration first and retry the apply.

The temporary bucket stays private. The publication bucket uses a lock for published objects. Temporary cleanup must preserve sources needed by committed finalizations; do not add a blanket age-based object deletion rule.

The workflow prints resource actions and changed attribute names rather than raw plan values, and removes its saved plan file. HCP state still contains sensitive values once runtime credentials are supplied. Never run a production destroy operation as a routine way to stop service or reduce costs.

## 10. Create runtime credentials after provisioning

The five bootstrap secrets are not the complete runtime configuration.

### R2 runtime key pair

1. In Cloudflare, open **R2 Object Storage → Manage API Tokens → Create Account API Token**.
2. Use a descriptive name, such as `SCI publisher runtime`.
3. Select **Object Read & Write** and **Apply to specific buckets only**.
4. Select both `sci-temp` and `sci-publications`, or the actual names configured in section 8. Do not select all buckets.
5. Choose **30 days** for the pilot and record the expiration privately. This is a live service credential: publishing will stop working if it expires before rotation. Choose a different lifetime only with a corresponding rotation plan.
6. Create the token and save its S3 **Access Key ID** and **Secret Access Key** in your password manager.
7. In GitHub Actions repository secrets, add `R2_ACCESS_KEY_ID` with the Access Key ID and `R2_SECRET_ACCESS_KEY` with the Secret Access Key.

Use those two S3 fields, not Cloudflare's separate **Token value**. Keep `CLOUDFLARE_API_TOKEN` as the infrastructure administration credential. The runtime token does not need bucket-administration permissions.

### Publisher database URL

1. Generate a new, strong password for `sci_publisher` and save it in your password manager. A randomly generated 32-character password using letters and numbers avoids URL-encoding issues. Do not reuse the PostgreSQL administrator password.
2. Open the Supabase project's **Connect** dialog and select **Session pooler**. Copy the actual pooler host; do not infer it from the project's region.
3. Construct the following connection string, replacing all placeholders:

   ```text
   postgresql://sci_publisher.<project-ref>:<url-encoded-password>@<session-pooler-host>:5432/postgres?sslmode=require
   ```

4. Save the complete string as the GitHub Actions repository secret **`PUBLISHER_DATABASE_URL`**. If the password contains reserved URL characters, percent-encode the password component locally; never send it to an online encoding website.
5. Run **`migrate`** again and wait for success. The workflow extracts the password from this URL and applies it to `sci_publisher`, enabling login. Saving the GitHub secret alone does not set the database password.

Use session pooling on port **5432**, not transaction pooling on port **6543**: the publisher uses session advisory locks. The dedicated role has publication permissions and whitelist reads; it must not administer the whitelist.

Terraform maps these GitHub secrets to encrypted App Platform runtime variables:

| GitHub secret | Publisher runtime variable |
| --- | --- |
| `R2_ACCESS_KEY_ID` | `SCI_S3_ACCESS_KEY_ID` |
| `R2_SECRET_ACCESS_KEY` | `SCI_S3_SECRET_ACCESS_KEY` |
| `PUBLISHER_DATABASE_URL` | `SCI_DATABASE_URL` |

These values also enter Terraform state, so restrict HCP state access. A successful plan verifies configuration, not that the R2 keys or publisher login work; test them after deployment.

## 11. Deploy and verify the first release

1. Confirm all three runtime secrets are saved and the second migration succeeded.
2. Under **Actions → Variables**, set **`SCI_DEPLOY_PUBLISHER=true`**.
3. Run **`infrastructure-plan`**. After initial provisioning, the expected new resource is `digitalocean_app.publisher[0]`, plus its outputs. Investigate any unexpected changes.
4. Review the selected app size and [current DigitalOcean pricing](https://docs.digitalocean.com/products/app-platform/details/pricing/). Running a plan does not create the app; applying it starts the paid service.
5. Run **`infrastructure-apply`**, review that run's plan summary, and approve **`production`**. Wait for App Platform's initial build and deployment to succeed, then check the publisher's `/health` endpoint. Leave `SCI_DEPLOY_PUBLISHER=true` afterward; setting it back to `false` would propose deleting the app on the next apply.
6. Run **`website`** to publish `web/` and the generated `sci-server.json` through GitHub Pages. For subsequent deployments, **`publisher`** rebuilds the app; **`release`** runs the combined test, migration, publisher, and website sequence after infrastructure exists.
7. Check that the descriptor contains the actual HTTPS publisher, catalog, and archive URLs, `catalog_schema`, `schema_version`, and the public `supabase_publishable_key`. Its `archive_url` must end at the `publications/` base path. Include no private credentials.
8. For production archive delivery, connect a custom domain using the procedure below, apply the configuration, and rerun **`website`**.
9. Keep the author's signing private key on the author's machine. Enable only the author's public key using the whitelist administration helpers in the Supabase SQL editor.
10. Test HTTPS, CORS, catalog permissions, and archive delivery. Run sign → verify → push → browse → pull against the live instance and compare exact signed bytes. This is where the runtime credentials and their actual permissions are exercised.
11. Complete [TEST_PLAN.md](TEST_PLAN.md), including real R2 upload-size enforcement, conditional copies, interrupted finalization, whitelist enforcement, backup restoration, archive recovery, and preservation across redeployment. Resolve the upload-handler versus limited-pilot choice in `DESIGN.md` if real R2 does not enforce the intended upload cap.
12. Import existing research only after those checks pass and the import-attestation format is defined.

Test public reads while the publisher is stopped. Test pulling a known publication with the catalog unavailable. Neither case should depend on private deployment credentials.

### Custom domains while keeping GitHub Pages

You can use `sci.example.org` for the website and `archive.sci.example.org` for R2 without migrating website hosting. A personal site at `example.org` can remain on its existing host. Keep this repository public under the setup described here.

1. In the repository's **Settings → Pages**, save `sci.example.org` as the custom domain **before** adding its DNS record. Keep the source set to **GitHub Actions**.
2. In Cloudflare's existing zone, add a **CNAME** named `sci` targeting `<owner>.github.io`, with **DNS only** and **TTL Auto**. The target has no repository path. Leave the personal site's apex and `www` records unchanged. This serves the website directly from GitHub Pages; Cloudflare manages its DNS.
3. In **R2 → publication bucket → Settings → Custom Domains → Add**, enter `archive.sci.example.org`, select minimum TLS **1.2**, and connect it. Cloudflare creates the bucket's DNS record. Never connect a public domain to the temporary bucket.
4. Wait for the archive domain to become **Active**, then verify HTTPS independently. A missing object can return HTTP 404; certificate or handshake errors mean it is not ready. Later, verify a real publication can be downloaded.
5. Set these GitHub Actions repository **variables**:

   | Variable | Value |
   | --- | --- |
   | `SCI_INSTANCE_URL` | `https://sci.example.org/` |
   | `SCI_ARCHIVE_HOSTNAME` | `archive.sci.example.org` |
   | `SCI_ARCHIVE_ZONE_ID` | Leave unset for this dashboard-managed domain. |

   The dashboard procedure needs no new Actions secret or broader Cloudflare API token. Leave optional endpoint overrides unset unless intentionally overriding Terraform's outputs.
6. Run **`infrastructure-apply`** and inspect its plan before approving **`production`**. This updates publisher configuration and archive outputs, and disables the development `r2.dev` endpoint. Domain changes should not require replacing the publisher app. If a replacement appears, investigate the reported reason first. An earlier failed creation can leave an app marked as tainted even after a later publisher release succeeds; resolve that state before treating the plan as a routine URL update.
7. Run **`website`** to publish the site and regenerate `sci-server.json`. In **Settings → Pages**, enable **Enforce HTTPS** when GitHub makes it available. If the deployment succeeds but its final HTTPS check fails during certificate provisioning, wait for certificate readiness and rerun `website`.
8. Verify HTTPS for `/`, `/publication/`, and `/sci-server.json` at the website domain. Confirm the descriptor points to the new archive's `/publications` base path and the current publisher. Check the publisher's `/health` response body, including storage status; HTTP 200 alone does not prove publishing works. Complete the live publishing test before importing research.

DNS and certificate issuance finish separately. New records may resolve quickly, but allow for provider provisioning delays; do not promise a fixed completion time or bypass TLS checks. GitHub documents up to 24 hours for DNS propagation. GitHub Actions publishing does not require a `CNAME` file in the site artifact. See [GitHub's custom-domain procedure](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site) and [Cloudflare's public-bucket guide](https://developers.cloudflare.com/r2/buckets/public-buckets/).

Choose the final instance URL before the first real publication: existing acceptance records retain their historical instance URL.

## 12. Maintain the installation

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

After total SQL loss, an archive rebuild can restore the public catalog but cannot restore publishing permissions or failed UUID reservations. Keep publishing disabled until operational SQL state is restored and reconciled.

Before production use, review whether the Supabase plan provides the availability and backups needed. Track R2 storage and request charges and DigitalOcean compute costs. Budget alerts notify; they do not necessarily cap spending.

When changing a domain, update DNS, CORS, the website configuration, and the generated descriptor, then rerun delivery checks. Preserve historical `instance_url` values in existing acceptance records.

Keep a private operations log containing credential owners and expiration dates, resource IDs, backup locations, restoration results, and rotation history. Store secret values in a password manager or secret store, not in that log or this public guide.
