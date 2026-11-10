# Contributing to SCI

## Layout

| Path | Contents |
| --- | --- |
| `protocol/` | Authoritative protocol specification, baseline publication license, and shared conformance corpus |
| `python/` | `pysci`, the Python protocol library and private `_cli` implementation |
| `python/tests/` | Library tests and CLI tests in `cli/` |
| `cli/` | Command guide and design; implementation in `python/pysci/_cli/`, tests in `python/tests/cli/` |
| `typescript/` | TypeScript protocol library used by Sciscape's backend and browser reader |

The supported Python API lives in `pysci`; `_cli` contains private command
implementation details. Run the command as `sci` or `python -m pysci`. Tests
stay outside the installed package, so wheels contain only runtime code and data.

Keep protocol rules in the libraries. Keep command arguments, output, and local settings in the CLI.
Keep implementation decisions out of `protocol/README.md`. Use README files for operating instructions and DESIGN files for intended behavior.

## Setup

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 22, Deno 2, and OpenSSL 3 or later for the corpus audit.

```sh
uv sync                         # One sci-protocol package, including the sci command
(cd typescript && npm ci)       # TypeScript library
```

## Checks

Run the same checks as CI before opening a pull request:

```sh
# Python library and CLI
uv run ruff check python
uv run ruff format --check python
uv run mypy
uv run pytest

# TypeScript library: type checks, lint, Node and Deno tests, conformance
(cd typescript && npm run check)

# Conformance corpus integrity
python3 -m venv /tmp/sci-vectors-venv
/tmp/sci-vectors-venv/bin/pip install -r protocol/test-vectors/requirements.txt
/tmp/sci-vectors-venv/bin/python protocol/test-vectors/tools/audit.py --openssl
/tmp/sci-vectors-venv/bin/python protocol/test-vectors/tools/check_reproducible.py
```

Both libraries must pass the same corpus cases. Report a subset pass as a subset, not as whole-protocol conformance.

## Rules

- Typed Python with strict mypy, Ruff, and pytest. Strict TypeScript with tests in Node and Deno.
- Validate external files, JSON, and network responses at runtime.
- Keep command and HTTP entry points small; put shared operations in separate modules.
- Keep Python tests in `python/tests/` (CLI tests in `python/tests/cli/`) and TypeScript tests in `typescript/tests/`.
- Pin dependencies through `uv.lock` and `typescript/package-lock.json`.
- `python/pysci/data/sci-protocol.md` is the copy of `protocol/README.md` that `sci spec` prints. Update both together; a test checks that they match and that the hash equals the corpus pin.
- Do not edit signed JSON or binary fixtures by hand. Regenerate them with `protocol/test-vectors/tools/generate.py` after reviewing a protocol change.
- After protocol `0.1` is released, changes to accepted bytes or their meaning require a new schema version.

## Releasing

For package publishing and the one-time PyPI setup, see
[Releasing sci-protocol](python/RELEASING.md).

## License

Contributions are licensed under the [Apache License, Version 2.0](LICENSE). Publication bundles, including test fixtures, keep their own licenses.
