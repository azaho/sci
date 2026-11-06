# PKI.js runtime compatibility patch

Vendored PKI.js 3.3.3 `build/index.es.js`; upstream BSD license is in the file.
Upstream SHA-256: `3be6bd25384c28895cf369aa846dedff8115e72630387cd5463d595fa42209fd`.

Changes: dependencies use pinned Deno npm specifiers. In both `setEngine` and
`getEngine`, the Node-specific branch requires a numeric `process.pid`, rather
than merely a `pid` property. Supabase's runtime has a process shim with an
undefined pid; upstream otherwise tries to assign `global.undefined`, a read-only
property, during module initialization. The patch selects the library's existing
browser/WebCrypto engine storage in this environment. Cryptographic verification
code is unchanged. The TypeScript types still come from the original npm package.
