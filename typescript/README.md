# @sci/core

TypeScript implementation of the [SCI protocol 0.1](../protocol/README.md). It runs in browsers, Deno, Cloudflare Workers, and Node 22+ and uses only Web-standard APIs (`Uint8Array`, `TextDecoder`, `crypto.subtle`, `fetch`, `Blob`). The only runtime dependencies are `@noble/curves` and `@noble/hashes`. The package is local only and is not published to npm.

## Build and check

```sh
npm ci
npm run check   # typecheck, lint, build, Node tests, Deno tests, bundle smoke test, conformance corpus
```

| Script | Purpose |
| --- | --- |
| `build` | `tsc` to `dist/` (ESM + `.d.ts`), copies `../LICENSE`, writes `dist/sci.bundle.js` (single file, dependencies inlined) and `build/adapter.mjs` |
| `typecheck` | Strict `tsc` over `src/` and `tests/` |
| `lint` | Biome (formatting + lint rules; Node built-ins are banned in `src/`) |
| `test` / `test:deno` | The same `tests/*.test.ts` files under `node --test` and `deno test` |
| `conformance` | `run_adapter.py` against `build/adapter.mjs` (run `build` first) |

## Vendoring

Copy `dist/sci.bundle.js` (and optionally `dist/sci.bundle.js.map`) into the consumer. It is self-contained ESM:

```ts
import { openPublication, verifyZipBundle, citationTargetKey } from './vendor/sci.bundle.js';
```

For types, also copy `dist/*.d.ts`. `dist/sci.bundle.d.ts` re-exports `./index.js`.

## Usage

All validators return or throw structured issues (`code`, `outcome`, `requirement`, `path`, `observed`, `message`). The outcomes are `invalid` (a failed check), `unsupported` (an unknown schema), and `incomplete` (unavailable data or a local budget). Use `capture`/`captureAsync` or the `check*` wrappers to get a `CheckResult` instead of an exception.

```ts
// Browser reader: verify displayed files using an archive descriptor and an expected ID.
const pub = await openPublication([descriptorUrl], digest, { fetch });
const { metadata, readme } = await pub.readMetadata();          // §6.1–§6.3
const image = await pub.readFile(metadata.display?.[0]?.path ?? 'README.md'); // §6.2 via entry_url, blob_url, or ZIP ranges
const full = await pub.verifyComplete();                         // §6.4

// Local ZIP chosen in the browser, verified with bounded memory.
const bundle = await verifyZipBundle(blobSource(file), { expectedId: digest });
```

Backends should inject a `fetch` that enforces their outbound address policy.
