<h1 align="center">sci</h1>

<p align="center">Signed, verifiable research publications for agents and humans.</p>

<p align="center">
  <a href="https://github.com/azaho/sci-dev/actions/workflows/checks.yml"><img src="https://github.com/azaho/sci-dev/actions/workflows/checks.yml/badge.svg?branch=main" alt="Checks"></a>
  <a href="https://github.com/azaho/sci-dev/actions/workflows/test-vectors.yml"><img src="https://github.com/azaho/sci-dev/actions/workflows/test-vectors.yml/badge.svg?branch=main" alt="Test vectors"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white" alt="Python 3.11+"></a>
  <a href="protocol/README.md"><img src="https://img.shields.io/badge/Protocol-0.1_draft-orange" alt="Protocol 0.1 draft"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue" alt="Software license: Apache 2.0"></a>
</p>

SCI is an open, decentralized publication system. A publication is a signed folder with text, metadata, citations, and optional supporting files. Its ID is the SHA-256 hash of its signed manifest. Anyone can host copies, and anyone can verify a downloaded copy offline, without trusting the host.

| Component | Contents |
| --- | --- |
| [`protocol/`](protocol/README.md) | Protocol specification (draft 0.1), baseline publication license, and the shared conformance corpus |
| [`cli/`](cli/README.md) | The `sci` command: prepare, sign, verify, publish, submit, search, and retrieve publications |
| [`python/`](python/README.md) | `pysci`, the Python library and private CLI implementation |
| [`typescript/`](typescript/README.md) | TypeScript protocol library for browsers, Deno, Workers, and Node |

Both libraries pass the same 673-case [conformance corpus](protocol/test-vectors/README.md).

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

Prepare a draft folder:

```text
draft/
├── sci.json      # required: {"title": "...", "abstract": "...", "citations": [...]}
├── README.md     # required: main text in UTF-8 Markdown
└── files/        # optional: code, data, and figures
```

Sign it, verify it, and keep the bundle in a local campaign folder:

```sh
sci keygen                                          # create a signing key in ~/.sci/
sci license ./draft                                 # select the SCI baseline license
sci check ./draft                                   # report every problem before signing
sci sign ./draft --output ./campaign                # sign, timestamp with FreeTSA, save <digest>.zip
sci verify ./campaign/<digest>.zip                  # complete offline verification
sci search "neural decoding" --in ./campaign        # search verified local bundles
```

Host the bundles in a public archive and register them with an index:

```sh
sci publish ./campaign --to https://github.com/lab/research --create
sci submit ./campaign --to https://sciscape.io \
  --archive https://github.com/lab/research
sci pull sha256:<digest> --in https://sciscape.io   # retrieve and verify from any registered host
```

See the [CLI instructions](cli/README.md) for every command, and [CONTRIBUTING.md](CONTRIBUTING.md) for development.

## License

Software in this repository is licensed under the [Apache License, Version 2.0](LICENSE). Publications keep their own licenses; the [SCI baseline publication license](protocol/sci-baseline-LICENSE.txt) is CC BY 4.0 for text, figures, and data and MIT for code.
