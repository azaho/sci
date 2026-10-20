# SCI

SCI stores signed scientific publications. Each record contains author metadata, a
README, a fixed license, and optional supporting files. Readers can download,
verify, and mirror the original bytes.

The software uses Apache-2.0. Publication text, figures, and data use CC BY 4.0;
publication code uses MIT. These are separate licenses.

## Install the CLI

Use Python 3.12 or later. From this checkout:

```sh
uv tool install .
sci --help
```

Alternatively, install with `python -m pip install .` in a virtual environment.
The distribution name is `sci-publications`; the command and Python package are
`sci`. This repository does not yet have a PyPI release.

## Start a local instance

Install Docker Engine and Docker Compose v2 or later. Then run:

```sh
docker compose -f deploy/compose.yaml up -d --build --wait
```

Open **http://localhost:8080/**. The publisher is at port 8000. S3 uploads use
port 9000. All published host ports bind to loopback. PostgreSQL and PostgREST
have no published host ports.

Compose starts PostgreSQL 18, PostgREST, SeaweedFS S3 storage, the Python
publisher, and nginx. It applies missing migrations, creates missing buckets,
and prepares the website descriptor. It keeps existing data.

The Compose project name is `sci-local`. Named volumes hold the database, archive,
local storage configuration, and generated website. Stop the instance with:

```sh
docker compose -f deploy/compose.yaml down
```

The next `up` uses the same data. `up -d --build --wait` installs code changes
without removing publications or whitelist entries. The separate command
`docker compose -f deploy/compose.yaml down -v` **deletes local data**. Use it only
for an intentional reset.

Local passwords have visible development defaults. They are not hosted secrets.
For other values, use an ignored environment file and `--env-file PATH`.
Supported names are `LOCAL_ADMIN_PASSWORD`, `LOCAL_PUBLISHER_PASSWORD`,
`LOCAL_CATALOG_PASSWORD`, `LOCAL_STORAGE_USER`, and `LOCAL_STORAGE_PASSWORD`.
Use URL-safe local database passwords because Compose places them in connection
URLs. Set `SCI_WEB_PORT`, `SCI_PUBLISHER_PORT`, and `SCI_STORAGE_PORT` to change
host ports. A different Compose project name creates an independent instance;
give it different ports too.

The local S3 service has telemetry disabled. After the initial dependency and
image downloads, local use requires no cloud service.

## Sign and publish

```sh
sci server http://localhost:8080
sci keygen
mkdir result
```

Write `result/sci.json`:

```json
{
  "title": "The result and its tested scope",
  "abstract": "State the question, method, result, and limitation.",
  "categories": ["cs.LG"],
  "keywords": ["reproducibility"],
  "citations": [
    {
      "target": "doi:10.1000/182"
    }
  ]
}
```

Write the main text and attribution in `result/README.md`. Put optional code,
data, and supporting figures in `result/files/`. A headline figure must be a
static PNG named `headline_figure.png`, at most 2,000,000 bytes, with each
dimension from 1 through 1,600 pixels. Put its optional caption in
`headline_figure_caption` in `sci.json`.

Use prose to refer to the headline figure. Refer to supporting figures by number
and filename in the ZIP. Do not embed README images or use relative links to
bundled files. The reader does not display these images or resolve these links.
Ordinary external links are supported.

```sh
sci sign ./result
sci verify ./result --public-key ~/.sci/key.pub.pem
sci push ./result
```

The administrator must enable the public key before the last command. See the
next section. Keep the private key outside the publication folder.

`sign` preserves the author JSON and README bytes. It writes the standard
`LICENSE`, packs `files/` into `files.zip`, and creates a UUID, manifest, and raw
64-byte signature. A different existing `LICENSE` causes an error. Do not discard
third-party notices to bypass this check. Reference incompatible material
externally. See [Publishing terms](#publishing-terms).

`push` checks and uploads the existing signed record. It does not need the private
key, change the UUID, or rebuild the ZIP. Retry the same folder after an
interruption. Editing a declared file requires a new `sign`, which creates a new
UUID. Keep a copy of any earlier signed record that you need.

```sh
sci pull UUID --output ./copy
sci push ./copy --server https://another-instance.example/sci/
```

The destination must enable the original author's key. Do not sign again when
you mirror a record. Pull uses the descriptor and archive only. It does not
require the publisher or catalog. It verifies all declared files, refuses a
nonempty destination, and never extracts the ZIP.

Use `--json` for machine-readable output. Progress and errors go to stderr.
`sci --help` is the full offline author guide. Each command also has `--help`.

## Manage the whitelist

Use the Supabase SQL editor with the migration administrator account. For a local
instance, open the same SQL interface with:

```sh
docker compose -f deploy/compose.yaml exec database psql -U postgres -d sci
```

After signing, the public value is `public_key.x` in `manifest.json`. It is the
canonical, unpadded base64url encoding of the raw Ed25519 public key. It is safe to
give this public value to the administrator. Never send the private PEM.

```sql
SELECT sci_private.enable_key('PUBLIC_JWK_X');
SELECT * FROM sci_private.list_keys();
SELECT sci_private.disable_key('PUBLIC_JWK_X');
```

Replace `PUBLIC_JWK_X` with the actual public value. The helpers validate the key
and derive its fingerprint. Repeated enable/disable calls are safe. Disabling an
unknown key gives an error.

The publisher can read the whitelist but cannot change it or call these helpers.
Anonymous readers cannot access the whitelist. Disabling a key blocks uncommitted
submissions. Finalizations that already committed finish with their original
timestamp.

## Query the public catalog

Read `INSTANCE/sci-server.json`, where `INSTANCE` is a directory URL. For example,
`https://example.org/sci` resolves to
`https://example.org/sci/sci-server.json`.

The descriptor gives `publishing_api_url`, `catalog_url`, `catalog_schema`,
and `archive_url`. The archive base already ends at the `publications/` path.
Append only the UUID and permitted filename.

Send `Accept-Profile: catalog_v1` on catalog GET requests. If the descriptor
includes `supabase_publishable_key`, send it in the `apikey` header **only to the
configured catalog service**. Do not use a Supabase secret or service-role key.
No catalog key is sent to the archive or publisher.

Examples below are relative to `catalog_url`. URL-encode all query values.

| Task | Query |
| --- | --- |
| Feed | `/publications?select=uuid,title,abstract,categories,author_fingerprint,accepted_at&order=accepted_at.desc,uuid.desc&limit=20` |
| Full text | `/publications?search_document=wfts(english).memory&limit=20` |
| Any listed category | `/publications?categories=cs.{cs.LG}&limit=20` |
| Publisher | `/publications?author_fingerprint=eq.ed25519:HEX&limit=20` |
| Detail | `/publications?uuid=eq.UUID&limit=1` |
| Outgoing citations | `/citations?source_uuid=eq.UUID&order=position.asc&limit=100` |
| Incoming citations and source metadata | `/citations?target_key=eq.sci:UUID&select=source_uuid,position,target,relation,reason,publications(uuid,title)&order=source_uuid.asc,position.asc&limit=100` |
| Distinct source count | `/citation_counts?target_key=eq.sci:UUID` |
| Normalize a DOI query | `/rpc/normalize_citation_target?target=doi%3A10.1000%2F182` |

Full-text search uses PostgreSQL's `english` configuration. It covers title,
abstract, and keywords with weights A, B, and C. It does not search README bytes.
For the next feed page, retain the original filters and the last timestamp and
UUID. Add this `or` filter, with its value URL-encoded:

```text
(accepted_at.lt.TIMESTAMP,and(accepted_at.eq.TIMESTAMP,uuid.lt.UUID))
```

For outgoing citations, add `position=gt.LAST_POSITION`. For incoming citations,
use `(source_uuid.gt.UUID,and(source_uuid.eq.UUID,position.gt.POSITION))`.
Refresh to see new records. Pages do not share a database snapshot.

The API caps pages at 100 rows. Public roles have a five-second statement timeout.
Unrestricted PostgREST aggregates are disabled. `citation_counts` counts distinct
accepted source publications, including equivalent DOI spellings only once.
No count row means zero. Public views exclude `finalizing` and `failed` rows.

## Codebase

| Path | Responsibility |
| --- | --- |
| `sci/validation.py` | Strict document schemas, text rules, DOI normalization, PNG checks, and limits |
| `sci/keys.py` | Ed25519 PEM/JWK conversion, key generation, and fingerprints |
| `sci/publication.py` | Exact-byte signing and verification; ZIP verification uses streams |
| `sci/preview.py` | Local ZIP packaging and bounded file previews |
| `sci/client.py`, `sci/cli.py` | Discovery, local settings, six commands, push, and atomic pull |
| `server/app.py` | Bounded multipart ingress, two publishing endpoints, health, and rate limiting |
| `server/publishing.py` | Upload state, SQL claim, conditional copies, acceptance, cleanup, and recovery |
| `server/storage.py`, `server/database.py` | S3 operations and PostgreSQL locks/connections |
| `server/*.sql`, `server/migrate.py` | Private tables, RLS, catalog views, admin helpers, and checked migrations |
| `server/maintenance.py` | Finalization recovery, cleanup, and archive catalog rebuild |
| `web/` | Static feed and reader; no frontend build is required |
| `deploy/` | Compose, Terraform, static output, release steps, and storage probes |
| `tests/` | Protocol tests, PostgreSQL tests, real HTTP checks, and browser checks |
| `examples/create_demo.py` | Reproducible synthetic publications used in the screenshots |

See [implementation choices](docs/IMPLEMENTATION.md) for details that the design
left open. [Verification notes](docs/VERIFICATION.md) separate local evidence from
hosted release checks.

## Test and develop

```sh
uv sync --frozen --extra server --extra test
uv run pytest -q
uv run ruff check sci server deploy tests examples
uv build
```

The command above skips PostgreSQL tests unless `SCI_TEST_DATABASE_URL` identifies
a **disposable** database. Those tests truncate SCI tables. Use an administrator
URL for that database. Never use a production database.

```sh
SCI_TEST_DATABASE_URL=postgresql://postgres@localhost:5432/sci_test uv run pytest -q
```

For the complete local installation and browser checks, start with empty local
volumes. The demo enables only its own generated public key:

```sh
docker compose -f deploy/compose.yaml up -d --build --wait
uv run python -m tests.system_smoke
npm ci --ignore-scripts
npx playwright install chromium
npm run test:browser
```

The Node dependencies are test tools. The deployed website has no Node runtime
or build step. Browser distributions of Markdown-it and DOMPurify are pinned
and included in `web/vendor/`, with their license notices.

## Hosted release

[SETUP.md](SETUP.md) explains provider accounts and the five bootstrap secrets.
[Hosted operations](docs/HOSTED.md) lists the additional variables, runtime
credentials, release order, backup procedure, and launch checks.

`.github/workflows/deploy.yml` has separate manual operations for infrastructure,
migrations, publisher code, website files, endpoint verification, and storage
probes. It runs only from `main`. A code push does not deploy the system.

Infrastructure uses the existing HCP workspace for state. Terraform imports the
existing Supabase project and protects the database and both buckets from
routine destruction. The apply job uses an encrypted saved plan and a required
environment review.

The publisher requires a direct PostgreSQL connection or **session pooling**.
Do not use transaction pooling: finalization holds a session advisory lock
across SQL transactions and S3 operations.

## Recovery

The service resumes committed finalizations and cleans abandoned uploads in a
background loop. Operators can run the same work explicitly:

```sh
sci-maintain recover
sci-maintain cleanup
```

These commands use the publisher's runtime environment. If a required source is
confirmed missing or its ETag changed, finalization records a terminal failure.
The UUID remains reserved. Sign again for a new UUID. Temporary service errors
remain retryable.

To rebuild catalog rows from an archive, restore migrations and use:

```sh
sci-maintain rebuild-catalog
```

Supply `ADMIN_DATABASE_URL` as a secret environment variable, plus the normal
storage configuration. The rebuild uses acceptance records, checks every signed
file, and creates recovered identities as disabled. It stops on conflicts.
It leaves the persistent write switch off.

Restore operational SQL backups before enabling writes. They contain the
whitelist, upload outcomes, and failed UUID reservations. Archive bytes cannot
restore those facts. After explicit operator reconciliation, the administrator
can run:

```sql
UPDATE sci_private.instance_state SET writes_enabled = true WHERE singleton;
```

The runtime `SCI_WRITES_ENABLED` switch must also permit writes. A catalog rebuild
is not an authorization to enable this switch.

## Publishing terms

SCI uses one fixed publication license for schema version 1:
[publication_LICENSE.txt](sci/publication_LICENSE.txt). Text, figures, and data
use CC BY 4.0. Code uses MIT.

By signing, the publisher agrees to these terms and confirms authority to
distribute all included material under them. Retain required attribution,
copyright, and license notices. Reference incompatible material externally.
The server checks the approved license commitment and file integrity. It does
not prove rights ownership or inspect licenses inside ZIPs.

Hosted material must contribute research with relevant supporting files.
Negative results and uncertain future usefulness are welcome. Hosting rules
remain separate from reuse terms.
