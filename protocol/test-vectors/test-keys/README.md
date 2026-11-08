# Public test-only keys

These three RSA private keys were generated solely for this test corpus and are
intentionally committed. They contain no production credentials. Anyone can
forge certificates and timestamp proofs under them. Never trust the test CA,
install it in a system trust store, or use these keys outside isolated tests.

The fixture generator reuses these keys to produce deterministic certificates,
CRLs, and RSA PKCS#1 v1.5 signatures. Publisher Ed25519 keys are derived from a
public, fixed test seed in `tools/generate.py`; they are also unsuitable for real
publications. No private key is included inside any publication bundle.
