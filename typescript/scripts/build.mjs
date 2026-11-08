// Post-tsc build steps: copy the repository LICENSE, bundle a single-file ESM, and bundle the adapter.
import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { build } from 'esbuild';

const root = new URL('../', import.meta.url);
const pkg = JSON.parse(await readFile(new URL('package.json', root), 'utf8'));
const deps = Object.entries(pkg.dependencies ?? {})
  .map(([n, v]) => `${n}@${v}`)
  .join(', ');

await copyFile(new URL('../LICENSE', root), new URL('LICENSE', root));

// Single-file, dependency-inlined ESM for vendoring (browser, Deno, Workers, Node).
await build({
  entryPoints: [new URL('src/index.ts', root).pathname],
  outfile: new URL('dist/sci.bundle.js', root).pathname,
  bundle: true,
  format: 'esm',
  platform: 'neutral',
  mainFields: ['module', 'main'],
  conditions: ['import', 'default'],
  target: 'es2022',
  legalComments: 'inline',
  sourcemap: true,
  banner: {
    js: `/*! ${pkg.name} ${pkg.version} — SCI protocol 0.1 library. Apache-2.0. Inlines ${deps} (MIT, (c) Paul Miller). */`,
  },
  logLevel: 'warning',
});
await writeFile(new URL('dist/sci.bundle.d.ts', root), "export * from './index.js';\n");

// Conformance adapter (Node), bundled against the library sources.
await mkdir(new URL('build/', root), { recursive: true });
await build({
  entryPoints: [new URL('tests/adapter.ts', root).pathname],
  outfile: new URL('build/adapter.mjs', root).pathname,
  bundle: true,
  format: 'esm',
  platform: 'node',
  target: 'node22',
  logLevel: 'warning',
});
