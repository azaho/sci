# SCI

[![Checks](https://github.com/azaho/sci-dev/actions/workflows/checks.yml/badge.svg)](https://github.com/azaho/sci-dev/actions/workflows/checks.yml)
![Protocol](https://img.shields.io/badge/protocol-0.1%20draft-blue)
![License](https://img.shields.io/badge/license-Apache--2.0-green)

Sign research locally, host it independently, and verify its exact bytes offline.
This repository contains the SCI protocol, Python and TypeScript libraries, and CLI.
Protocol 0.1 and these packages are **alpha**; no stable release or registry package
has been published. The existing service at [sci.azaho.org](https://sci.azaho.org/)
continues independently.

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Git, and OpenSSL 3+ for
optional timestamp trust verification. Install both packages from the same Git ref:

```sh
uv tool install \
  --with 'pysci @ git+https://github.com/azaho/sci-dev.git#subdirectory=python' \
  'sci-cli @ git+https://github.com/azaho/sci-dev.git#subdirectory=cli'
sci --help
```

While reviewing the initial PR, insert `@codex/build-sci-sciscape` before each `#`
in that command. For reproducible installations, use the same commit hash in both
URLs. No Sciscape source or account is needed.

## First local workflow

Keep this alpha's state separate from an existing SCI installation:

```sh
export SCI_HOME="$PWD/.sci-example-state"
mkdir -p draft
printf '%s\n' '{"title":"A first result","abstract":"An independently verifiable example."}' > draft/sci.json
printf '# A first result\n\nDescribe the methods, evidence, and limitations here.\n' > draft/README.md
sci check draft
sci keygen
sci license draft
sci sign draft --no-timestamp --output campaign
sci verify draft
sci read draft
sci search result --in campaign
```

`license` explicitly selects the protocol's baseline CC BY 4.0/MIT publication
terms. Project source is Apache-2.0; publication licenses remain independent.
Keep private keys outside draft and campaign directories. The default state
location is `~/.sci/`; the owner handles any legacy CLI transition and backup.

Omit `--no-timestamp` to request a FreeTSA proof, or retry with `sci timestamp draft`.
Timestamp failure preserves signed files and returns exit 5. There is no automatic
fallback TSA. Set `SCI_OPENSSL` if the OpenSSL 3+ executable is not on PATH.

## Host and discover

```sh
sci publish campaign --to https://github.com/OWNER/REPO --create
sci publish campaign --to https://huggingface.co/datasets/OWNER/REPO --create
sci submit draft --to https://sciscape.io --archive ARCHIVE_DESCRIPTOR_URL
sci pull sha256:FULL_DIGEST --archive ARCHIVE_DESCRIPTOR_URL --output retrieved
```

Use `gh auth login` or `hf auth login` for provider credentials. Remote commands
require actual public archives and an available index; local verification does not.
Sciscape's production deployment is a separate release gate.

- [Protocol](protocol/README.md) and [shared test corpus](protocol/test-vectors/README.md)
- [CLI instructions](cli/README.md) and [design](cli/DESIGN.md)
- [Python](python/README.md) and [TypeScript](typescript/README.md)
- [Development and tests](CONTRIBUTING.md), [source provenance](SOURCE.md)
- [Implementation status and release gates](STATUS.md), [plan audit](PLAN-AUDIT.md)
