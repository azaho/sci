# SCI CLI design

Draft, 2026-11-07. Commands implement the [SCI protocol](../protocol/README.md) and the [Sciscape interfaces](https://github.com/azaho/sciscape/blob/main/DESIGN.md).

## Commands

| Command | Function |
| --- | --- |
| `sci search [QUERY] [--in SOURCE]` | Search local campaigns, remote catalogs, or both. |
| `sci read ID_OR_PATH [--file PATH]` | Read verified text or retrieve one file. |
| `sci pull ID [--output DIR]` | Save a complete verified publication. |
| `sci check DIR` | Check an unsigned draft without signing it. |
| `sci license DIR` | Install the baseline license in a draft. |
| `sci sign DIR [--output DIR]` | Sign a draft and optionally save its ZIP in a campaign folder. |
| `sci verify PATH` | Verify a signed directory or ZIP. |
| `sci publish PATH --to DESTINATION` | Store signed bundles in local folders or external archives. |
| `sci submit ID_OR_PATH --archive ARCHIVE [--to INDEX]` | Register hosted publications with an indexer. |
| `sci keygen [--key PATH]` | Create a signing key pair. |
| `sci claim TYPE ACCOUNT --output DIR` | Create a signed account claim. |
| `sci timestamp PATH [--tsa URL \| --submit]` | Obtain or submit timestamp proofs. |
| `sci revoke [--output DIR \| --submit DIR]` | Create or submit a key revocation. |
| `sci index [URL]` | Show or save the default index origin. |
| `sci folders [add DIR \| remove DIR]` | Manage the folders selected by `@local`. |
| `sci spec [--section N \| --list]` | Print the bundled protocol specification. |

`--in` selects a read source. `--to` selects a publishing or submission destination. `--output` selects a local file or directory. Each command defines the values it accepts.

## Search and retrieval

### `sci search [QUERY] [--in SOURCE]`

Repeat `--in` to combine sources:

| Source | Search scope |
| --- | --- |
| Folder path | Immediate `*.zip` children of that folder |
| HTTPS index origin | Catalog discovered through the index descriptor |
| `@local` | Known folders at command start |
| `@index` | Configured index |

Without `--in`, search the current folder offline. Without a query or filter, list local folders; index sources require one. Deduplicate resolved sources. Reject an unconfigured `@index`. An archive without a catalog is not a search service; do not enumerate or download it to build an index.

```sh
sci search "neural decoding" --in ./campaign-a --in ./campaign-b
sci search "neural decoding" --in @local --in https://sciscape.io
```

Return one local result group and one group per index. Identify each group's source, searched fields, and verification status. `--limit N` defaults to 20 per group. Show a continuation token when more results are available. `--cursor TOKEN` continues the same sources, query, and filters; reject conflicting options. Return available results and mark them as partial if a source fails.

Accept `--category SCHEME CODE` and `--key-x X` for either scope. `--domain DOMAIN` requires remote-only scope. Permit an omitted query when filters are supplied.

#### Local search

Verify new or changed ZIPs under [SCI protocol §6.4](../protocol/README.md#64-complete-bundle-checks). Search title, abstract, keywords, and README text. Match all case-folded query terms; order by publication ID. Return ID, title, abstract, signer, citations, and local paths. Merge duplicate IDs across selected folders. Report invalid candidates separately.

Store search data in `catalog.sqlite` in the CLI state directory. Key metadata and README text by verified publication ID. Track folder membership by path, file identity, size, modification time, and change time where available.

Scan selected folders before each query. Verify and index new or changed files; reuse unchanged rows without opening their ZIPs. Remove absent paths after a successful scan. An unavailable folder is not empty. Include only IDs present in the selected folders; discard metadata with no remaining paths.

Use short SQLite write transactions after verification. Several processes on one machine may share the cache in WAL mode; WAL does not work across machines, so keep the state directory on a local disk. Do not take the write lock when nothing changed. Rebuild a missing or corrupt cache from bundles, but never treat a busy database as corrupt. File state is a change detector, not a content proof. Other commands verify their inputs independently of this cache.

#### Remote search

Use `catalog_url` and `catalog_schema` under [Sciscape catalog queries](https://github.com/azaho/sciscape/blob/main/DESIGN.md#catalog-queries). The CLI constructs PostgREST requests and follows that interface's filters and pagination. Return newest admissions first.

Return ID, title, abstract, signer, admission time, and publication URL. Label these results as catalog metadata. Search does not retrieve publication files or verify them locally.

### `sci read ID_OR_PATH [--file PATH]`

Accept a signed directory, ZIP, or full publication ID. Local input is offline. Resolve IDs through the retrieval rules in `pull`.

By default, return metadata, citations, and README text after [SCI protocol §6.3](../protocol/README.md#63-metadata-checks). `--list` returns paths, sizes, and hashes after [SCI protocol §6.1](../protocol/README.md#61-manifest-checks). `--file PATH` retrieves one manifest entry under [SCI protocol §6.2](../protocol/README.md#62-file-checks). Do not combine `--list` and `--file`.

Prefer individual-file routes, then ZIP ranges. Fall back to a complete ZIP within the operation limits. Verify the entire requested file before returning any part of it.

Default text output contains at most 200 lines. `--start-line N` and `--lines N` select a range in the README or selected text file. Report truncation and the next line. With `--file`, `--output FILE` saves the complete original bytes instead of a text range. Binary files require `--output`. Keep an identical destination; reject conflicting bytes. Report which files passed verification, without claiming a complete bundle check.

### `sci pull ID [--output DIR]`

Use the configured index or `--in URL` for [SCI protocol §8.1.1](../protocol/README.md#811-publication-lookup). Map `404` and an empty source list to `no_known_sources`, with exit 5. Alternatively, repeated `--archive URL` options select descriptors without an index.

Retrieve under [SCI protocol §8.2](../protocol/README.md#82-archive-descriptor). Try alternative sources against the same expected ID. No catalog admission or acceptance receipt is required.

Default output is `./<digest>/`. Check an existing destination before downloading: keep a complete verified copy of the requested publication; reject other nonempty destinations. Verify temporary downloads under [SCI protocol §6.4](../protocol/README.md#64-complete-bundle-checks) before extraction. `--public-key PATH` requires the signer to match that public PEM key.

`--zip --output DIR` saves a ZIP using the local publishing rules below. Retrieve optional proofs through archive `entry_url` and the index's [timestamp lookup](../protocol/README.md#813-timestamp-lookup). Report proof checks separately; proof retrieval failure does not fail a complete bundle download.

## Publication preparation and delivery

### `sci check DIR`

Validate an unsigned draft under [SCI protocol §§2–4](../protocol/README.md#2-publication-bundle), including baseline eligibility. Calculate planned manifest and ZIP sizes with the signing serializer. Report independent errors together, identifying each file or JSON field, requirement, and observed value.

This command writes no files, reads no private key, and makes no network requests.

### `sci license DIR`

Copy the exact [approved baseline license](../protocol/README.md#21-baseline-publications) to `DIR/LICENSE`. Keep an identical file; reject a different license or a signed directory. This command explicitly selects the terms; signing does not insert them automatically.

### `sci sign DIR [--output DIR]`

Use the [protocol bundle layout](../protocol/README.md#2-publication-bundle). Apply the draft checks from `check`; reject symbolic links, special files, and recognized private-key data. Stop on baseline failures unless `--allow-nonbaseline` is supplied. This override permits only protocol-valid publications.

Build the [manifest](../protocol/README.md#3-manifest) with sorted file paths and a stable JSON serializer. Sign under [SCI protocol §5.1](../protocol/README.md#51-keys-and-signatures), then verify under [SCI protocol §6.4](../protocol/README.md#64-complete-bundle-checks). Stop if inputs change. Replace generated files together through a recoverable operation.

Preserve existing manifest bytes and the ID when the input and selected key are unchanged. Otherwise require `--replace`; discard proofs for the replaced signature.

Use the configured TSA unless `--no-timestamp` is set; `--tsa URL` overrides it. The default TSA is [FreeTSA](https://www.freetsa.org/index_en.php) at `https://freetsa.org/tsr`, with its [CA certificate](https://freetsa.org/files/cacert.pem) bundled as a timestamp trust anchor. Use no automatic fallback TSA. A timestamp failure preserves the signed publication and returns `partial`; retry with `timestamp`.

`--output DIR` also performs local publication as defined below. Packing can complete even if timestamping fails. A packing failure preserves the signed directory. Signing without timestamping is offline.

```sh
sci license ./draft
sci sign ./draft --no-timestamp --output ./campaign
```

### `sci verify PATH`

Verify a signed directory or ZIP under [SCI protocol §6.4](../protocol/README.md#64-complete-bundle-checks). Report baseline eligibility separately. `--public-key PATH` requires a matching public PEM key.

Verification is offline unless `--check-timestamp-online` permits timestamp trust checks. Report proof results under [SCI protocol §6.8](../protocol/README.md#68-timestamp-verification). Do not infer current account associations or absence of revocation from offline verification.

### `sci publish PATH --to DESTINATION`

Accept signed input under the shared input rules below. Publishing stores bundles; it never contacts an indexer. Repeat `--to` for several destinations:

| Destination | Operation |
| --- | --- |
| Folder path | Store verified ZIPs locally |
| `@local` | Store ZIPs in all known folders at command start |
| `https://github.com/OWNER/REPO` or `git@github.com:OWNER/REPO.git` | Upload reader files and GitHub Release assets |
| `https://huggingface.co/datasets/OWNER/REPO` or `git@hf.co:datasets/OWNER/REPO` | Upload reader files and ZIPs to a Hugging Face dataset repository |

Recognize the provider from the host and repository path. Accept an optional `.git` suffix, a trailing `/`, and equivalent `ssh://git@HOST/PATH` addresses. Resolve equivalent addresses to one destination. Reject unsupported remote addresses, file or branch URLs, query strings, and fragments. Do not treat a rejected remote address as a local folder.

Require a nonempty destination set. Snapshot inputs and destinations before writing. `--dry-run` lists the plan without writes or uploads. Report each publication and destination separately; a failure does not undo completed destinations.

#### Local folders

Create the destination if needed. Write `<digest>.zip` atomically under [SCI protocol §2.2](../protocol/README.md#22-single-file-bundle-zip). Keep identical existing bundles; reject conflicting bytes. Copy timestamp sidecars using [protocol naming](../protocol/README.md#2-publication-bundle). Report successful storage. Folder membership comes from the stored bundle, not a database row.

#### Hosted archives

Require an existing public repository, or `--create` to create one. Do not change an existing repository's visibility. Use provider credentials from `gh auth login`, `hf auth login`, or their supported token environment variables.

The destination address selects the repository. An SSH address does not replace credentials required for provider API calls.

Reader files are the manifest, signature, metadata, README, any license, and files named in `display`. Store their exact bytes at `sci/publications/<id2>/<id>/<path>` on `main`. Place an available timestamp sidecar beside the signature.

Publish `sci/archive.json` under [SCI protocol §8.2](../protocol/README.md#82-archive-descriptor). Maintain a sorted `sci/publications.txt` list containing completed uploads. Preserve existing publications and unrelated files. Batch repository changes into commits; merge concurrent additions without force-pushing.

For GitHub, upload `<id>.zip` as an asset of the shared release `sci-<id2>`, with a matching tag, where `<id2>` is the first two characters of the ID. Do not mark these releases as latest. An asset cannot be downloaded until its upload completes. Commit reader files in batches, after their ZIPs are uploaded. Wait when GitHub reports a rate limit. Descriptor:

```json
{
  "schema": "sci-archive/0.1",
  "bundle_url": "https://github.com/OWNER/REPO/releases/download/sci-{id2}/{id}.zip",
  "entry_url": "https://raw.githubusercontent.com/OWNER/REPO/main/sci/publications/{id2}/{id}/{+path}",
  "list_url": "https://raw.githubusercontent.com/OWNER/REPO/main/sci/publications.txt"
}
```

For Hugging Face, use the Hub upload API and its large-file storage. Descriptor:

```json
{
  "schema": "sci-archive/0.1",
  "bundle_url": "https://huggingface.co/datasets/OWNER/REPO/resolve/main/bundles/{id2}/{id}.zip",
  "entry_url": "https://huggingface.co/datasets/OWNER/REPO/resolve/main/sci/publications/{id2}/{id}/{+path}",
  "list_url": "https://huggingface.co/datasets/OWNER/REPO/resolve/main/sci/publications.txt"
}
```

Confirm public availability and browser access to reader files, including CORS. Report successful hosting, IDs, and the descriptor URL. On retry, compare remote content digests, or download and verify files when suitable digests are unavailable. Reuse matching content; reject conflicts. Recovery must work without local upload history.

### `sci submit ID_OR_PATH --archive ARCHIVE [--to INDEX]`

Accept full IDs or local signed inputs under the shared rules below. Repeat `--archive` for external descriptors or supported GitHub/Hugging Face
repository addresses (the same addresses accepted by `publish --to`). Resolve
a repository to the `sci/archive.json` descriptor written by `publish`; preserve
explicit descriptor URLs for custom layouts. Deduplicate identical resolved URLs. This
resolution uses no credentials, network, or local upload history and does not
establish that the files are hosted. Reject local folders and `@local`.
The same archive argument forms apply to `read` and `pull`. Use the configured index or one HTTPS origin supplied with `--to`. Files must already be hosted; submission neither uploads them nor changes local folder membership.

Call `submit_url` under [SCI protocol §8.1.4](../protocol/README.md#814-publication-submission), once per ID. Report success and include the service's response. Treat additional fields as service-specific results; protocol success alone does not establish catalog admission.

`--account TYPE ID` supplies a protocol account hint. Use the account rules from `claim`. Adding or renewing a claim permits resubmission of unchanged bundles.

Publish a campaign, then register it:

```sh
sci publish ./campaign --to https://github.com/lab/research --create
sci submit ./campaign --to https://sciscape.io \
  --archive https://github.com/lab/research
```

## Identity and timestamp proofs

### `sci keygen [--key PATH]`

Create keys under [SCI protocol §5.1](../protocol/README.md#51-keys-and-signatures). Default files are `key.pem` and `key.pub.pem` in the CLI state directory. Use PKCS#8 for private keys and SubjectPublicKeyInfo for public keys. Restrict private-key access to the owner; reject existing outputs.

For a custom key path, replace the final `.pem` with `.pub.pem`, or append `.pub.pem`. Return the public JWK and its `x` value. `--key PATH` also selects the private key for signing, claims, and revocation creation. Keep private keys outside publication input and output directories.

### `sci claim TYPE ACCOUNT --output DIR`

Create a claim under [SCI protocol §5.2](../protocol/README.md#52-external-identity-claims), including account normalization and required lookups. Write `<x>.json` and `<x>.json.sig`; print the required hosting locations. Do not upload them.

Default expiry is 90 days; `--expires RFC3339` overrides it. Renewal replaces the claim, not publication bytes.

### `sci timestamp PATH [--tsa URL | --submit]`

For proof creation, accept a signed publication directory, ZIP, or revocation directory. Use the configured TSA or `--tsa URL`. Verify the target signature before requesting a proof under [SCI protocol §6.8](../protocol/README.md#68-timestamp-verification).

Check returned response structure, status, and imprint; report trust checks separately. Append complete DER responses without duplicate bytes, using the protocol sidecar names. Write atomically. Preserve malformed existing sidecars and stop instead of appending to them.

`--submit` sends publication proofs through [Sciscape timestamp submission](https://github.com/azaho/sciscape/blob/main/DESIGN.md#post-apisubmittimestamp). Use the configured index or `--to URL`. This mode requires catalog admission and cannot combine with `--tsa`. Submit revocation proofs with their revocation record.

### `sci revoke [--output DIR | --submit DIR]`

Require exactly one action. `--output DIR` creates the record under [SCI protocol §5.3](../protocol/README.md#53-key-revocation), outside publication directories. Explain its permanent effect before interactive creation. Noninteractive invocation explicitly requests creation; it does not submit the record.

`--submit DIR` verifies under [SCI protocol §6.7](../protocol/README.md#67-revocation-checks) and calls [Sciscape revocation submission](https://github.com/azaho/sciscape/blob/main/DESIGN.md#post-apisubmitkey_revocation). Include an available timestamp sidecar. Use the configured index or `--to URL`.

## Local settings

### `sci index [URL]`

Show the configured index, or save the supplied HTTPS origin without a network request. No public index is selected by default.

Use `~/.sci/` for CLI state; `SCI_HOME` overrides it. Store index origin, key path, known folders, TSA URL, and trust configuration in `config.json`.

Pace requests to FreeTSA, together with Sciscape writes from the same machine, to at most one per second, under the [FreeTSA usage policy](https://www.freetsa.org/freetsa.html). Write configuration atomically. The SQLite cache is separate from configuration and signed bundles.

### `sci folders [add DIR | remove DIR]`

List known folders and their availability. `add DIR` saves an existing folder's canonical absolute path. `remove DIR` forgets the path without deleting files.

Remember folders after an explicit local search or a successful bundle write by `sign --output`, `pull --zip`, or `publish`. Keep the list in configuration so cache deletion does not erase it. Do not scan the computer for campaigns.

### `sci spec [--section N | --list]`

Print the complete protocol specification that the installed CLI implements, byte for byte. Bundle it with the library and check it against the hash pinned by the conformance corpus. `--section N` prints one numbered section with its subsections; `--list` prints section numbers and titles. This command works offline and lets agents read the authoritative rules without fetching them.

Top-level help also summarizes the draft format (files, `sci.json` fields and limits, citation targets and relations) and points to the relevant sections.

## Shared behavior

### Inputs and discovery

ID arguments accept `sha256:<digest>` and `sci:sha256:<digest>` under [SCI protocol §2](../protocol/README.md#2-publication-bundle). Results use `sha256:<digest>`; API requests use the required protocol representation.

For `publish` and local `submit` inputs, a ZIP or directory containing `manifest.json` is one publication. Other directories supply their immediate `*.zip` children. Require at least one candidate. Verify each under [SCI protocol §6.4](../protocol/README.md#64-complete-bundle-checks), deduplicate by ID, and report invalid candidates without sending them. Preserve signed bytes and IDs; do not follow citations or include drafts.

Discover index endpoints under [SCI protocol §8.1](../protocol/README.md#81-index-descriptor). Remote search and service-specific writes require their supported catalog or API schemas. Report absent capabilities as `unsupported_capability`. Retrieval uses [SCI protocol §8.2](../protocol/README.md#82-archive-descriptor); an explicit archive needs no configured index.

### Network operations

`--timeout SECONDS` sets the operation deadline, including retries; default to 120 seconds. For batches, apply it per publication and destination. Pace index writes to published quotas. Retry temporary failures with bounded backoff, jitter, and `Retry-After`; do not retry policy or validation refusals.

Retry the same signed inputs and request. A lost submission response is `outcome_unknown` until a repeat request confirms the result. Preserve completed items after interruption or partial failure. Keep incomplete files outside final output names.

### Results

Every command accepts `--help`. Help lists arguments, options, defaults, outputs, file changes, network use, exit codes, and a complete example. Generate help from the command definitions so it matches the installed CLI. Top-level help shows local campaign and remote publishing workflows. `sci --version` reports the installed CLI version. Help and version output work offline without configuration or side effects.

Report baseline eligibility, bundle verification, timestamps, associations, and revocation separately. Explicitly identify checks that were not performed. Use [SCI protocol §8.1.4](../protocol/README.md#814-publication-submission) for publication-submission errors and [Sciscape response contracts](https://github.com/azaho/sciscape/blob/main/DESIGN.md#shared-api-behavior) for its extensions. Errors show a code, explanation, and whether retrying may help; validation errors also identify the file or field, requirement, and observed value.

| Exit | Meaning |
| --- | --- |
| 0 | Requested operation completed |
| 2 | Invalid command or missing configuration |
| 3 | Failed protocol, signature, or content check |
| 4 | Unsupported schema or service capability |
| 5 | Incomplete operation, temporary failure, or partial result |
| 6 | Baseline refusal before signing or service policy refusal |

Valid non-baseline bundles can pass `verify` with exit 0. Local work limits return 5, not invalid-content status. Timestamp failure after signing returns 5 with the preserved publication ID.

## Remaining decisions

- Additional trust authorities and proof count/byte limits.

The `sci-protocol` distribution contains both the Python library and the `sci` CLI; see the [repository README](../README.md#install). It is not published to PyPI yet.

## Signed voting extension: `sci vote`

`vote ID (--up | --down | --withdraw)` signs and submits a `sciscape-vote/0.1`
record. Options: `--reason TEXT`, `--review ID`, `--revision N`, `--refresh`, `--key PATH`,
`--to INDEX`, `--account domain NAME`, `--timeout`, and `--json`.
`--output DIR --revision N` creates files offline; `--submit DIR` verifies
and submits existing files without loading a signing key. These modes are
mutually exclusive with incompatible creation options.

The service extension codec lives in `_cli/clients/votes.py` and reuses SCI's
strict JSON, Unicode character, Ed25519 public-key, and signature checks.
It does not add a record type to the SCI 0.1 protocol. The record requires
schema, public_key, target (SCI URI), value (-1/0/1), and revision (digits-only
integer 1..2147483647), and allows only optional reason and review. The JSON
is limited to 16 KiB, reason to 1,000 nonblank Unicode code points; the signature
is the raw 64 bytes over the exact original JSON.

Automatic revision selection discovers `vote_url` and the catalog, fetches
zero or one current record for the selected key and paper, and verifies its
signature, hash, target, and key before using it. The CLI retries the exact
current record for an unchanged opinion. `--refresh` forces a new revision
to attach current domain evidence, without altering prior signed events.
Omitted reason/review flags preserve the prior fields when refreshing; an
ordinary vote without those flags clears them. It
cannot combine with `--revision`, `--output`, or `--submit`. Otherwise the
command uses the next revision.
Explicit revisions bypass lookup. Cross-client conflicts remain errors;
network retries reuse the same multipart bytes and never advance revisions.

Created records are saved before POST to `SCI_HOME/votes/<hash>/` (or the
explicit offline directory), using no-clobber storage and keeping private keys
outside output directories. Submission responses must confirm the exact hash,
revision, and Boolean created flag. Lost responses and conflicts retain the
path for exact `--submit` retries. Domain hints are unsigned multipart metadata;
Sciscape decides eligibility and counting. Full review publications use the
existing publication pipeline and are referenced, not embedded. No credit
accounting is included.
