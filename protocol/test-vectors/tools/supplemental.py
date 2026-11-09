"""Supplemental fixtures adapted from Claude commit a9f733d9fdd33398d72e09e42feebe18fedfc68d.

Preserves the independent manifest/ZIP builder and all 177 non-timestamp cases.
The 12 timestamp cases are mapped to deterministic equivalents in REVIEW.md.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

HERE = Path(__file__).resolve().parent
LICENSE_PATH = Path(__file__).resolve().parents[2] / "sci-baseline-LICENSE.txt"
BASELINE_LICENSE = LICENSE_PATH.read_bytes()
BASELINE_LICENSE_SHA256 = "8ccfd5fdaa93e5e71dc8812bf11e0a7198bd0a534fa64b8970fd2de58e6c2478"
assert hashlib.sha256(BASELINE_LICENSE).hexdigest() == BASELINE_LICENSE_SHA256, "license bytes changed"

# Ed25519 group order.
L = 2**252 + 27742317777372353535851937790883648493
NOW = "2026-11-07T00:00:00Z"  # Evaluation time for claim expiry vectors.

CASES: list[dict] = []


# ---------------------------------------------------------------- helpers

def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Key:
    def __init__(self, name: str):
        self.name = name
        self.seed = hashlib.sha256(b"sci-test-vectors/key/" + name.encode()).digest()
        self.sk = Ed25519PrivateKey.from_private_bytes(self.seed)
        self.pub = self.sk.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.x = b64u(self.pub)
        self.jwk = {"kty": "OKP", "crv": "Ed25519", "x": self.x}

    def sign(self, msg: bytes) -> bytes:
        return self.sk.sign(msg)


ALICE = Key("alice")
BOB = Key("bob")


def jdump(obj, ensure_ascii=False) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=ensure_ascii).encode()


def manifest_obj(files: dict[str, bytes], key: Key = ALICE) -> dict:
    return {
        "schema": "sci-publication/0.1",
        "public_key": dict(key.jwk),
        "files": {p: {"sha256": sha(files[p]), "bytes": len(files[p])} for p in sorted(files)},
    }


# ZIP (section 2.2)

def zip_entries(entries, *, mutate=None) -> bytes:
    """entries: list of (name_bytes, data). mutate(field_dict) may alter one entry's fields."""
    out = bytearray()
    central = bytearray()
    records = []
    for i, (name, data) in enumerate(entries):
        f = dict(sig=0x04034B50, ver_needed=10, flags=0, method=0, time=0, date=0x0021,
                 crc=zlib.crc32(data) & 0xFFFFFFFF, csize=len(data), usize=len(data),
                 name=name, extra=b"", data=data, ver_made=0x000A, comment=b"",
                 disk=0, int_attr=0, ext_attr=0, c_crc=None, gap=b"")
        if mutate:
            mutate(i, f)
        records.append(f)
    offsets = []
    for f in records:
        offsets.append(len(out))
        out += struct.pack("<IHHHHHIIIHH", f["sig"], f["ver_needed"], f["flags"], f["method"],
                           f["time"], f["date"], f["crc"], f["csize"], f["usize"],
                           len(f["name"]), len(f["extra"]))
        out += f["name"] + f["extra"] + f["data"] + f["gap"]
    cd_start = len(out)
    for f, off in zip(records, offsets):
        crc = f["c_crc"] if f["c_crc"] is not None else f["crc"]
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, f["ver_made"], f["ver_needed"],
                               f["flags"], f["method"], f["time"], f["date"], crc, f["csize"],
                               f["usize"], len(f["name"]), len(f["extra"]), len(f["comment"]),
                               f["disk"], f["int_attr"], f["ext_attr"], off)
        central += f["name"] + f["extra"] + f["comment"]
    out += central
    return bytes(out), cd_start, len(central), len(records)


def eocd(count, cd_size, cd_start, comment=b"") -> bytes:
    return struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, count, count, cd_size, cd_start,
                       len(comment)) + comment


def canonical_zip(manifest: bytes, sig: bytes, files: dict[str, bytes], *, mutate=None,
                  order=None, extra_entries=(), eocd_comment=b"", trailer=b"") -> bytes:
    names = order if order is not None else sorted(files)
    entries = [(b"manifest.json", manifest), (b"manifest.json.sig", sig)]
    entries += [(p.encode(), files[p]) for p in names]
    entries += list(extra_entries)
    body, cd_start, cd_size, n = zip_entries(entries, mutate=mutate)
    return body + eocd(n, cd_size, cd_start, eocd_comment) + trailer


def zip_size_formula(all_entries: dict[str, int]) -> int:
    return 22 + sum(n + 76 + 2 * len(p.encode()) for p, n in all_entries.items())


# ---------------------------------------------------------------- writers

def write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def add_case(**kw):
    newly_valid = {
        'meta-nbsp', 'meta-zwsp', 'meta-rlo', 'meta-soft-hyphen',
        'meta-zwj-no-context', 'meta-zwnj-no-context', 'meta-zwj-at-start',
        'meta-private-use', 'meta-readme-bom', 'claim-note-zwsp',
    }
    if kw['id'] in newly_valid:
        kw['expected'] = 'valid'
        kw['description'] = kw['description'].split(' (fails RFC')[0].rstrip('.') + '; permitted by RFC 9839 Assignables.'
        if kw['kind'] == 'publication':
            kw['baseline'] = False  # These metadata cases use minimal(), without LICENSE.
    if kw['id'] == 'valid-text-rules':
        kw['description'] = 'Multiline text, non-ASCII letters, and unrestricted joiners are valid.'
    assert not any(c["id"] == kw["id"] for c in CASES), kw["id"]
    CASES.append(kw)


def publication(cid, desc, rule, files, *, expected, baseline=None, key=ALICE,
                manifest=None, sig=None, fmt="both", disk_files=None, zip_bytes_override=None,
                scope="complete", extra=None):
    """Write a publication case.

    files: the listed files (used for the manifest unless `manifest` bytes are given).
    disk_files: the bytes actually written (defaults to files); lets a case lie.
    fmt: "both", "loose", "zip", or "manifest" (manifest and signature only).
    """
    m = manifest if manifest is not None else jdump(manifest_obj(files, key))
    s = sig if sig is not None else key.sign(m)
    disk = disk_files if disk_files is not None else files
    base = f"publications/{cid}"
    case = dict(id=cid, kind="publication", rule=rule, description=desc, expected=expected)
    if expected == "valid":
        case["baseline"] = baseline
        case["publication_id"] = "sha256:" + sha(m)
    if fmt == "manifest":
        write(HERE / base / "loose/manifest.json", m)
        write(HERE / base / "loose/manifest.json.sig", s)
        case["input"] = {"format": "loose", "path": base + "/loose"}
        case["scope"] = "manifest"
    else:
        inputs = []
        if fmt in ("both", "loose"):
            write(HERE / base / "loose/manifest.json", m)
            write(HERE / base / "loose/manifest.json.sig", s)
            for p, b in disk.items():
                write(HERE / base / "loose" / p, b)
            inputs.append({"format": "loose", "path": base + "/loose"})
        if fmt in ("both", "zip"):
            z = zip_bytes_override if zip_bytes_override is not None else canonical_zip(m, s, disk)
            name = sha(m) + ".zip"
            write(HERE / base / name, z)
            inputs.append({"format": "zip", "path": f"{base}/{name}"})
        case["inputs"] = inputs
        case["scope"] = scope
        if expected == "valid":
            z = canonical_zip(m, s, files)
            sizes = {"manifest.json": len(m), "manifest.json.sig": len(s)}
            sizes.update({p: len(b) for p, b in files.items()})
            assert zip_size_formula(sizes) == len(z)
            case["zip_bytes"] = len(z)
            case["zip_sha256"] = sha(z)
    if extra:
        case.update(extra)
    add_case(**case)
    return m, s


# ---------------------------------------------------------------- content

README = "# Test publication\n\nThis publication exists only to test SCI readers.\n".encode()


def sci(**fields) -> bytes:
    obj = {"title": "Test publication", "abstract": "A small publication for SCI conformance tests."}
    obj.update(fields)
    return jdump(obj)


def minimal() -> dict[str, bytes]:
    return {"sci.json": sci(), "README.md": README}


def full() -> dict[str, bytes]:
    files = {
        "sci.json": sci(
            categories=[{"scheme": "arxiv", "code": "cs.DL"},
                        {"scheme": "anzsrc-for-2020", "code": "4605"}],
            keywords=["test vectors", "conformance"],
            display=[{"path": "files/figures/fig1.svg", "caption": "Figure 1."},
                     {"path": "files/data/table.csv"}],
            citations=[
                {"target": "doi:10.48550/arXiv.1706.03762", "relations": ["uses"],
                 "reason": "Cited for the method."},
                {"target": "sci:sha256:" + "0" * 64},
                {"target": "https://example.org/data#v1", "relations": ["supports", "reviews"]},
                {"target": "doi:10.1000/182", "relations": []},
            ]),
        "README.md": README,
        "LICENSE": BASELINE_LICENSE,
        "files/data/table.csv": b"a,b\n1,2\n",
        "files/figures/fig1.svg": b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>\n',
        "files/empty.txt": b"",
        "files/code/run.py": b"print('hello')\n",
    }
    return files


def with_sci(files, sci_bytes):
    f = dict(files)
    f["sci.json"] = sci_bytes
    return f


# ---------------------------------------------------------------- cases

def gen_publications():
    F = full()

    # Valid publications
    publication("valid-minimal", "Smallest valid publication: sci.json and README.md, no LICENSE.",
                "2, 3, 4", minimal(), expected="valid", baseline=False)
    publication("valid-baseline", "Valid publication with LICENSE, categories, display items, "
                "citations, nested files, and an empty file. SVG and CSV display items are non-baseline.", "2.1", F, expected="valid",
                baseline=False)
    publication("valid-other-license", "Valid, but LICENSE differs from every approved license, so "
                "not baseline.", "2.1", {**minimal(), "LICENSE": b"Public domain.\n"},
                expected="valid", baseline=False)
    publication("valid-license-crlf", "Baseline license with CRLF line endings: valid, not baseline "
                "(exact bytes required).", "2.1",
                {**minimal(), "LICENSE": BASELINE_LICENSE.replace(b"\n", b"\r\n")},
                expected="valid", baseline=False)
    # Manifest written with unsorted members, whitespace, and escapes.
    f = minimal()
    raw = ('{\n  "files": {\n    "sci.json": {"bytes": %d, "sha256": "%s"},\n'
           '    "README.md": {"sha256": "%s", "bytes": %d}\n  },\n'
           '  "public_key": {"x": "%s", "crv": "Ed25519", "kty": "OKP"},\n'
           '  "\\u0073chema": "sci-publication/0.1"\n}\n') % (
        len(f["sci.json"]), sha(f["sci.json"]), sha(f["README.md"]), len(f["README.md"]), ALICE.x)
    publication("valid-manifest-formatting", "Manifest with whitespace, unsorted members, and a "
                "\\u escape in a key. Valid; the ID is the hash of these exact bytes.",
                "3, 7", f, expected="valid", baseline=False, manifest=raw.encode())
    publication("valid-path-255", "A path of exactly 255 bytes.", "3.1",
                {**minimal(), "files/" + "a" * 249: b"x"}, expected="valid", baseline=False)
    publication("valid-text-rules", "Text that uses TAB/LF/CR where permitted, a ZWJ after a "
                "virama (RFC 5892 A.2), a ZWNJ after a virama (A.1), and non-ASCII letters.",
                "4.2",
                {"sci.json": sci(title="Devक्‍ष क्‌ष "
                                       "été",
                                 abstract="Line one.\nLine two.\r\n\tIndented."),
                 "README.md": "# Übersicht\r\n\n\tText.\n".encode()},
                expected="valid", baseline=False)
    publication("valid-title-200", "Title of exactly 200 code points, abstract of exactly 2,500, "
                "non-ASCII.", "4",
                {"sci.json": sci(title="é" * 200, abstract="一" * 2500),
                 "README.md": README}, expected="valid", baseline=False)
    publication("valid-display-3", "Exactly 3 display items, but their types are non-baseline.", "2.1, 4.3",
                {**F, "sci.json": sci(display=[{"path": "files/data/table.csv"},
                                               {"path": "files/empty.txt"},
                                               {"path": "files/code/run.py"}])},
                expected="valid", baseline=False)

    # Baseline boundaries (valid, not baseline)
    publication("nonbaseline-display-4", "4 display items: valid, not baseline.", "2.1",
                {**F, "sci.json": sci(display=[{"path": p} for p in
                                               ["files/data/table.csv", "files/empty.txt",
                                                "files/code/run.py", "files/figures/fig1.svg"]])},
                expected="valid", baseline=False)
    f = {**minimal(), "LICENSE": BASELINE_LICENSE}
    f.update({f"files/f{i:04d}": b"" for i in range(998)})  # 3 + 998 = 1001 entries
    publication("nonbaseline-1001-files", "1,001 entries in `files`: valid, not baseline.", "2.1",
                f, expected="valid", baseline=False, fmt="zip")
    f = {**minimal(), "LICENSE": BASELINE_LICENSE}
    f.update({f"files/f{i:04d}": b"" for i in range(997)})  # exactly 1,000
    publication("baseline-1000-files", "Exactly 1,000 entries in `files`: baseline.", "2.1",
                f, expected="valid", baseline=True, fmt="zip")
    readme = b"#" + b"a" * (1048576 - 2) + b"\n"
    publication("baseline-readme-1mib", "README.md of exactly 1 MiB: baseline.", "2.1",
                {"sci.json": sci(), "README.md": readme, "LICENSE": BASELINE_LICENSE},
                expected="valid", baseline=True, fmt="loose")
    publication("nonbaseline-readme-over", "README.md of 1 MiB + 1 byte: valid, not baseline.",
                "2.1", {"sci.json": sci(), "README.md": readme + b"\n",
                        "LICENSE": BASELINE_LICENSE}, expected="valid", baseline=False, fmt="loose")
    # sci.json of exactly 65,536 bytes: pad with JSON whitespace (text fields have their own caps).
    base_sci = sci()
    s64 = base_sci[:-1] + b" " * (65536 - len(base_sci)) + b"}"
    assert len(s64) == 65536
    publication("baseline-sci-json-64kib", "sci.json of exactly 64 KiB (JSON whitespace padding): "
                "baseline.", "2.1", {"sci.json": s64, "README.md": README,
                                     "LICENSE": BASELINE_LICENSE}, expected="valid", baseline=True)
    s64b = base_sci[:-1] + b" " * (65537 - len(base_sci)) + b"}"
    publication("nonbaseline-sci-json-over", "sci.json of 64 KiB + 1 byte: valid, not baseline.",
                "2.1", {"sci.json": s64b, "README.md": README, "LICENSE": BASELINE_LICENSE},
                expected="valid", baseline=False)
    # Manifest over 256 KiB with <= 1,000 files: long paths.
    f = {**minimal(), "LICENSE": BASELINE_LICENSE}
    f.update({f"files/{i:04d}" + "p" * 240: b"" for i in range(997)})
    m = jdump(manifest_obj(f))
    assert len(m) > 262144, len(m)
    publication("nonbaseline-manifest-over", f"manifest.json of {len(m):,} bytes (over 256 KiB) "
                "with 1,000 entries: valid, not baseline.", "2.1", f, expected="valid",
                baseline=False, fmt="zip")

    # ZIP size boundary: manifest-only cases, declared sizes.
    def size_case(cid, target, desc):
        # Fixed small sci.json/README/LICENSE plus one big declared file sized so that the
        # canonical ZIP is exactly `target` bytes. Only manifest and signature are provided.
        f = {"sci.json": sci(), "README.md": README, "LICENSE": BASELINE_LICENSE}
        big = "files/big.bin"
        # Signature is always 64 bytes; manifest length depends on the declared size digits.
        for _ in range(3):
            obj = manifest_obj(f)
            obj["files"][big] = {"sha256": "0" * 64, "bytes": 0}
            # iterate to fixed point
            n = 0
            for _ in range(5):
                obj["files"][big]["bytes"] = n
                obj["files"] = dict(sorted(obj["files"].items()))
                m = jdump(obj)
                sizes = {"manifest.json": len(m), "manifest.json.sig": 64}
                sizes.update({p: len(b) for p, b in f.items()})
                sizes[big] = n
                n = n + (target - zip_size_formula(sizes))
            obj["files"][big]["bytes"] = n
            m = jdump(obj)
            sizes = {"manifest.json": len(m), "manifest.json.sig": 64}
            sizes.update({p: len(b) for p, b in f.items()})
            sizes[big] = n
            assert zip_size_formula(sizes) == target
        s = ALICE.sign(m)
        write(HERE / f"publications/{cid}/loose/manifest.json", m)
        write(HERE / f"publications/{cid}/loose/manifest.json.sig", s)
        add_case(id=cid, kind="publication", rule="2.1", description=desc, expected="valid",
                 scope="manifest", input={"format": "loose", "path": f"publications/{cid}/loose"},
                 publication_id="sha256:" + sha(m), zip_bytes=target,
                 within_baseline_zip_limit=target <= 67108864,
                 note="Manifest and signature only. files/big.bin is not supplied; its declared "
                      "size makes the canonical ZIP exactly zip_bytes. Check the size formula "
                      "and the 64 MiB limit from the manifest alone.")

    size_case("zip-size-64mib-exact", 67108864, "Declared sizes give a canonical ZIP of exactly "
              "67,108,864 bytes: within the baseline ZIP limit.")
    size_case("zip-size-64mib-plus-1", 67108865, "Declared sizes give a canonical ZIP of "
              "67,108,865 bytes: over the baseline ZIP limit.")

    # ------------------------------------------------------------ manifest-level failures
    def mcase(cid, desc, rule, mbytes, *, expected="invalid", key=ALICE, sig=None):
        publication(cid, desc, rule, {}, expected=expected, manifest=mbytes, key=key,
                    sig=sig, fmt="manifest")

    good = manifest_obj(minimal())

    def mod(fn):
        o = json.loads(json.dumps(good))
        fn(o)
        return jdump(o)

    gm = jdump(good)
    mcase("manifest-unknown-schema", "schema `sci-publication/0.2`: unsupported, not invalid.",
          "5.1, 6", mod(lambda o: o.update(schema="sci-publication/0.2")), expected="unsupported")
    mcase("manifest-wrong-record-type", "A key-claim schema in manifest.json.", "5.1",
          mod(lambda o: o.update(schema="sci-key-claim/0.1")), expected="invalid")
    mcase("manifest-extra-field", "Extra top-level field `created`.", "3",
          mod(lambda o: o.update(created="2026-11-07")))
    mcase("manifest-missing-files", "No `files` field.", "3", mod(lambda o: o.pop("files")))
    mcase("manifest-jwk-extra-field", "Public JWK with an extra `kid`.", "5.1",
          mod(lambda o: o["public_key"].update(kid="a")))
    mcase("manifest-jwk-wrong-crv", "`crv` is `X25519`.", "5.1",
          mod(lambda o: o["public_key"].update(crv="X25519")))
    # Noncanonical x: last base64url char encodes 2 unused bits; set them.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(ALICE.x[-1])
    nc = ALICE.x[:-1] + alphabet[last | 1 if not last & 1 else last & ~1]
    assert base64.urlsafe_b64decode(nc + "=") == ALICE.pub and nc != ALICE.x
    mcase("manifest-jwk-noncanonical-x", "`x` decodes to the right key but uses nonzero padding "
          "bits (noncanonical base64url).", "5.1",
          mod(lambda o: o["public_key"].update(x=nc)))
    mcase("manifest-jwk-padded-x", "`x` with `=` padding.", "5.1",
          mod(lambda o: o["public_key"].update(x=ALICE.x + "=")))
    mcase("manifest-bad-signature", "Valid manifest, signature with one bit flipped.", "5.1",
          gm, sig=ALICE.sign(gm)[:40] + bytes([ALICE.sign(gm)[40] ^ 1]) + ALICE.sign(gm)[41:])
    mcase("manifest-signature-63-bytes", "Signature file of 63 bytes.", "5.1",
          gm, sig=ALICE.sign(gm)[:63])
    mcase("manifest-signature-other-key", "Signature by a different key than `public_key`.",
          "5.1", gm, sig=BOB.sign(gm))
    s = ALICE.sign(gm)
    S = int.from_bytes(s[32:], "little") + L
    assert S < 2**256
    mcase("manifest-signature-s-not-reduced", "Signature with S + L (S >= L, malleable form).",
          "5.1", gm, sig=s[:32] + S.to_bytes(32, "little"))
    ident = (1).to_bytes(32, "little")  # encoding of the identity point
    weak = mod(lambda o: o["public_key"].update(x=b64u(ident)))
    mcase("manifest-identity-key", "Public key A is the identity point, with signature "
          "(R = identity, S = 0). Passes the cofactorless equation; MUST be rejected.", "5.1",
          weak, sig=ident + b"\x00" * 32)
    mcase("manifest-signature-r-identity", "Signature whose R is the identity point.", "5.1",
          gm, sig=ident + s[32:])
    mcase("manifest-bom", "manifest.json starts with a UTF-8 byte-order mark.", "7",
          b"\xef\xbb\xbf" + gm)
    dup = gm.replace(b'"schema":"sci-publication/0.1"',
                     b'"schema":"sci-publication/0.1","schema":"sci-publication/0.1"', 1)
    mcase("manifest-duplicate-key", "Duplicate `schema` key.", "7", dup)
    dup2 = gm.replace(b'"schema":"sci-publication/0.1"',
                      b'"schema":"sci-publication/0.1","sch\\u0065ma":"sci-publication/0.1"', 1)
    mcase("manifest-duplicate-key-escaped", "Duplicate key that only collides after escape "
          "decoding (`sch\\u0065ma`).", "7", dup2)
    def fileset(fn):
        o = json.loads(json.dumps(good))
        fn(o["files"])
        return jdump(o)

    mcase("manifest-missing-readme", "`files` does not list README.md.", "3",
          fileset(lambda f: f.pop("README.md")))
    mcase("manifest-file-outside-files", "Listed path `notes.txt` outside `files/`.", "3",
          fileset(lambda f: f.update({"notes.txt": {"sha256": "0" * 64, "bytes": 0}})))
    mcase("manifest-lists-signature", "`files` lists `manifest.json.sig`.", "3",
          fileset(lambda f: f.update({"manifest.json.sig": {"sha256": "0" * 64, "bytes": 64}})))
    mcase("manifest-sha256-uppercase", "SHA-256 in uppercase hex.", "3",
          fileset(lambda f: f["README.md"].update(sha256=f["README.md"]["sha256"].upper())))
    mcase("manifest-bytes-leading-zero", "`bytes` written as `012`.", "3",
          gm.replace(b'"bytes":%d' % len(README), b'"bytes":0%d' % len(README)))
    mcase("manifest-bytes-float", "`bytes` written as `12.0`.", "3",
          gm.replace(b'"bytes":%d' % len(README), b'"bytes":%d.0' % len(README)))
    mcase("manifest-bytes-exponent", "`bytes` written as `1e2`.", "3",
          gm.replace(b'"bytes":%d' % len(README), b'"bytes":1e2'))
    mcase("manifest-bytes-too-large", "`bytes` = 9007199254740992 (2^53).", "3",
          fileset(lambda f: f.update(
              {"files/x": {"sha256": "0" * 64, "bytes": 9007199254740992}})))
    mcase("manifest-bytes-string", "`bytes` as a JSON string.", "3",
          fileset(lambda f: f["README.md"].update(bytes=str(len(README)))))
    mcase("manifest-file-extra-field", "File entry with an extra `type` field.", "3",
          fileset(lambda f: f["README.md"].update(type="text/markdown")))

    # Paths (section 3.1)
    bad_paths = [
        ("path-dotdot", "files/../x", "`..` component"),
        ("path-dot", "files/./x", "`.` component"),
        ("path-empty-component", "files//x", "empty component"),
        ("path-trailing-slash", "files/x/", "trailing `/`"),
        ("path-leading-slash", "/files/x", "leading `/`"),
        ("path-trailing-period", "files/x.", "component ending in a period"),
        ("path-space", "files/a b", "space (outside the POSIX portable set)"),
        ("path-non-ascii", "files/ä", "non-ASCII character"),
        ("path-backslash", "files\\x", "backslash"),
        ("path-colon", "files/a:b", "colon"),
        ("path-con", "files/CON", "Windows device name"),
        ("path-con-ext", "files/con.txt", "Windows device name with an extension"),
        ("path-com1-dir", "files/Com1/a", "Windows device name as a directory"),
        ("path-lpt9", "files/lpt9.csv", "Windows device name LPT9 with an extension"),
        ("path-256", "files/" + "a" * 250, "path of 256 bytes"),
    ]
    for cid, p, why in bad_paths:
        mcase("manifest-" + cid, f"Path `{p if len(p) < 40 else p[:20] + '...'}`: {why}.",
              "3.1", fileset(lambda f, p=p: f.update({p: {"sha256": sha(b""), "bytes": 0}})))
    conflicts = [
        ("path-case-conflict", ["files/a", "files/A"], "paths equal ignoring ASCII case"),
        ("path-file-dir-conflict", ["files/a", "files/A/b.txt"],
         "file path names a directory used by another entry"),
        ("path-dir-case-conflict", ["files/Data/a.csv", "files/data/b.csv"],
         "same directory with different capitalization"),
    ]
    for cid, ps, why in conflicts:
        mcase("manifest-" + cid, f"Paths {ps}: {why}.", "3.1",
              fileset(lambda f, ps=ps: f.update({p: {"sha256": sha(b""), "bytes": 0} for p in ps})))
    publication("valid-com10-name", "Component `COM10` and `CONSOLE.txt` are not device names.",
                "3.1", {**minimal(), "files/COM10": b"", "files/CONSOLE.txt": b""},
                expected="valid", baseline=False)

    # ------------------------------------------------------------ file checks (complete bundles)
    f = minimal()
    publication("file-hash-mismatch", "README.md bytes differ from the manifest hash (same "
                "length).", "6.2", f, expected="invalid",
                disk_files={**f, "README.md": README.replace(b"Test", b"Tost")})
    publication("file-size-mismatch", "README.md has one extra byte.", "6.2", f,
                expected="invalid", disk_files={**f, "README.md": README + b"\n"})
    publication("loose-missing-file", "A listed file is absent from the loose files.", "6.4",
                {**f, "files/a.txt": b"a"}, expected="invalid", fmt="loose",
                disk_files=f)
    publication("loose-unlisted-file", "Loose files include an unlisted file.", "6.4", f,
                expected="invalid", fmt="loose", disk_files={**f, "files/extra.txt": b"x"})
    # sidecar allowed in loose inventory
    publication("loose-with-tsr-sidecar-name", "Loose files plus a `manifest.json.sig.tsr` "
                       "file. The sidecar is excluded from the inventory, so the bundle is valid "
                       "(the sidecar content is garbage and is reported separately).", "6.4", f,
                       expected="valid", baseline=False, fmt="loose")
    write(HERE / "publications/loose-with-tsr-sidecar-name/loose/manifest.json.sig.tsr", b"junk")

    # ------------------------------------------------------------ metadata (sci.json, README)
    def meta(cid, desc, rule, sci_bytes=None, readme=None, files_extra=None, expected="invalid"):
        f = minimal()
        if sci_bytes is not None:
            f["sci.json"] = sci_bytes
        if readme is not None:
            f["README.md"] = readme
        if files_extra:
            f.update(files_extra)
        publication(cid, desc, rule, f, expected=expected,
                    baseline=False if expected == "valid" else None, fmt="loose")

    meta("meta-missing-title", "No `title`.", "4", jdump({"abstract": "x"}))
    meta("meta-unknown-field", "Unknown field `authors`.", "4", sci(authors=["A"]))
    meta("meta-blank-title", "Title of spaces only.", "4", sci(title="   "))
    meta("meta-title-201", "Title of 201 code points.", "4", sci(title="a" * 201))
    meta("meta-abstract-2501", "Abstract of 2,501 code points.", "4", sci(abstract="a" * 2501))
    meta("meta-title-null", "`title` is JSON null.", "4", sci(title=None))
    meta("meta-title-tab", "TAB in `title` (permitted only in abstract, reason, README, LICENSE).",
         "4.2", sci(title="a\tb"))
    meta("meta-keyword-lf", "LF in a keyword.", "4.2", sci(keywords=["a\nb"]))
    meta("meta-nbsp", "NO-BREAK SPACE (U+00A0, category Zs) in `title`.", "4.2",
         sci(title="a b"))
    meta("meta-zwsp", "ZERO WIDTH SPACE (U+200B, Default_Ignorable) in `title`.", "4.2",
         sci(title="a​b"))
    meta("meta-rlo", "RIGHT-TO-LEFT OVERRIDE (U+202E) in `abstract`.", "4.2",
         sci(abstract="a‮b"))
    meta("meta-soft-hyphen", "SOFT HYPHEN (U+00AD, Cf, Default_Ignorable) in `title`.", "4.2",
         sci(title="a­b"))
    meta("meta-zwj-no-context", "ZWJ between two Latin letters (fails RFC 5892 A.2).", "4.2",
         sci(title="a‍b"))
    meta("meta-zwnj-no-context", "ZWNJ between two Latin letters (fails RFC 5892 A.1).", "4.2",
         sci(title="a‌b"))
    meta("meta-zwj-at-start", "ZWJ as the first character of a string.", "4.2",
         sci(title="‍क"))
    meta("meta-noncharacter", "Noncharacter U+FDD0 in `title`.", "4.2", sci(title="a﷐"))
    meta("meta-private-use", "Private-use U+E000 in `title`.", "4.2", sci(title="a"))
    meta("meta-escaped-nul", "`\\u0000` in `title`.", "4.2",
         sci().replace(b'"Test publication"', b'"Test\\u0000publication"'))
    meta("meta-unpaired-surrogate", "Unpaired surrogate escape `\\ud800` in `title`.", "7",
         sci().replace(b'"Test publication"', b'"Test \\ud800"'))
    meta("meta-paired-surrogate-escape", "Valid surrogate pair escape `\\ud83d\\ude00` (U+1F600, "
         "category So).", "7", sci().replace(b'"Test publication"', b'"Test \\ud83d\\ude00"'),
         expected="valid")
    meta("meta-invalid-utf8", "sci.json contains an invalid UTF-8 byte.", "7",
         sci().replace(b"Test publication", b"Test \xff publication"))
    meta("meta-overlong-utf8", "sci.json contains an overlong UTF-8 encoding of `/`.", "7",
         sci().replace(b"Test publication", b"Test \xc0\xaf publication"))
    meta("meta-bom", "sci.json starts with a byte-order mark.", "7", b"\xef\xbb\xbf" + sci())
    meta("meta-duplicate-key", "Duplicate `title`.", "7",
         sci().replace(b'{"title"', b'{"title":"x","title"'))
    meta("meta-trailing-data", "Bytes after the JSON value.", "7", sci() + b"x")
    meta("meta-top-level-array", "sci.json is an array.", "4", b"[]")
    meta("meta-categories-4", "4 categories.", "4",
         sci(categories=[{"scheme": "arxiv", "code": c} for c in ["a", "b", "c", "d"]]))
    meta("meta-category-dup", "Duplicate (scheme, code).", "4.1",
         sci(categories=[{"scheme": "arxiv", "code": "cs.DL"}] * 2))
    meta("meta-category-scheme-upper", "Scheme `ArXiv` (uppercase).", "4.1",
         sci(categories=[{"scheme": "ArXiv", "code": "cs.DL"}]))
    meta("meta-category-scheme-65", "Scheme of 65 characters.", "4.1",
         sci(categories=[{"scheme": "a" * 65, "code": "x"}]))
    meta("meta-category-unknown-scheme", "Unregistered scheme `my.scheme-1`: valid "
         "(schemes are not a closed list).", "4.1",
         sci(categories=[{"scheme": "my.scheme-1", "code": "x"}]), expected="valid")
    meta("meta-keywords-21", "21 keywords.", "4", sci(keywords=[str(i) for i in range(21)]))
    meta("meta-keyword-65", "Keyword of 65 code points.", "4", sci(keywords=["k" * 65]))
    meta("meta-display-not-listed", "Display path not in the manifest.", "4.3",
         sci(display=[{"path": "files/missing.png"}]))
    meta("meta-display-not-under-files", "Display path `README.md`.", "4.3",
         sci(display=[{"path": "README.md"}]))
    meta("meta-display-duplicate", "Same display path twice.", "4.3",
         sci(display=[{"path": "files/a.png"}, {"path": "files/a.png"}]),
         files_extra={"files/a.png": b""})
    meta("meta-display-case", "Display path differs from the manifest only by case.", "4.3",
         sci(display=[{"path": "files/A.png"}]), files_extra={"files/a.png": b""})
    meta("meta-display-extra-field", "Display item with an extra `alt` field.", "4.3",
         sci(display=[{"path": "files/a.png", "alt": "x"}]), files_extra={"files/a.png": b""})
    meta("meta-display-caption-2001", "Caption of 2,001 code points.", "4.3",
         sci(display=[{"path": "files/a.png", "caption": "c" * 2001}]),
         files_extra={"files/a.png": b""})
    meta("meta-citation-sci-uppercase", "SCI citation with uppercase hex.", "4.4",
         sci(citations=[{"target": "sci:sha256:" + "A" * 64}]))
    meta("meta-citation-sci-short", "SCI citation with 63 hex characters.", "4.4",
         sci(citations=[{"target": "sci:sha256:" + "a" * 63}]))
    meta("meta-citation-sci-no-algorithm", "SCI citation without `sha256:`.", "4.4",
         sci(citations=[{"target": "sci:" + "a" * 64}]))
    meta("meta-citation-ftp", "Citation target `ftp://example.org/x`.", "4.4",
         sci(citations=[{"target": "ftp://example.org/x"}]))
    meta("meta-citation-relative", "Citation target `/relative/path`.", "4.4",
         sci(citations=[{"target": "/relative/path"}]))
    meta("meta-citation-relation-unknown", "Relation `extends`.", "4.4",
         sci(citations=[{"target": "doi:10.1000/182", "relations": ["extends"]}]))
    meta("meta-citation-relation-dup", "Relation repeated.", "4.4",
         sci(citations=[{"target": "doi:10.1000/182", "relations": ["uses", "uses"]}]))
    meta("meta-citation-relation-string", "`relations` is a string, not an array.", "4.4",
         sci(citations=[{"target": "doi:10.1000/182", "relations": "uses"}]))
    meta("meta-citation-extra-field", "Citation with an extra `title`.", "4.4",
         sci(citations=[{"target": "doi:10.1000/182", "title": "x"}]))
    meta("meta-citation-reason-1001", "Reason of 1,001 code points.", "4.4",
         sci(citations=[{"target": "doi:10.1000/182", "reason": "r" * 1001}]))
    meta("meta-citations-1001", "1,001 citations.", "4",
         sci(citations=[{"target": "doi:10.1000/182"}] * 1001))
    meta("meta-readme-invalid-utf8", "README.md with an invalid UTF-8 byte.", "2, 4.2",
         readme=b"# Title\n\xff\n")
    meta("meta-readme-nul", "README.md containing NUL.", "4.2", readme=b"# Title\n\x00\n")
    meta("meta-readme-bom", "README.md starting with U+FEFF.", "4.2",
         readme="﻿# Title\n".encode())
    meta("meta-readme-escape", "README.md containing ESC (U+001B), used in terminal escapes.",
         "4.2", readme=b"# Title\n\x1b[31mred\n")
    meta("meta-readme-empty", "README.md of zero bytes (README must be nonblank).", "2, 4.2",
         readme=b"")
    meta("meta-readme-whitespace", "README.md of only SPACE, TAB, CR, and LF.", "2, 4.2",
         readme=b" \t\r\n \n")
    meta("meta-license-empty", "Empty LICENSE is permitted (the nonblank rule covers README "
         "only).", "2, 4.2", files_extra={"LICENSE": b""}, expected="valid")
    meta("meta-license-bad-char", "LICENSE containing a form feed (U+000C).", "4.2",
         files_extra={"LICENSE": b"Terms\x0cmore\n"})


def gen_zip():
    F = full()
    m = jdump(manifest_obj(F))
    s = ALICE.sign(m)

    def zcase(cid, desc, z, expected="invalid"):
        base = f"zip/{cid}"
        name = f"{base}/{sha(m)}.zip"
        write(HERE / name, z)
        case = dict(id=cid, kind="publication", rule="2.2", description=desc, expected=expected,
                    scope="complete", inputs=[{"format": "zip", "path": name}],
                    publication_id="sha256:" + sha(m))
        if expected == "valid":
            case["baseline"] = False  # full() selects SVG and CSV display files.
        add_case(**case)

    def one(idx, **changes):
        def mut(i, f):
            if i == idx:
                f.update(changes)
        return mut

    zcase("zip-valid", "Canonical ZIP of `valid-baseline` (control).", canonical_zip(m, s, F),
          expected="valid")
    order = sorted(F)
    zcase("zip-files-unsorted", "Listed files not in ascending byte order.",
          canonical_zip(m, s, F, order=list(reversed(order))))
    body, cds, cdl, n = zip_entries([(b"manifest.json.sig", s), (b"manifest.json", m)] +
                                    [(p.encode(), F[p]) for p in order])
    zcase("zip-signature-first", "manifest.json.sig before manifest.json.", body + eocd(n, cdl, cds))
    zcase("zip-deflate", "manifest.json stored with method 8 (data left uncompressed).",
          canonical_zip(m, s, F, mutate=one(0, method=8)))
    zcase("zip-utf8-flag", "General-purpose flag 0x0800 set on one entry.",
          canonical_zip(m, s, F, mutate=one(2, flags=0x0800)))
    zcase("zip-data-descriptor-flag", "General-purpose flag bit 3 set.",
          canonical_zip(m, s, F, mutate=one(2, flags=0x0008)))
    zcase("zip-extra-field", "Extra field on one entry.",
          canonical_zip(m, s, F, mutate=one(3, extra=b"\x75\x70\x00\x00")))
    zcase("zip-entry-comment", "File comment on one central entry.",
          canonical_zip(m, s, F, mutate=one(3, comment=b"x")))
    zcase("zip-mtime", "Nonzero modification time.",
          canonical_zip(m, s, F, mutate=one(2, time=0x6000)))
    zcase("zip-mdate", "Modification date 2026-11-07.",
          canonical_zip(m, s, F, mutate=one(2, date=((2026 - 1980) << 9) | (11 << 5) | 7)))
    zcase("zip-unix-made-by", "Version made by 0x031E (Unix).",
          canonical_zip(m, s, F, mutate=one(2, ver_made=0x031E)))
    zcase("zip-ext-attr", "External attributes set (Unix mode 0644).",
          canonical_zip(m, s, F, mutate=one(2, ext_attr=0o100644 << 16)))
    zcase("zip-version-needed-20", "Version needed 20.",
          canonical_zip(m, s, F, mutate=one(2, ver_needed=20)))
    zcase("zip-central-crc-mismatch", "Central CRC-32 differs from the local header.",
          canonical_zip(m, s, F, mutate=one(4, c_crc=0)))
    zcase("zip-bad-crc", "CRC-32 wrong in both headers.",
          canonical_zip(m, s, F, mutate=one(4, crc=0, c_crc=0)))
    zcase("zip-gap", "One zero byte between two entries.",
          canonical_zip(m, s, F, mutate=one(2, gap=b"\x00")))
    zcase("zip-trailing-byte", "One byte after the end-of-central-directory record.",
          canonical_zip(m, s, F, trailer=b"\x00"))
    zcase("zip-eocd-comment", "Archive comment in the end record.",
          canonical_zip(m, s, F, eocd_comment=b"x"))
    zcase("zip-directory-entry", "Explicit directory entry `files/`.",
          canonical_zip(m, s, F, extra_entries=[(b"files/", b"")]))
    zcase("zip-unlisted-entry", "Entry not listed in the manifest.",
          canonical_zip(m, s, F, extra_entries=[(b"files/zzz.txt", b"x")]))
    zcase("zip-tsr-entry", "manifest.json.sig.tsr stored inside the ZIP.",
          canonical_zip(m, s, F, extra_entries=[(b"manifest.json.sig.tsr", b"x")]))
    zcase("zip-duplicate-entry", "A listed file appears twice.",
          canonical_zip(m, s, F, order=order + [order[-1]]))
    zcase("zip-missing-entry", "A listed file is missing.",
          canonical_zip(m, s, F, order=order[:-1]))
    # Prepended byte (self-extracting style) with offsets shifted.
    zcase("zip-prefix-byte", "One byte before the first local header.",
          b"\x00" + canonical_zip(m, s, F))
    # Name in headers differs by case from the manifest key.
    def rename(i, f):
        if f["name"] == b"files/empty.txt":
            f["name"] = b"files/Empty.txt"
    zcase("zip-name-case", "ZIP entry name differs from the manifest key by case.",
          canonical_zip(m, s, F, mutate=rename))


# ---------------------------------------------------------------- claims and revocations

def gen_claims():
    def claim(cid, desc, obj, *, key=ALICE, expected, account, sig=None, raw=None):
        b = raw if raw is not None else jdump(obj)
        sg = sig if sig is not None else key.sign(b)
        base = f"claims/{cid}"
        write(HERE / base / f"{ALICE.x}.json", b)
        write(HERE / base / f"{ALICE.x}.json.sig", sg)
        add_case(id=cid, kind="key-claim", rule="5.2, 6.6", description=desc, expected=expected,
                 inputs=[{"format": "claim", "path": f"{base}/{ALICE.x}.json"}],
                 expected_public_key=ALICE.jwk, requested_account=account, now=NOW)

    good = {"schema": "sci-key-claim/0.1", "public_key": ALICE.jwk,
            "account": {"type": "domain", "id": "example.org"},
            "expires": "2027-01-01T00:00:00Z"}
    acct = {"type": "domain", "id": "example.org"}
    claim("claim-valid", "Valid domain claim.", good, expected="valid", account=acct)
    claim("claim-valid-note", "Valid claim with a note and an offset expiry.",
          {**good, "note": "Lab key.", "expires": "2026-12-31T19:00:00-05:00"},
          expected="valid", account=acct)
    claim("claim-github", "Valid GitHub claim.",
          {**good, "account": {"type": "github", "id": "octo-lab"}}, expected="valid",
          account={"type": "github", "id": "octo-lab"})
    claim("claim-expired", "Expired (`expires` before `now`).",
          {**good, "expires": "2026-01-01T00:00:00Z"}, expected="invalid", account=acct)
    claim("claim-account-mismatch", "Claim names example.org; requested example.com.", good,
          expected="invalid", account={"type": "domain", "id": "example.com"})
    claim("claim-type-mismatch", "Claim type `domain`; requested `github` with the same id.",
          good, expected="invalid", account={"type": "github", "id": "example.org"})
    claim("claim-key-mismatch", "Claim names Bob's key, signed by Bob, fetched for Alice.",
          {**good, "public_key": BOB.jwk}, key=BOB, expected="invalid", account=acct)
    claim("claim-bad-signature", "Signed by Bob, names Alice.", good, key=BOB,
          expected="invalid", account=acct)
    claim("claim-no-expires", "`expires` missing.", {k: v for k, v in good.items()
                                                    if k != "expires"},
          expected="invalid", account=acct)
    claim("claim-bad-expires", "`expires` not RFC 3339 (`2027-01-01`).",
          {**good, "expires": "2027-01-01"}, expected="invalid", account=acct)
    claim("claim-extra-field", "Extra field `alias`.", {**good, "alias": "Alice"},
          expected="invalid", account=acct)
    claim("claim-wrong-schema", "Publication schema in a claim.",
          {**good, "schema": "sci-publication/0.1"}, expected="invalid", account=acct)
    claim("claim-domain-uppercase", "Domain id `Example.org` (must be lowercase).",
          {**good, "account": {"type": "domain", "id": "Example.org"}}, expected="invalid",
          account={"type": "domain", "id": "Example.org"})
    claim("claim-domain-ip", "Domain id is an IP address.",
          {**good, "account": {"type": "domain", "id": "192.0.2.1"}}, expected="invalid",
          account={"type": "domain", "id": "192.0.2.1"})
    claim("claim-domain-final-dot", "Domain id with a final dot.",
          {**good, "account": {"type": "domain", "id": "example.org."}}, expected="invalid",
          account={"type": "domain", "id": "example.org."})
    claim("claim-domain-alabel", "Domain id with a valid A-label (`xn--bcher-kva.example`).",
          {**good, "account": {"type": "domain", "id": "xn--bcher-kva.example"}},
          expected="valid", account={"type": "domain", "id": "xn--bcher-kva.example"})
    claim("claim-domain-bad-alabel", "Domain id with an invalid A-label (`xn--a.example`).",
          {**good, "account": {"type": "domain", "id": "xn--a.example"}}, expected="invalid",
          account={"type": "domain", "id": "xn--a.example"})
    claim("claim-note-tab-ok", "Note with TAB and LF (abstract rules permit them).",
          {**good, "note": "Line 1\n\tLine 2"}, expected="valid", account=acct)
    claim("claim-note-zwsp", "Note with U+200B.", {**good, "note": "a​b"},
          expected="invalid", account=acct)

    # Revocations
    def revocation(cid, desc, obj, *, key=ALICE, expected, sig=None, raw=None):
        b = raw if raw is not None else jdump(obj)
        sg = sig if sig is not None else key.sign(b)
        base = f"revocations/{cid}"
        write(HERE / base / "revocation.json", b)
        write(HERE / base / "revocation.json.sig", sg)
        add_case(id=cid, kind="revocation", rule="5.3, 6.7", description=desc, expected=expected,
                 inputs=[{"format": "revocation", "path": base}], revoked_key=ALICE.jwk)
        return b, sg

    rgood = {"schema": "sci-key-revocation/0.1", "public_key": ALICE.jwk}
    rb, rs = revocation("revocation-valid", "Valid revocation of Alice's key.", rgood,
                        expected="valid")
    revocation("revocation-other-signer", "Names Alice, signed by Bob.", rgood, key=BOB,
               expected="invalid")
    revocation("revocation-extra-field", "Extra `reason` field.", {**rgood, "reason": "lost"},
               expected="invalid")
    revocation("revocation-wrong-schema", "Claim schema in a revocation.",
               {**rgood, "schema": "sci-key-claim/0.1"}, expected="invalid")
    revocation("revocation-unknown-version", "Schema `sci-key-revocation/0.2`: unsupported.",
               {**rgood, "schema": "sci-key-revocation/0.2"}, expected="unsupported")
    return rb, rs




def generate_supplemental(out, emit):
    """Convert both representations to the common catalog without running a verifier."""
    global HERE
    CASES.clear()
    with tempfile.TemporaryDirectory(prefix='sci-supplemental-') as td:
        HERE = Path(td)
        gen_publications()
        gen_zip()
        gen_claims()
        for old in CASES:
            kind = old['kind']
            if kind == 'publication':
                for inp in old.get('inputs', [old.get('input')]):
                    path = HERE / inp['path']
                    fmt = inp['format']
                    artifacts = ({'bundle.zip': path.read_bytes()} if fmt == 'zip' else
                                 {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob('*')) if p.is_file()})
                    scope = old.get('scope', 'complete')
                    ctx = {'format': fmt, 'scope': scope}
                    # A known expected ID is useful even for invalid bytes; no guessed
                    # validity, baseline, or cryptographic verdict is supplied here.
                    if fmt == 'loose':
                        ctx['publication_id'] = 'sha256:' + sha(artifacts['manifest.json'])
                    elif old.get('publication_id'):
                        ctx['publication_id'] = old['publication_id']
                    expected = {'outcome': old['expected'], 'baseline': None}
                    if old['expected'] == 'valid':
                        expected['baseline'] = old.get('baseline') if scope == 'complete' else None
                        if 'zip_bytes' in old: expected['zip_bytes'] = old['zip_bytes']
                        if scope == 'complete' and 'zip_sha256' in old:
                            expected['zip_sha256'] = old['zip_sha256']
                        if 'within_baseline_zip_limit' in old:
                            expected['within_baseline_zip_limit'] = old['within_baseline_zip_limit']
                    case = emit('supplemental-' + old['id'] + '-' + fmt, 'publication',
                                old['rule'].replace(' ', ''), old['description'], artifacts, expected, ctx)
                    case['provenance'] = {'corpus': 'claude', 'case_id': old['id']}
            else:
                path = HERE / old['inputs'][0]['path']
                if kind == 'key-claim':
                    raw, sig = path.read_bytes(), Path(str(path) + '.sig').read_bytes()
                    ctx = {'expected_public_key_x': old['expected_public_key']['x'],
                           'requested_account': old['requested_account'], 'now': old['now'],
                           'known_revocation': False, 'retrieved_from_account_location': True}
                    kind = 'claim'
                else:
                    raw, sig = (path / 'revocation.json').read_bytes(), (path / 'revocation.json.sig').read_bytes()
                    ctx = {'expected_public_key_x': old['revoked_key']['x']}
                case = emit('supplemental-' + old['id'], kind, old['rule'].replace(' ', ''),
                            old['description'], {'record.json': raw, 'record.json.sig': sig},
                            {'outcome': old['expected']}, ctx)
                case['provenance'] = {'corpus': 'claude', 'case_id': old['id']}
