# Development

Use Python 3.12, uv 0.12.1, Node 22, and OpenSSL 3+. Python 3.11+ is supported.

```sh
uv sync --all-packages --locked
uv run ruff check python cli
uv run ruff format --check python cli
uv run mypy
uv run pytest -q
npm ci --prefix typescript --ignore-scripts
npm run build --prefix typescript
npm test --prefix typescript
```

On macOS, set `SCI_OPENSSL=/opt/homebrew/bin/openssl` when the system executable
is LibreSSL. The tests are offline, including the captured public FreeTSA regression.
Both libraries run the same publication, identity, timestamp, discovery, submission
and template cases. The corpus's HTTP/state scenarios are distinct from byte
verification; service transaction tests live in Sciscape.

Do not modify signed fixtures or their original inventory to make an implementation
pass. Add regressions beside the affected component. Keep protocol changes in
`protocol/README.md` and implementation choices outside it. Keep each package's
license text and notices in its distributable. Never publish packages to a registry
as part of PR checks.
