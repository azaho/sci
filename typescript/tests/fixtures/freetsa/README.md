Real, public FreeTSA regression fixture; no private keys are included.

- Publication: `sha256:b2fc187a6c172286ce29fb8ce3edf160f88d97fd0947a69042b9bd580ba4650d`
- Proof source: https://longshelf.azaho.org/bundles/b2fc187a6c172286ce29fb8ce3edf160f88d97fd0947a69042b9bd580ba4650d.tsr
- Signature source: https://longshelf.azaho.org/entries/b2fc187a6c172286ce29fb8ce3edf160f88d97fd0947a69042b9bd580ba4650d/manifest.json.sig
- Trust anchor: FreeTSA's public CA, also distributed with pysci.
- Timestamp: 2026-11-09T06:44:34Z. Tests freeze verification time and do not fetch live CRLs.

The token uses P-384 with SHA-512, which Deno 2.1.4's native WebCrypto
rejects with `NotSupportedError`. This fixture exercises the complete CMS,
certificate chain, imprint and timestamp verification path offline.
