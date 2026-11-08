# SCI TypeScript library

Strict TypeScript for browsers, Node and Supabase's Deno runtime. Built locally;
no npm publication is planned. The package includes Apache-2.0 LICENSE and NOTICE.

```sh
npm ci --ignore-scripts
npm run build
npm test
npm pack
```

```ts
import { unzip, verify } from '@sci/protocol';
const publication = verify(unzip(downloadedBytes), expectedBareDigest);
console.log(publication.id, publication.baseline);
```

The same validation path serves browser and backend callers. ZIP parsing validates
headers and CRCs in place, without extraction or constructing a second complete
archive. Opaque supporting files are never executed. Callers must still sanitize
rendered text and limit decoded images.

`verifyProofs` uses PKI.js, explicit roots/CRLs and an evaluation time. It validates
CMS, SHA-256 imprint, ESS certificate binding, critical timestamp-only EKU, chain,
current validity and revocation. The caller must first verify the publication or
revocation signature. A bad sidecar never invalidates a valid publication.
