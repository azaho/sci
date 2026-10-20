"""Encrypt saved Terraform plans before they leave a trusted runner."""

import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["encrypt", "decrypt"])
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    key = base64.b64decode(os.environ["TF_PLAN_ENCRYPTION_KEY"], validate=True)
    if len(key) != 32:
        raise SystemExit("TF_PLAN_ENCRYPTION_KEY must be base64 of 32 random bytes")
    context = (os.environ["GITHUB_REPOSITORY"] + ":" + os.environ["GITHUB_SHA"] + ":" +
               os.environ["GITHUB_RUN_ID"]).encode()
    data = args.source.read_bytes()
    cipher = AESGCM(key)
    if args.operation == "encrypt":
        nonce = os.urandom(12)
        result = nonce + cipher.encrypt(nonce, data, context)
    else:
        result = cipher.decrypt(data[:12], data[12:], context)
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    args.destination.write_bytes(result)
    args.destination.chmod(0o600)


if __name__ == "__main__":
    main()
