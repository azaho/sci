# Contributing to SCI

## Layout

| Path | Contents |
| --- | --- |
| `protocol/` | Authoritative protocol specification, baseline publication license, and shared conformance corpus |
| `python/` | `pysci`, the Python protocol library |
| `cli/` | The `sci` command-line tool, built on `pysci` |
| `typescript/` | TypeScript protocol library used by Sciscape's backend and browser reader |

Keep protocol rules in the libraries. Keep command arguments, output, and local settings in the CLI.
Keep implementation decisions out of `protocol/README.md`. Use README files for operating instructions and DESIGN files for intended behavior.

## Setup

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 22, Deno 2, and OpenSSL 3 or later for the corpus audit.

```sh
uv sync --all-packages          # Python workspace: pysci and the CLI
(cd typescript && npm ci)       # TypeScript library
```

## Checks

Run the same checks as CI before opening a pull request:

```sh
# Python library and CLI
uv run ruff check python cli
uv run ruff format --check python cli
uv run mypy --strict python/pysci cli/sci
uv run pytest python/tests cli/tests

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
- Put tests beside their component.
- Pin dependencies through `uv.lock` and `typescript/package-lock.json`.
- Do not edit signed JSON or binary fixtures by hand. Regenerate them with `protocol/test-vectors/tools/generate.py` after reviewing a protocol change.
- After protocol `0.1` is released, changes to accepted bytes or their meaning require a new schema version.

## License

Contributions are licensed under the [Apache License, Version 2.0](LICENSE). Publication bundles, including test fixtures, keep their own licenses.
