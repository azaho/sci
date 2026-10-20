"""Keys, signatures, packaging, preview, PNG checks, and folder verification."""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import zipfile
import zlib

import pytest

from sci import keys
from sci import publication as pub
from sci.publication import PublicationError

from .conftest import make_png, write_folder

# -- keys ------------------------------------------------------------------------


def test_jwk_pem_roundtrip_and_fingerprints(tmp_path):
    priv, pubp, k = keys.write_new_key_pair(tmp_path / "key.pem")
    assert pubp.name == "key.pub.pem"
    assert oct(priv.stat().st_mode & 0o777) == "0o600"
    loaded = keys.load_private_key(priv)
    pk = keys.load_public_key(pubp)
    jwk = keys.jwk_from_public_key(pk)
    assert set(jwk) == {"kty", "crv", "x"} and len(jwk["x"]) == 43 and "=" not in jwk["x"]
    back = keys.public_key_from_jwk(jwk)
    assert keys.raw_public_key(back) == keys.raw_public_key(pk) == keys.raw_public_key(loaded.public_key())
    assert keys.fingerprint(back) == keys.fingerprint(pk)
    with pytest.raises(keys.KeyError_):
        keys.write_new_key_pair(tmp_path / "key.pem")
    assert keys.public_key_path_for(tmp_path / "id") == tmp_path / "id.pub.pem"


@pytest.mark.parametrize("jwk", [
    {"kty": "OKP", "crv": "Ed25519"},
    {"kty": "OKP", "crv": "Ed25519", "x": "AAAA"},
    {"kty": "OKP", "crv": "X25519", "x": "A" * 43},
    {"kty": "EC", "crv": "Ed25519", "x": "A" * 43},
    {"kty": "OKP", "crv": "Ed25519", "x": "A" * 43, "d": "A" * 43},
    {"kty": "OKP", "crv": "Ed25519", "x": "A" * 43 + "="},
    {"kty": "OKP", "crv": "Ed25519", "x": "A" * 42 + "B"},  # nonzero trailing bits
    {"kty": "OKP", "crv": "Ed25519", "x": "A" * 41 + "+/"},
    {"kty": "OKP", "crv": "Ed25519", "x": " " + "A" * 43},
    {"kty": "OKP", "crv": "Ed25519", "x": "A" * 43, "use": "sig"},
])
def test_jwk_rejections(jwk):
    with pytest.raises(keys.KeyError_):
        keys.public_key_from_jwk(jwk)


def test_non_ed25519_keys_rejected(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    k = ec.generate_private_key(ec.SECP256R1())
    p = tmp_path / "ec.pem"
    p.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with pytest.raises(keys.KeyError_):
        keys.load_private_key(p)
    q = tmp_path / "ec.pub.pem"
    q.write_bytes(k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    with pytest.raises(keys.KeyError_):
        keys.load_public_key(q)


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")
def test_openssl_interoperability(tmp_path, key):
    folder = write_folder(tmp_path / "r")
    pub.sign_folder(folder, key)
    pem = tmp_path / "k.pub.pem"
    pem.write_bytes(keys.public_key_pem(key.public_key()))
    r = subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(pem),
                        "-in", str(folder / "manifest.json"), "-sigfile", str(folder / "manifest.json.sig")], capture_output=True)
    assert r.returncode == 0, r.stderr
    # OpenSSL signs; SCI verifies.
    priv = tmp_path / "o.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(priv)], check=True, capture_output=True)
    okey = keys.load_private_key(priv)
    folder2 = write_folder(tmp_path / "r2")
    pub.sign_folder(folder2, okey)
    sig = subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(priv), "-in", str(folder2 / "manifest.json")],
                         capture_output=True, check=True).stdout
    assert len(sig) == 64
    (folder2 / "manifest.json.sig").write_bytes(sig)
    pub.verify_folder(folder2)


# -- signing and verification --------------------------------------------------------


def test_sign_preserves_author_bytes_and_generates_files(tmp_path, key):
    raw = b'{\n  "abstract": "A",\n    "title": "T"   }\n'
    folder = write_folder(tmp_path / "r", sci_bytes=raw)
    readme = (folder / "README.md").read_bytes()
    res = pub.sign_folder(folder, key)
    assert (folder / "sci.json").read_bytes() == raw and (folder / "README.md").read_bytes() == readme
    assert (folder / "LICENSE").read_bytes() == pub.license_bytes()
    assert len((folder / "manifest.json.sig").read_bytes()) == 64
    m = json.loads((folder / "manifest.json").read_bytes())
    assert list(m) == ["schema_version", "uuid", "public_key", "created_at", "files_tree", "files"]
    assert m["files_tree"] == {"paths": ["code/", "data/", "notes.txt", "data/raw/", "code/analyze.py"], "truncated": False}
    with zipfile.ZipFile(folder / "files.zip") as z:
        assert "LICENSE" not in z.namelist() and "data/raw/a.csv" in z.namelist()
    docs = pub.verify_folder(folder)
    assert docs.uuid == res.uuid
    # Signing again creates a new UUID and the same deterministic ZIP.
    zip1 = (folder / "files.zip").read_bytes()
    res2 = pub.sign_folder(folder, key)
    assert res2.uuid != res.uuid and (folder / "files.zip").read_bytes() == zip1


def test_custom_license_is_never_replaced(tmp_path, key):
    folder = write_folder(tmp_path / "r")
    (folder / "LICENSE").write_text("All rights reserved\n")
    with pytest.raises(PublicationError) as e:
        pub.sign_folder(folder, key)
    assert e.value.code == "unapproved_license"
    assert (folder / "LICENSE").read_text() == "All rights reserved\n"
    assert not (folder / "manifest.json").exists()


@pytest.mark.parametrize("target", ["README.md", "sci.json", "LICENSE", "files.zip", "headline_figure.png", "manifest.json", "manifest.json.sig"])
def test_verify_detects_changed_and_missing_files(signed, target):
    folder, _ = signed
    p = folder / target
    original = p.read_bytes()
    p.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(PublicationError):
        pub.verify_folder(folder)
    p.write_bytes(original)
    pub.verify_folder(folder)
    p.unlink()
    with pytest.raises(PublicationError):
        pub.verify_folder(folder)


def test_verify_trusted_key(signed, tmp_path):
    folder, _ = signed
    other = keys.generate_private_key()
    with pytest.raises(PublicationError) as e:
        pub.verify_folder(folder, other.public_key())
    assert e.value.code == "untrusted_key"


def test_signature_must_be_raw_64_bytes(signed):
    folder, _ = signed
    sig = (folder / "manifest.json.sig").read_bytes()
    import base64

    for bad in (sig + b"\n", base64.b64encode(sig), sig[:63]):
        (folder / "manifest.json.sig").write_bytes(bad)
        with pytest.raises(PublicationError):
            pub.verify_folder(folder)


def test_symlinks_and_special_files_rejected(tmp_path, key):
    folder = write_folder(tmp_path / "r")
    os.symlink("/etc/passwd", folder / "files" / "link")
    with pytest.raises(PublicationError, match="symbolic link"):
        pub.sign_folder(folder, key)
    os.unlink(folder / "files" / "link")
    os.symlink(folder / "README.md", folder / "headline_figure.png.tmp")
    pub.sign_folder(folder, key)  # other files in the folder are ignored
    os.unlink(folder / "headline_figure.png")
    os.symlink(folder / "README.md", folder / "headline_figure.png")
    with pytest.raises(PublicationError):
        pub.sign_folder(folder, key)


def test_unsafe_names_rejected(tmp_path, key):
    folder = write_folder(tmp_path / "r", files={"a\\b.txt": b"x"})
    with pytest.raises(PublicationError, match="backslash"):
        pub.sign_folder(folder, key)
    folder = write_folder(tmp_path / "r2", files={"bad\nname": b"x"})
    with pytest.raises(PublicationError):
        pub.sign_folder(folder, key)


def test_prepared_zip_and_empty_zip(tmp_path, key):
    folder = write_folder(tmp_path / "r", files={})
    with zipfile.ZipFile(folder / "files.zip", "w") as z:
        z.writestr("deep/a/b/c.txt", "x")
        z.writestr("top.txt", "y")
        z.writestr("empty/", "")
    pub.sign_folder(folder, key)
    m = json.loads((folder / "manifest.json").read_bytes())
    assert m["files_tree"] == {"paths": ["deep/", "empty/", "top.txt", "deep/a/"], "truncated": False}
    folder2 = write_folder(tmp_path / "r2", files={})
    zipfile.ZipFile(folder2 / "files.zip", "w").close()
    pub.sign_folder(folder2, key)
    assert json.loads((folder2 / "manifest.json").read_bytes())["files_tree"] == {"paths": [], "truncated": False}
    folder3 = write_folder(tmp_path / "r3", files={})
    with zipfile.ZipFile(folder3 / "files.zip", "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(PublicationError):
        pub.sign_folder(folder3, key)


def test_empty_directories_in_files(tmp_path, key):
    folder = write_folder(tmp_path / "r", files={"a/x.txt": b"1"})
    (folder / "files" / "empty").mkdir()
    (folder / "files" / "a" / "nested_empty").mkdir()
    pub.sign_folder(folder, key)
    m = json.loads((folder / "manifest.json").read_bytes())
    assert m["files_tree"]["paths"] == ["a/", "empty/", "a/nested_empty/", "a/x.txt"]


def test_no_zip_without_files(tmp_path, key):
    folder = write_folder(tmp_path / "r", files={})
    pub.sign_folder(folder, key)
    m = json.loads((folder / "manifest.json").read_bytes())
    assert "files.zip" not in m["files"] and "files_tree" not in m


# -- preview ------------------------------------------------------------------------


def test_preview_bounded_for_million_entries():
    c = pub.PreviewCollector()

    def names():
        for i in range(1_000_000):
            yield f"dir{i % 300:03d}/sub{i % 7}/file{i}.bin"

    for n in names():
        c.add_entry(n, False)
    tree = c.tree()
    assert len(tree["paths"]) == 200 and tree["truncated"] is True
    assert tree["paths"][0] == "dir000/" and all(p.endswith("/") and p.count("/") == 1 for p in tree["paths"])
    assert len(pub.compact_json_bytes(tree)) <= pub.PREVIEW_MAX_BYTES
    pub.validate_files_tree(tree)


def test_preview_group_order_and_byte_limit():
    c = pub.PreviewCollector()
    for n in ["z.txt", "b/x.txt", "a/", "a/y/", "a/b.txt", "c/d/e/f.txt"]:
        c.add_entry(n.rstrip("/"), n.endswith("/"))
    assert c.tree() == {"paths": ["a/", "b/", "c/", "z.txt", "a/y/", "c/d/", "a/b.txt", "b/x.txt"], "truncated": False}
    c = pub.PreviewCollector()
    for i in range(150):
        c.add_entry("é" * 30 + f"{i:03d}.txt", False)  # long non-ASCII names
    tree = c.tree()
    assert tree["truncated"] and len(pub.compact_json_bytes(tree)) <= 8192
    assert all(p.endswith(".txt") for p in tree["paths"])  # names never shortened
    pub.validate_files_tree(tree)


def test_combined_json_limit_trims_preview(tmp_path, key):
    files = {f"d{i:03d}/{'n' * 30}{j}.txt": b"x" for i in range(100) for j in range(2)}
    big_abstract = "a" * 2500
    cits = [{"target": f"doi:10.1000/{'x' * 480}{i:05d}", "reason": "r" * 1000} for i in range(40)]
    folder = write_folder(tmp_path / "r", sci={"title": "T", "abstract": big_abstract, "citations": cits}, files=files)
    res = pub.sign_folder(folder, key)
    total = len((folder / "sci.json").read_bytes()) + len((folder / "manifest.json").read_bytes())
    assert total <= 65536 and res.files_tree["truncated"] is True
    # Metadata too large even without preview paths.
    cits = [{"target": f"doi:10.1000/{'x' * 480}{i:05d}", "reason": "r" * 1000} for i in range(50)]
    folder2 = write_folder(tmp_path / "r2", sci={"title": "T", "abstract": "A", "citations": cits}, files={})
    with pytest.raises(PublicationError, match="limit"):
        pub.sign_folder(folder2, key)


def test_preview_edit_invalidates_signature(signed):
    folder, _ = signed
    m = (folder / "manifest.json").read_bytes()
    (folder / "manifest.json").write_bytes(m.replace(b'"notes.txt"', b'"other.txt"'))
    with pytest.raises(PublicationError) as e:
        pub.verify_folder(folder)
    assert e.value.code == "invalid_signature"


# -- PNG ------------------------------------------------------------------------------


def _chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)


@pytest.mark.parametrize("kw", [dict(), dict(color=6), dict(color=0, depth=1), dict(color=3, depth=4), dict(color=4, depth=16),
                                dict(width=1600, height=1600), dict(width=1, height=1), dict(width=13, height=7, interlace=1)])
def test_valid_pngs(kw):
    pub.check_png(make_png(**kw))


def test_invalid_pngs():
    good = make_png()
    cases = {
        "not png": b"GIF89a" + good[6:],
        "too wide": make_png(width=1601, height=1),
        "too many pixels": make_png(width=1600, height=1601),
        "animated": make_png(extra_chunks=[(b"acTL", struct.pack(">II", 2, 0))]),
        "unknown critical": make_png(extra_chunks=[(b"ABCD", b"")]),
        "bad crc": good[:-5] + bytes([good[-5] ^ 1]) + good[-4:],
        "truncated": good[:-12],
        "trailing data": good + b"x",
        "bad depth": make_png(color=2, depth=4),
        "palette without PLTE": good.replace(struct.pack(">IIBBBBB", 4, 3, 8, 2, 0, 0, 0), struct.pack(">IIBBBBB", 4, 3, 8, 3, 0, 0, 0)),
    }
    # Short image data.
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 3, 8, 2, 0, 0, 0))
    cases["short data"] = sig + ihdr + _chunk(b"IDAT", zlib.compress(b"\x00" * 5)) + _chunk(b"IEND", b"")
    cases["bad filter"] = sig + ihdr + _chunk(b"IDAT", zlib.compress((b"\x07" + b"\x00" * 12) * 3)) + _chunk(b"IEND", b"")
    cases["corrupt zlib"] = sig + ihdr + _chunk(b"IDAT", b"\x78\x9c\xff\xff") + _chunk(b"IEND", b"")
    for name, data in cases.items():
        with pytest.raises(PublicationError):
            pub.check_png(data)
            pytest.fail(name)
    with pytest.raises(PublicationError) as e:
        pub.check_png(b"\x00" * 2_000_001)
    assert "Resize" in str(e.value)


def test_oversized_figure_fails_signing(tmp_path, key):
    folder = write_folder(tmp_path / "r")
    (folder / "headline_figure.png").write_bytes(make_png(width=2000, height=10))
    with pytest.raises(PublicationError, match="1-1600"):
        pub.sign_folder(folder, key)


def test_readme_rules(tmp_path, key):
    folder = write_folder(tmp_path / "r", readme=b"\xff\xfe bad")
    with pytest.raises(PublicationError, match="UTF-8"):
        pub.sign_folder(folder, key)
    folder = write_folder(tmp_path / "r2", readme=b"![img](files/x.png) and <img src=x>\n")
    pub.sign_folder(folder, key)  # image syntax is display policy, not an admission rule


def test_attachment_total_limit(tmp_path, key):
    folder = write_folder(tmp_path / "r", files={"big.bin": os.urandom(1024)})
    big = folder / "files" / "big.bin"
    with open(big, "wb") as f:
        for _ in range(50):
            f.write(os.urandom(1_000_000))
    with pytest.raises(PublicationError, match="50000000"):
        pub.sign_folder(folder, key)


def test_failed_resign_keeps_previous_signed_folder(tmp_path, key):
    folder = write_folder(tmp_path / "r")
    pub.sign_folder(folder, key)
    before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    (folder / "files" / "big.bin").write_bytes(os.urandom(50_000_001))
    with pytest.raises(PublicationError):
        pub.sign_folder(folder, key)
    after = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    assert after == before
    pub.verify_folder(folder)


def test_oversized_inputs_rejected_before_reading(tmp_path, key):
    folder = write_folder(tmp_path / "r", readme=b"x" * 1_000_001)
    with pytest.raises(PublicationError) as e:
        pub.sign_folder(folder, key)
    assert e.value.code == "too_large" and not (folder / "LICENSE").exists()
