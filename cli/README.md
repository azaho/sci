# sci

The `sci` command-line tool signs, verifies, publishes, and finds
[SCI](../protocol/README.md) research publications (protocol draft 0.1). It is
built on the [`pysci`](../python/README.md) library. The intended behaviour is
specified in [DESIGN.md](DESIGN.md).

A publication is a folder or `<digest>.zip` with `README.md`, `sci.json`
(metadata and citations), an optional `LICENSE`, and supporting `files/`. Its
ID is the SHA-256 of its signed `manifest.json`: `sha256:<digest>`. Every
publication can be verified offline, without an index or archive.

## Install

Requires Python 3.11 or later. One package, `pysci`, contains both the Python
library and the `sci` command. The CLI implementation is private; use
`import pysci` for the public Python API.

After the first PyPI release, install the standalone command with:

```sh
uv tool install pysci
```

Or install both the library and command in a project virtual environment:

```sh
pip install pysci
```

`uv tool install` isolates the command; it does not add `pysci` to your
project or notebook environment.

Until the PyPI release, install from this checkout at the repository root:

```sh
uv tool install .
# Or, inside a project virtual environment:
pip install .
```

For development, use `uv sync` and `uv run sci --help`. To replace an older
Git-installed `sci-cli` tool, run `uv tool uninstall sci-cli` first; for an
older pip installation, run `pip uninstall sci-cli` in that environment
before installing the unified package. After the PyPI release, existing
library users should run `pip install --upgrade pysci`; plain `pip install`
can keep an older Git-installed copy. Package version 0.2.0 introduces this
unified distribution; the SCI protocol remains at draft 0.1.

[`tests/install_check.sh`](tests/install_check.sh) installs the unified package
with uv and pip in clean temporary environments, then verifies the library and
runs the offline CLI workflow. Pass a repository URL and ref, or a built wheel
or source distribution:

```sh
cli/tests/install_check.sh https://github.com/azaho/sci-dev main
cli/tests/install_check.sh "$PWD/dist/pysci-0.2.0-py3-none-any.whl"
```

## First local workflow

Everything here is offline. Prepare a draft folder:

```text
draft/
├── sci.json      {"title": "...", "abstract": "...", "keywords": [...], "citations": [...]}
├── README.md     main text (UTF-8 Markdown)
└── files/        optional code, data, and figures
```

```sh
sci keygen                                   # ~/.sci/key.pem (mode 0600) and key.pub.pem
sci license ./draft                          # select the SCI baseline license (CC BY 4.0 / MIT)
sci check ./draft                            # all problems at once, planned sizes, baseline
sci sign ./draft --no-timestamp --output ./campaign
sci verify ./campaign/<digest>.zip           # complete bundle checks (§6.4)
sci search "neural decoding" --in ./campaign # verified local search (catalog.sqlite cache)
sci search --in ./campaign --limit 50        # list publications; continue with --cursor
sci read ./campaign/<digest>.zip             # verified metadata and README
sci read ./campaign/<digest>.zip --file files/results.csv
sci publish ./campaign --to ./mirror         # copy verified ZIPs to another folder
```

`sign` without `--no-timestamp` also requests an RFC 3161 timestamp from
[FreeTSA](https://freetsa.org) (no automatic fallback TSA). If timestamping
fails, the signed publication is kept, the command exits 5 with its ID, and
`sci timestamp ./draft` retries later. Signing an unchanged draft with the same
key keeps its manifest and ID; changed drafts need `--replace`.

## Remote publishing

1. Host the bundles in a public archive. Publishing never contacts an index.

   ```sh
   gh auth login                              # or GH_TOKEN / GITHUB_TOKEN
   sci publish ./campaign --to https://github.com/lab/research --create
   # Hugging Face: hf auth login (or HF_TOKEN), then
   sci publish ./campaign --to https://huggingface.co/datasets/lab/research --create
   ```

   GitHub stores reader files under `sci/publications/<id2>/<id>/` on `main` and
   each `<id>.zip` as a release asset; Hugging Face stores the ZIPs under
   `bundles/` with Git LFS. Both get `sci/archive.json` and a sorted
   `sci/publications.txt`. Re-running compares remote digests and reuses
   matching content. `--dry-run` lists the plan without uploading.

   The same repository address works with `--archive` on `submit`, `read`,
   and `pull`. Explicit descriptor URLs remain supported for custom hosting.

2. Register the hosted copies with an index (no index is configured by default):

   ```sh
   sci index https://sciscape.io
   sci submit ./campaign \
     --archive https://github.com/lab/research
   ```

   Registration makes the publication retrievable by ID. Sciscape adds it to
   its searchable catalog only with a qualifying account association; create
   one with `sci claim domain lab.example --output ./claims`, host the two
   files where the command says, and submit again with
   `--account domain lab.example`. Registration alone does not guarantee
   admission to the searchable catalog.

3. Find and retrieve publications:

   ```sh
   sci search "neural decoding" --in @index --in @local
   sci read sha256:<digest>                    # verified README by ID
   sci pull sha256:<digest>                    # complete verified copy in ./<digest>/
   sci pull sha256:<digest> --archive https://github.com/lab/research
   ```

Timestamp proofs can be submitted to an index with
`sci timestamp ./campaign/<digest>.zip --submit`. A key revocation is created
with `sci revoke --output DIR` (permanent; keep it private until needed) and
sent with `sci revoke --submit DIR`.

## Format reference for agents

`sci --help` summarizes the draft format: files, `sci.json` fields and limits,
citation targets and relations. `sci spec` prints the complete protocol
specification bundled with the installed CLI (`--section 4` for metadata,
`--section 4.4` for citations, `--list` for the outline), so agents can read the
authoritative rules offline.

## Output for agents

Every command accepts `--json` and prints one object:

```json
{"command": "verify", "status": "ok", "exit_code": 0, "result": {...},
 "errors": [{"code": "...", "message": "...", "retryable": false,
             "file": "sci.json", "field": "/title", "requirement": "...", "observed": "..."}],
 "warnings": []}
```

Verification results carry `result.checks` with `bundle`, `baseline`,
`timestamps`, `associations`, and `revocation`, each with a `status`; checks
that were not performed say so and are listed in `checks.not_performed`.
`status` is `partial` when some items completed and others did not.

## Configuration

State lives in `~/.sci/`, or in `$SCI_HOME` if set:

| File | Contents |
| --- | --- |
| `config.json` | index origin, key path, known folders, TSA URL, timestamp trust |
| `key.pem`, `key.pub.pem` | default signing key pair (`sci keygen`) |
| `catalog.sqlite` | local search cache; safe to delete (rebuilt from bundles) |
| `pace` | last paced request time (FreeTSA and index writes: at most one per second) |

`config.json` example (all fields optional; `sci index` and `sci folders` edit
the first and third):

```json
{
  "index": "https://sciscape.io",
  "key": "/home/me/keys/lab.pem",
  "folders": ["/home/me/campaign"],
  "tsa_url": "https://freetsa.org/tsr",
  "timestamp_trust": {
    "freetsa": true,
    "anchors": ["/path/extra-tsa-root.pem"],
    "crls": ["/path/extra.crl"],
    "crl_urls": ["http://tsa.example/crl"]
  }
}
```

The FreeTSA root certificate is bundled with `pysci` and trusted by default.
Offline verification checks certificate revocation only against configured
`crls`; `sci verify --check-timestamp-online` fetches current CRLs.

Network commands accept `--timeout SECONDS` (default 120), an operation
deadline that includes retries and applies per publication and destination in
batches. Temporary failures are retried with bounded exponential backoff,
jitter, and `Retry-After`; policy and validation refusals are not retried. A
submission whose response was lost is reported as `outcome_unknown`. Plain
HTTP URLs are accepted only for loopback test servers.

## Exit codes

| Exit | Meaning |
| --- | --- |
| 0 | Requested operation completed |
| 2 | Invalid command or missing configuration |
| 3 | Failed protocol, signature, or content check |
| 4 | Unsupported schema or service capability |
| 5 | Incomplete operation, temporary failure, or partial result |
| 6 | Baseline refusal before signing or service policy refusal |

Valid non-baseline bundles pass `verify` with exit 0. `check` exits 6 for a
valid draft outside the baseline unless `--allow-nonbaseline` is given.

## Development

From the repository root:

```sh
uv sync
uv run ruff check python cli
uv run ruff format --check python cli
uv run mypy
uv run pytest python/tests cli/tests
```

`SCI_NETWORK_TESTS=1` also runs a live FreeTSA timestamp test. Tests use a
local HTTP server, a fake TSA built from the corpus's public test-only PKI,
and in-memory GitHub and Hugging Face fakes; they never upload anything.

## License

Apache License 2.0; see [LICENSE](LICENSE), a copy of the repository license.
