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

Requires Python 3.11 or later. Neither package is on PyPI yet; install the CLI
and its `pysci` dependency from Git:

```sh
uv tool install --with "pysci @ git+https://github.com/azaho/sci-dev#subdirectory=python" \
  "sci-cli @ git+https://github.com/azaho/sci-dev#subdirectory=cli"
sci --help
```

With pip (inside a virtual environment):

```sh
pip install "pysci @ git+https://github.com/azaho/sci-dev#subdirectory=python" \
  "sci-cli @ git+https://github.com/azaho/sci-dev#subdirectory=cli"
sci --help
```

[`tests/install_check.sh`](tests/install_check.sh) runs both commands in a clean
temporary environment, followed by the local workflow below. It accepts a
repository URL and ref, so it also works with a local `git+file://` clone:

```sh
cli/tests/install_check.sh https://github.com/azaho/sci-dev main
```

From a checkout, `uv sync --all-packages` at the repository root installs both
packages for development (`uv run sci --help`).

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

2. Register the hosted copies with an index (no index is configured by default):

   ```sh
   sci index https://sciscape.io
   sci submit ./campaign \
     --archive https://raw.githubusercontent.com/lab/research/main/sci/archive.json
   ```

   Registration makes the publication retrievable by ID. Sciscape adds it to
   its searchable catalog only with a qualifying account association; create
   one with `sci claim domain lab.example --output ./claims`, host the two
   files where the command says, and submit again.

3. Find and retrieve publications:

   ```sh
   sci search "neural decoding" --in @index --in @local
   sci read sha256:<digest>                    # verified README by ID
   sci pull sha256:<digest>                    # complete verified copy in ./<digest>/
   sci pull sha256:<digest> --archive https://raw.githubusercontent.com/lab/research/main/sci/archive.json
   ```

Timestamp proofs can be submitted to an index with
`sci timestamp ./campaign/<digest>.zip --submit`. A key revocation is created
with `sci revoke --output DIR` (permanent; keep it private until needed) and
sent with `sci revoke --submit DIR`.

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
uv sync --all-packages
uv run ruff check python cli
uv run ruff format --check python cli
uv run mypy --config-file cli/pyproject.toml python/pysci cli/sci
uv run pytest python/tests cli/tests
```

`SCI_NETWORK_TESTS=1` also runs a live FreeTSA timestamp test. Tests use a
local HTTP server, a fake TSA built from the corpus's public test-only PKI,
and in-memory GitHub and Hugging Face fakes; they never upload anything.

## License

Apache License 2.0; see [LICENSE](LICENSE), a copy of the repository license.
