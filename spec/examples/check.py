"""Check example bytes and signatures, not general protocol conformance.

Requires Python 3.10+ and cryptography. --openssl adds an independent backend.
Run without -O: assertions are intentional checks of this fixed fixture.
"""

import argparse
import base64
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ROOT = Path(__file__).resolve().parent


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openssl", help="Path to an OpenSSL 3 executable")
    args = parser.parse_args()
    if not __debug__:
        raise SystemExit("Run without -O so fixture assertions are enabled.")
    expected = json.loads((ROOT / "expected.json").read_bytes())
    folder = ROOT / "minimal"
    objects = {name: (folder / name).read_bytes() for name in expected["objects"]}
    assert {p.name for p in folder.iterdir()} == set(objects)
    for name, data in objects.items():
        assert {"bytes": len(data), "sha256": digest(data)} == expected["objects"][name], name

    manifest_bytes = objects["manifest.json"]
    manifest = json.loads(manifest_bytes)
    assert manifest["schema"] == "sci-publication/0.1"
    assert manifest["inventory"] == expected["objects"]["inventory.json"]
    inventory = json.loads(objects["inventory.json"])
    assert inventory["schema"] == "sci-inventory/0.1"
    assert set(inventory["files"]) == {"sci.json", "README.md", "LICENSE"}
    for name, desc in inventory["files"].items():
        assert desc == expected["objects"][name], name

    raw_key = base64.urlsafe_b64decode(manifest["public_key"]["x"] + "=")
    assert raw_key.hex() == expected["public_key_hex"]
    assert expected["fingerprint"] == "ed25519:" + digest(raw_key)
    assert expected["content_id"] == "sha256:" + digest(manifest_bytes)
    prefix = b"SCI-PUBLICATION\x00sci-publication/0.1\x00"
    assert prefix.hex() == expected["signature_input_prefix_hex"]
    message = prefix + manifest_bytes
    signature = objects["manifest.json.sig"]
    assert len(signature) == 64 and signature.hex() == expected["signature_hex"]
    public_key = Ed25519PublicKey.from_public_bytes(raw_key)
    public_key.verify(signature, message)

    catalog = json.loads((ROOT / "catalog.json").read_bytes())
    assert set(catalog) == {"schema", "publications"}
    assert catalog["schema"] == "sci-catalog/0.1" and len(catalog["publications"]) == 1
    location = catalog["publications"][0]
    assert set(location) == {"schema", "manifest", "signature_urls", "objects"}
    assert location["schema"] == "sci-location/0.1"
    assert location["manifest"] == expected["objects"]["manifest.json"]
    base_url = "https://mirror.example/objects/"
    assert location["signature_urls"] == [base_url + digest(signature)]
    assert len(location["objects"]) == len(objects) - 1
    actual_sources = {item["sha256"]: item for item in location["objects"]}
    assert len(actual_sources) == len(location["objects"])
    for name, desc in expected["objects"].items():
        if name != "manifest.json.sig":
            assert actual_sources[desc["sha256"]] == {**desc, "urls": [base_url + desc["sha256"]]}

    # Negative controls establish exact-byte and domain-prefix binding.
    for changed in (manifest_bytes, message + b"\n", message[:-1] + b" "):
        try:
            public_key.verify(signature, changed)
        except InvalidSignature:
            pass
        else:
            raise AssertionError("Changed signature input was accepted")
    print("PASS: all six object hashes and sizes, inventory chain, identity, and signature")
    print("PASS: unprefixed and altered signature inputs rejected")
    print("PASS: example catalog references match the publication objects")

    if args.openssl:
        with tempfile.TemporaryDirectory(prefix="sci-spec-example-") as tmp:
            tmp = Path(tmp)
            (tmp / "public.pem").write_bytes(public_key.public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            (tmp / "message.bin").write_bytes(message)
            (tmp / "signature.bin").write_bytes(signature)
            command = [
                args.openssl, "pkeyutl", "-verify", "-rawin", "-pubin",
                "-inkey", str(tmp / "public.pem"), "-in", str(tmp / "message.bin"),
                "-sigfile", str(tmp / "signature.bin"),
            ]
            subprocess.run(command, check=True, capture_output=True)
            (tmp / "message.bin").write_bytes(message + b"\n")
            result = subprocess.run(command, capture_output=True)
            assert result.returncode != 0, "OpenSSL accepted changed bytes"
        print("PASS: OpenSSL independently accepted the signature and rejected changed bytes")


if __name__ == "__main__":
    main()
