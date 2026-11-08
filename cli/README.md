# SCI CLI

See the root README for installation and a complete offline workflow. Every command
has `--help`, including its network use and exit codes. Output is JSON for reliable
scripting. `SCI_HOME` selects state; no index is configured by default.

Local: `check`, `license`, `keygen`, `sign --no-timestamp`, `verify`, `read`, folder
publishing and local `search`. Remote: archive publishing, ID retrieval, submission,
identity lookups and timestamp acquisition. `index` and `folders` only update local
configuration. `claim` creates evidence to host separately; a signature alone does
not prove an account association. `revoke --output` creates a permanent revocation
record but does not submit it.

Exit codes: 0 complete; 2 arguments/configuration; 3 invalid content; 4 unsupported;
5 incomplete/partial; 6 policy or baseline refusal. Valid non-baseline bundles pass
`verify`. `sign` requires `--allow-nonbaseline` before signing them.

FreeTSA is the default with its bundled public CA. Timestamp creation checks the
response and retrieves the published CRL. Offline verification uses configured or
cached CRLs; unavailable trust evidence is reported separately. CLI instances use
a file lock for request spacing; operators sharing an egress across machines must
coordinate the one-request-per-second FreeTSA quota across those machines.

Signing stages a recoverable manifest/signature pair in `.sci-sign-pending`. If a
process stops during replacement, restore both staged files together, remove the
staging directory, and run `sci verify`. Do not continue signing with a pending pair.

See [DESIGN.md](DESIGN.md) for the target behavior and [STATUS.md](../STATUS.md) for
release gates. Reads prefer individual files and report manifest, file or metadata verification
separately. Selected ZIP-range retrieval remains an unimplemented design requirement;
a bounded complete ZIP is the current fallback. `pull` verifies the complete bundle.

`search --cursor TOKEN` resumes the same source/query/filter snapshot. Search limits
are 1–99 per group (default 20), leaving room for a lookahead row within the index’s
100-row response budget. Explicit conflicting options are rejected.

Each command owns its arguments and handler in `sci/commands/<command>.py`. Shared
network operations live in `clients/`; file and cache operations live in `storage/`.
