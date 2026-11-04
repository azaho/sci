# Worked publication example

This example follows [draft 0.1](../../sci-spec.md).
The files in [minimal](minimal/) contain the exact signed bytes.
They are test data, not a publication for production use.

```text
minimal/
├── manifest.json
├── manifest.json.sig
├── inventory.json
├── sci.json
├── README.md
└── LICENSE
```

`manifest.json` binds the inventory. The inventory binds three logical files.
The manifest and inventory are protocol objects, not logical file entries.
`LICENSE` is an exact copy of the existing SCI publication license.
There is no figure, supporting file, or archive in this minimal example.

## 1. Identity and exact bytes

Publication UUID:

```text
c8af05c1-c45b-471c-9e84-c39bedb1a462
```

Content identifier:

```text
sha256:fa2cf2ec12a9227b0c67e46a6700ac71501a40ec5e779ac43a4702ee204af63c
```

Publisher fingerprint:

```text
ed25519:21fe31dfa154a261626bf854046fd2271b7bed4b6abe45aa58877ef47f9721b9
```

The public test seed appears in [make_example.py](make_example.py).
Anyone can sign with this seed. Never use it for a real publication.
The date in the manifest is example data, not a timestamp proof.

Each JSON file uses UTF-8, sorted object keys, no indentation, and one final LF byte.
Other valid JSON layouts are permitted by the protocol.
Changing the layout changes the hash and requires a new signature.

## 2. Check the hash chain

1. Read the 310 bytes of `manifest.json` and the 64 bytes of `manifest.json.sig`.
2. Compare the manifest SHA-256 with the content identifier above.
3. Decode the public JWK. Verify the signature over the prefix followed by the manifest bytes.
4. Check the 346 inventory bytes against the descriptor in the manifest.
5. Parse the verified inventory. Check the three file paths and descriptors.
6. Check each file's byte count and SHA-256, then its applicable content rules.

| Logical file | Bytes | SHA-256 |
| --- | ---: | --- |
| `sci.json` | 157 | `c59b58a5d77b8f115be18829cb8bb4c68622ca0e74b9b38a92791a9cfc3d803f` |
| `README.md` | 263 | `1acba21a077f64c987540a9b8c75a06e0b4d175f8e554c744834d1fee41ea91f` |
| `LICENSE` | 22239 | `fed84172ce5540c20b6c51ffee014941c61e0371f34875fa1114b9ac0db99b44` |

[expected.json](expected.json) contains all six object descriptors, the public key, and the signature in hexadecimal.
It also identifies `complete_verified` as the expected protocol operation result.
The complete record includes the README and license; metadata alone is insufficient for that result.

## 3. Discover and mirror it

[catalog.json](catalog.json) gives one location record for this publication.
Its `mirror.example` URLs are placeholders. They are not live download locations.
The example host serves each object at `/objects/<sha256>`.
This route is a host convention, not a required SCI route.

A second host can serve identical bytes at different URLs.
Change the location record, or add its URLs to the source arrays.
Do not change the manifest, inventory, signature, or logical files.
An importer must verify retrieved bytes even when an indexer supplied the catalog.

## 4. Check or rebuild the fixture

Run from the repository root, with Python 3.10 or later and `cryptography` installed:

```sh
python spec/examples/check.py
python spec/examples/check.py --openssl /path/to/openssl-3
```

The checker validates all stored hashes, the signature, and the catalog references.
It also checks that altered signature inputs fail.
The OpenSSL option checks the signature with a second backend.
This script does not implement the full schema, path, text, or strict point validation rules.
It is a fixture check, not evidence that a protocol library conforms.

To reproduce the fixture files:

```sh
python spec/examples/make_example.py
```

Generation is deterministic. Review any changed bytes and expected values together.
