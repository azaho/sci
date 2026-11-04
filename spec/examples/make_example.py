"""Rebuild the public draft example. Requires Python 3.10+ and cryptography.

This creates one fixture. It is not a protocol implementation or signing CLI.
The seed is public test material. Never use it for a real publication.
"""

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parent
PREFIX = b"SCI-PUBLICATION\x00sci-publication/0.1\x00"
TEST_SEED = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc4"
    "4449c5697b326919703bac031cae7f60"
)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def descriptor(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def main():
    target = ROOT / "minimal"
    target.mkdir(exist_ok=True)
    key = Ed25519PrivateKey.from_private_bytes(TEST_SEED)
    raw_public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    objects = {
        "sci.json": json_bytes({
            "title": "A three-value sample has a mean of two",
            "abstract": "The values 1, 2, and 3 have an arithmetic mean of 2. This publication is a protocol example.",
        }),
        "README.md": (
            "# A three-value sample has a mean of two\n\n"
            "The sample contains 1, 2, and 3. Their sum is 6.\n"
            "Divide the sum by the sample count, 3. The mean is 2.\n\n"
            "This is a protocol example, not a claim of a new scientific result.\n\n"
            "Attribution: SCI protocol example contributors.\n"
        ).encode("utf-8"),
        "LICENSE": (ROOT.parents[1] / "sci" / "publication_LICENSE.txt").read_bytes(),
    }
    inventory = json_bytes({
        "schema": "sci-inventory/0.1",
        "files": {name: descriptor(data) for name, data in objects.items()},
    })
    manifest = json_bytes({
        "schema": "sci-publication/0.1",
        "uuid": "c8af05c1-c45b-471c-9e84-c39bedb1a462",
        "public_key": {
            "kty": "OKP", "crv": "Ed25519",
            "x": base64.urlsafe_b64encode(raw_public).rstrip(b"=").decode("ascii"),
        },
        "created_at": "2026-11-04T12:00:00Z",
        "inventory": descriptor(inventory),
    })
    objects.update({
        "inventory.json": inventory,
        "manifest.json": manifest,
        "manifest.json.sig": key.sign(PREFIX + manifest),
    })
    for name, data in objects.items():
        (target / name).write_bytes(data)
    expected = {
        "operation": "complete_verified",
        "content_id": "sha256:" + descriptor(manifest)["sha256"],
        "fingerprint": "ed25519:" + hashlib.sha256(raw_public).hexdigest(),
        "public_key_hex": raw_public.hex(),
        "signature_hex": objects["manifest.json.sig"].hex(),
        "signature_input_prefix_hex": PREFIX.hex(),
        "objects": {name: descriptor(data) for name, data in objects.items()},
    }
    (ROOT / "expected.json").write_bytes(json_bytes(expected))
    base_url = "https://mirror.example/objects/"
    location = {
        "schema": "sci-location/0.1",
        "manifest": descriptor(manifest),
        "signature_urls": [base_url + descriptor(objects["manifest.json.sig"])["sha256"]],
        "objects": [
            {**descriptor(data), "urls": [base_url + descriptor(data)["sha256"]]}
            for name, data in sorted(objects.items()) if name != "manifest.json.sig"
        ],
    }
    (ROOT / "catalog.json").write_bytes(json_bytes({
        "schema": "sci-catalog/0.1", "publications": [location],
    }))
    print(expected["content_id"])


if __name__ == "__main__":
    main()
