# pysci

Python implementation of the [SCI protocol](../protocol/README.md) (draft 0.1):
publication verification, drafting and signing, key claims and revocations,
RFC 3161 timestamps, and the discovery/submission interfaces. The `sci` CLI is
included in the same `pysci` distribution. Its implementation under `pysci._cli`
is private; library users should use the public modules below. Run the command
as `sci` or `python -m pysci`; see the [CLI guide](../cli/README.md) and
[CLI design](../cli/DESIGN.md).

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

## First workflow

```python
from pathlib import Path
from pysci import check_draft, sign_draft, verify_zip, write_bundle_zip, generate_private_key
from pysci.baseline import baseline_license_bytes

draft = Path("draft")  # sci.json, README.md, files/...
(draft / "LICENSE").write_bytes(baseline_license_bytes())

report = check_draft(draft)  # all problems at once, with file/field/observed
for issue in report.issues:
    print(issue)
print("baseline:", report.baseline.eligible if report.baseline else None, report.zip_bytes)

key = generate_private_key()
signed = sign_draft(draft, key)  # raises BaselineRefused unless allow_nonbaseline=True
with open(signed.publication_id.removeprefix("sha256:") + ".zip", "wb") as out:
    write_bundle_zip(
        out,
        signed.manifest_bytes,
        signed.signature,
        {p: (draft / p).read_bytes() for p in signed.verification.manifest.files},
    )

result = verify_zip(out.name, expected_id=signed.publication_id)
print(result.outcome, result.baseline)  # Outcome.VALID True
```

Verification returns a `VerificationResult` with `outcome` (`valid`, `invalid`,
`unsupported`, `incomplete`), `baseline` (`True`/`False`, or `None` unless the
complete bundle verified), and structured `issues` (`code`, `requirement`,
`section`, `file`, `field` as a JSON pointer, `observed`). Local limits
(`VerificationLimits`) yield `incomplete`, never `invalid`. ZIPs are verified
without extraction in one streaming pass with bounded memory.

## Modules

| Module | Purpose |
| --- | --- |
| `bundle` | Verify ZIP/loose/directory bundles (§6.1–6.4), single files (§6.2), canonical ZIP writing, safe extraction |
| `draft` | Draft checks and signing (§2–4, §2.1) |
| `manifest`, `metadata` | Manifest (§3) and `sci.json` (§4) parsing, stable manifest serializer |
| `zipformat` | Strict §2.2 ZIP layout parser, canonical writer, §2.1 size formula |
| `baseline` | Approved license bytes (bundled), baseline evaluation |
| `records`, `accounts` | Key claims (§5.2, §6.6), revocations (§5.3, §6.7), account methods and hosting URLs |
| `timestamps`, `der` | RFC 3161 requests, proof verification with trust anchors and CRLs (§6.8) |
| `discovery`, `templates`, `submission` | Descriptors, RFC 6570 expansion, lookups, §8.1.4 submission |
| `jsonstrict`, `text`, `paths`, `ids`, `citations`, `keys`, `ed25519` | §7 JSON, §4.2 text, §3.1 paths, IDs, citation keys, JWKs, Ed25519 with subgroup checks |

## Timestamp defaults

`pysci.timestamps.FREETSA_URL` is `https://freetsa.org/tsr`. The FreeTSA root
certificate from <https://freetsa.org/files/cacert.pem> is bundled as
`pysci/data/freetsa-cacert.pem` with SHA-256
`2151b61137ffa86bf664691ba67e7da0b19f98c758e3d228d5d8ebf27e044438`
(checked at load time and in the tests). `freetsa_trust()` builds a trust
configuration from it; pass FreeTSA's CRL for a full revocation check.

## Checks

From the repository root:

```sh
uv run ruff check python
uv run ruff format --check python
uv run mypy
uv run pytest
```

`pytest` includes the conformance run. To run it directly from the repository root:

```sh
python3 protocol/test-vectors/tools/run_adapter.py \
  --kind publication --kind claim --kind revocation --kind timestamp \
  --kind discovery --kind submission --kind template --kind http -- \
  uv run python python/tests/adapter.py
```

This is a subset pass of 663 of the 673 cases: the 10 `scenario` cases describe
stateful indexer behavior (admission, registration, retention), which belongs
to a service rather than this protocol library. Set `SCI_NETWORK_TESTS=1` to
also run a live FreeTSA round trip.

## License

Apache License 2.0; see [repository license](../LICENSE). The root package build includes the repository license in source and wheel
distributions.
