"""Generate synthetic, freshly signed draft-0.18 ZIPs. Never use a real key."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def generate(out: Path, small=False):
    out.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    public = {"kty": "OKP", "crv": "Ed25519", "x": base64.urlsafe_b64encode(raw).decode().rstrip("=")}
    size = 100_000 if small else 50_000_000
    cases = []
    configs = [
        ("baseline", 1_000_000 if not small else 10_000, 1, None),
        ("50mb-one", size, 1, None),
        ("50mb-1000", size, 1000, None),
        ("50mb-5000", size, 5000, None),
        ("50mb-large-manifest", size, 1, "padding"),
        ("bad-final-hash", size, 1000, "hash"),
        ("bad-signature", 10_000, 1, "signature"),
        ("duplicate-json", 10_000, 1, "duplicate"),
        ("trailing-bytes", 10_000, 1, "trailing"),
        ("central-name", 10_000, 1, "central"),
        ("manifest-over-limit", 10_000, 1, "oversize"),
    ]
    for name, payload_size, count, fault in configs:
        files = {"README.md": b"Synthetic benchmark publication.\n",
                 "sci.json": b'{"title":"Benchmark","abstract":"Synthetic data for verification timing."}'}
        remaining = payload_size - sum(map(len, files.values()))
        for i in range(count):
            n = remaining // (count - i)
            pattern = hashlib.sha256(f"fixture-{i}".encode()).digest()
            files[f"files/{i:05d}.bin"] = (pattern * ((n + 31) // 32))[:n]
            remaining -= n
        doc = {"schema": "sci-publication/0.18", "public_key": public,
               "files": {p: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
                         for p, b in sorted(files.items())}}
        manifest = json.dumps(doc, separators=(",", ":")).encode()
        if fault in ("padding", "oversize"):
            manifest += b" " * (1_048_576 + (1 if fault == "oversize" else 0) - len(manifest))
        if fault == "duplicate":
            manifest = manifest.replace(b'{"schema":', b'{"schema":"ignored","schema":', 1)
        sig = bytearray(key.sign(manifest))
        if fault == "signature":
            sig[0] ^= 1
        if fault == "hash":
            last = sorted(files)[-2]  # sci.json sorts last; corrupt the last supporting file.
            files[last] = files[last][:-1] + bytes([files[last][-1] ^ 1])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as z:
            for path, data in [("manifest.json", manifest), ("manifest.json.sig", bytes(sig)), *sorted(files.items())]:
                info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
                info.create_system = 0
                info.create_version = info.extract_version = 10
                info.external_attr = 0x20  # zipfile would replace zero with POSIX permissions.
                z.writestr(info, data)
        blob = bytearray(buf.getvalue())
        central = int.from_bytes(blob[-6:-2], "little")
        pos = central
        while blob[pos:pos + 4] == b"PK\x01\x02":
            blob[pos + 38:pos + 42] = b"\0" * 4
            name_len = int.from_bytes(blob[pos + 28:pos + 30], "little")
            pos += 46 + name_len
        if fault == "central":
            blob[central + 46] ^= 1
        if fault == "trailing":
            blob += b"x"
        (out / f"{name}.zip").write_bytes(blob)
        cases.append({"name": name, "id": hashlib.sha256(manifest).hexdigest(),
                      "zip_bytes": len(blob), "payload_bytes": payload_size,
                      "files": len(files), "manifest_bytes": len(manifest),
                      "expected_error": {"hash": "file_hash", "signature": "signature",
                                         "duplicate": "json_duplicate", "trailing": "zip_end",
                                         "central": "zip_directory", "oversize": "manifest_limit"}.get(fault)})
    (out / "cases.json").write_text(json.dumps(cases, indent=2) + "\n")
    return cases


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--small", action="store_true")
    args = parser.parse_args()
    print(json.dumps(generate(args.output, args.small), indent=2))
