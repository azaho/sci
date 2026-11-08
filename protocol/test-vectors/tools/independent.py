"""Independent publication/record audit adapted from Claude's checker.

Deliberately shares no parsing, text, ZIP, path, or Ed25519 validation code with
 audit.py. Both use cryptography for the final Ed25519 equation. Not a production
 verifier: URI, RFC 3339 leap-second, and external-account checks are bounded to
 the corpus. Timestamp proofs are independently cross-checked by OpenSSL.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import subprocess
import sys
import tempfile
import idna
import ipaddress
from functools import lru_cache
import zlib
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
BASELINE_LICENSES = {hashlib.sha256((ROOT / "sci-baseline-LICENSE.txt").read_bytes()).hexdigest()}


class Invalid(Exception):
    pass


class Unsupported(Exception):
    pass


def sha(b):
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------------------ JSON (section 7)

class JsonInteger(int):
    def __new__(cls, raw):
        obj = super().__new__(cls, raw)
        obj.raw = raw
        return obj


def strict_json(b: bytes):
    if b.startswith(b"\xef\xbb\xbf"):
        raise Invalid("BOM")
    try:
        text = b.decode("utf-8")
    except UnicodeDecodeError as e:
        raise Invalid(f"UTF-8: {e}")

    def pairs(ps):
        d = {}
        for k, v in ps:
            if k in d:
                raise Invalid(f"duplicate key {k!r}")
            d[k] = v
        return d

    def bad_const(c):
        raise Invalid(f"nonstandard number {c}")

    try:
        obj = json.loads(text, object_pairs_hook=pairs, parse_constant=bad_const, parse_int=JsonInteger)
    except json.JSONDecodeError as e:
        raise Invalid(f"JSON: {e}")

    def walk(o):
        if isinstance(o, str):
            if any(0xD800 <= ord(c) <= 0xDFFF for c in o):
                raise Invalid("unpaired surrogate")
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(k)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return obj


def exact_keys(o, required, optional=()):
    if not isinstance(o, dict):
        raise Invalid("not an object")
    missing = set(required) - o.keys()
    extra = o.keys() - set(required) - set(optional)
    if missing or extra:
        raise Invalid(f"fields: missing {sorted(missing)}, extra {sorted(extra)}")


# ------------------------------------------------------------ Ed25519 (section 5.1)

P = 2**255 - 19
Lord = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def decode_point(b: bytes):
    y = int.from_bytes(b, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= P:
        raise Invalid("noncanonical point")
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P:
        raise Invalid("not a point")
    if x == 0 and sign:
        raise Invalid("noncanonical point")
    if x & 1 != sign:
        x = P - x
    return (x, y, 1, x * y % P)


def padd(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A = (y1 - x1) * (y2 - x2) % P
    B = (y1 + x1) * (y2 + x2) % P
    C = 2 * t1 * t2 * D % P
    Dd = 2 * z1 * z2 % P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % P, G * H % P, F * G % P, E * H % P)


def pmul(k, p):
    q = (0, 1, 1, 0)
    while k:
        if k & 1:
            q = padd(q, p)
        p = padd(p, p)
        k >>= 1
    return q


def is_identity(p):
    x, y, z, _ = p
    return x % P == 0 and (y - z) % P == 0


@lru_cache(maxsize=1024)
def check_point(b: bytes, what):
    pt = decode_point(b)
    if is_identity(pt):
        raise Invalid(f"{what} is the identity")
    if not is_identity(pmul(Lord, pt)):
        raise Invalid(f"{what} not in the prime-order subgroup")


def jwk_key(j) -> bytes:
    exact_keys(j, ["kty", "crv", "x"])
    if j["kty"] != "OKP" or j["crv"] != "Ed25519" or not isinstance(j["x"], str):
        raise Invalid("JWK kty/crv")
    x = j["x"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", x):
        raise Invalid("x encoding")
    raw = base64.urlsafe_b64decode(x + "=")
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != x:
        raise Invalid("noncanonical x")
    return raw


def verify_sig(pub: bytes, sig: bytes, msg: bytes):
    if len(sig) != 64:
        raise Invalid("signature length")
    check_point(pub, "A")
    check_point(sig[:32], "R")
    if int.from_bytes(sig[32:], "little") >= Lord:
        raise Invalid("S >= L")
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
    except InvalidSignature:
        raise Invalid("bad signature")


def check_schema(o, expected):
    s = o.get("schema") if isinstance(o, dict) else None
    if not isinstance(s, str):
        raise Invalid("schema missing")
    known = {"sci-publication/0.1", "sci-key-claim/0.1", "sci-key-revocation/0.1",
             "sci-index/0.1", "sci-archive/0.1"}
    if s != expected:
        if s in known:
            raise Invalid("wrong record type")
        raise Unsupported(s)


# ------------------------------------------------------------ paths (section 3.1)

DEVICE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)


def check_paths(paths):
    for p in paths:
        if len(p.encode("utf-8")) > 255:
            raise Invalid("path too long")
        comps = p.split("/")
        for c in comps:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", c):
                raise Invalid(f"component {c!r}")
            if c in (".", "..") or c.endswith("."):
                raise Invalid(f"component {c!r}")
            if DEVICE.match(c):
                raise Invalid(f"device name {c!r}")
    lower = {}
    dirs = {}
    for p in paths:
        k = p.lower()
        if k in lower:
            raise Invalid("case conflict")
        lower[k] = p
        comps = p.split("/")
        for i in range(1, len(comps)):
            d = "/".join(comps[:i])
            if d.lower() in dirs and dirs[d.lower()] != d:
                raise Invalid("directory capitalization")
            dirs[d.lower()] = d
    for d in dirs:
        if d in lower:
            raise Invalid("file/directory conflict")


# ------------------------------------------------------------ manifest (sections 3, 6.1)

def check_manifest(mb: bytes, sig: bytes):
    m = strict_json(mb)
    check_schema(m, "sci-publication/0.1")
    exact_keys(m, ["schema", "public_key", "files"])
    pub = jwk_key(m["public_key"])
    files = m["files"]
    if not isinstance(files, dict):
        raise Invalid("files")
    for p, e in files.items():
        exact_keys(e, ["sha256", "bytes"])
        if not isinstance(e["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", e["sha256"]):
            raise Invalid("sha256")
        n = e["bytes"]
        if not isinstance(n, JsonInteger) or not re.fullmatch(r"0|[1-9][0-9]*", n.raw) or not 0 <= n <= 9007199254740991:
            raise Invalid("bytes")
    for req in ("sci.json", "README.md"):
        if req not in files:
            raise Invalid(f"missing {req}")
    for p in files:
        if p not in ("sci.json", "README.md", "LICENSE") and not p.startswith("files/"):
            raise Invalid(f"path {p!r} not allowed")
    check_paths(list(files))
    # Preserve the lexical spelling: -0 must not become an acceptable zero.
    verify_sig(pub, sig, mb)
    return m


# ------------------------------------------------------------ text (section 4.2)

# Literal positive intervals from RFC 9839 section 4.3, independently of the
# audit.py exclusion predicate. No runtime Unicode database is used.
ASSIGNABLE = [(9,10),(13,13),(0x20,0x7e),(0xa0,0xd7ff),
              (0xe000,0xfdcf),(0xfdf0,0xfffd)]
ASSIGNABLE += [(plane << 16, (plane << 16) + 0xfffd) for plane in range(1,17)]


def check_text(s: str, multiline: bool, nonblank: bool, maxlen=None):
    if not isinstance(s,str): raise Invalid("not text")
    if maxlen is not None and len(s)>maxlen: raise Invalid("too long")
    for ch in s:
        cp=ord(ch)
        if not any(lo<=cp<=hi for lo,hi in ASSIGNABLE): raise Invalid("not assignable")
        if ch in "\t\n\r" and not multiline: raise Invalid("single-line control")
    if nonblank and not any(ch not in " \t\n\r" for ch in s): raise Invalid("blank")


def text_file(b: bytes, nonblank=False):
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        raise Invalid("UTF-8")
    check_text(s, True, nonblank)


def check_sci(b: bytes, files: dict):
    o = strict_json(b)
    exact_keys(o, ["title", "abstract"], ["categories", "keywords", "display", "citations"])

    def keys_ok(x):
        if isinstance(x, dict):
            for k, v in x.items():
                check_text(k, False, False)
                keys_ok(v)
        elif isinstance(x, list):
            for v in x:
                keys_ok(v)
    keys_ok(o)

    def string(v, ml, maxlen):
        if not isinstance(v, str):
            raise Invalid("not a string")
        check_text(v, ml, True, maxlen)

    string(o["title"], False, 200)
    string(o["abstract"], True, 2500)
    cats = o.get("categories", [])
    if not isinstance(cats, list) or len(cats) > 3:
        raise Invalid("categories")
    seen = set()
    for c in cats:
        exact_keys(c, ["scheme", "code"])
        if not isinstance(c["scheme"], str) or not re.fullmatch(r"[a-z][a-z0-9.-]{0,63}",
                                                               c["scheme"]):
            raise Invalid("scheme")
        string(c["code"], False, 128)
        if (c["scheme"], c["code"]) in seen:
            raise Invalid("duplicate category")
        seen.add((c["scheme"], c["code"]))
    kws = o.get("keywords", [])
    if not isinstance(kws, list) or len(kws) > 20:
        raise Invalid("keywords")
    for k in kws:
        string(k, False, 64)
    disp = o.get("display", [])
    if not isinstance(disp, list):
        raise Invalid("display")
    dp = set()
    for d in disp:
        exact_keys(d, ["path"], ["caption"])
        p = d["path"]
        if not isinstance(p, str) or not p.startswith("files/") or p not in files or p in dp:
            raise Invalid("display path")
        dp.add(p)
        if "caption" in d:
            string(d["caption"], False, 2000)
    cits = o.get("citations", [])
    if not isinstance(cits, list) or len(cits) > 1000:
        raise Invalid("citations")
    for c in cits:
        exact_keys(c, ["target"], ["relations", "reason"])
        t = c["target"]
        if not isinstance(t, str):
            raise Invalid("target")
        if t.startswith("sci:"):
            if not re.fullmatch(r"sci:sha256:[0-9a-f]{64}", t):
                raise Invalid("sci target")
        elif t.startswith("doi:"):
            if not re.fullmatch(r"doi:10\.[0-9]{4,}(\.[0-9]+)*/\S+", t):
                raise Invalid("doi target")
        elif not re.fullmatch(r"https?://[^\s/?#]+[^\s#]*(#[^\s#]*)?", t):
            raise Invalid("uri target")
        rel = c.get("relations", [])
        if not isinstance(rel, list) or len(set(rel)) != len(rel) or not set(rel) <= {
                "uses", "reviews", "supports", "challenges", "supersedes"}:
            raise Invalid("relations")
        if "reason" in c:
            if not isinstance(c["reason"], str):
                raise Invalid("reason")
            check_text(c["reason"], True, True, 1000)
    return o


# ------------------------------------------------------------ ZIP (section 2.2)

def build_zip(entries) -> bytes:
    out, central = bytearray(), bytearray()
    for name, data in entries:
        nb = name.encode()
        crc = zlib.crc32(data) & 0xFFFFFFFF
        off = len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 10, 0, 0, 0, 0x21, crc, len(data),
                           len(data), len(nb), 0) + nb + data
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 0x0A, 10, 0, 0, 0, 0x21, crc,
                               len(data), len(data), len(nb), 0, 0, 0, 0, 0, off) + nb
    cd = len(out)
    out += central
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(central),
                       cd, 0)
    return bytes(out)


def read_zip(z: bytes):
    """Walk local headers from byte 0; return the manifest, signature, and entry data."""
    pos, entries = 0, []
    while pos + 4 <= len(z) and struct.unpack_from("<I", z, pos)[0] == 0x04034B50:
        if pos + 30 > len(z):
            raise Invalid("truncated header")
        (_, _, flags, method, _, _, _, csize, usize, nl, xl) = struct.unpack_from(
            "<IHHHHHIIIHH", z, pos)
        name = z[pos + 30:pos + 30 + nl]
        start = pos + 30 + nl + xl
        if start + csize > len(z):
            raise Invalid("truncated data")
        entries.append((name, z[start:start + csize]))
        pos = start + csize
    if len(entries) < 2 or entries[0][0] != b"manifest.json" or entries[1][0] != b"manifest.json.sig":
        raise Invalid("first entries")
    return entries


def check_zip(z: bytes):
    entries = read_zip(z)
    mb, sig = entries[0][1], entries[1][1]
    if len(mb) > 16 * 1024 * 1024:
        raise Invalid("manifest too large")
    m = check_manifest(mb, sig)
    data = {}
    for name, d in entries[2:]:
        try:
            n = name.decode("ascii")
        except UnicodeDecodeError:
            raise Invalid("name")
        if n in data:
            raise Invalid("duplicate entry")
        data[n] = d
    if set(data) != set(m["files"]):
        raise Invalid("entry set differs from manifest")
    expected = build_zip([("manifest.json", mb), ("manifest.json.sig", sig)] +
                         [(p, data[p]) for p in sorted(m["files"], key=lambda s: s.encode())])
    if expected != z:
        raise Invalid("not the canonical ZIP")
    return mb, sig, m, data


def complete_checks(mb, sig, m, data):
    for p, e in m["files"].items():
        if len(data[p]) != e["bytes"] or sha(data[p]) != e["sha256"]:
            raise Invalid(f"file check {p}")
    sci = check_sci(data["sci.json"], m["files"])
    text_file(data["README.md"], nonblank=True)
    if "LICENSE" in data:
        text_file(data["LICENSE"])
    return sci


def zip_size(mb, m):
    return 22 + (len(mb) + 76 + 26) + (64 + 76 + 34) + sum(
        e["bytes"] + 76 + 2 * len(p) for p, e in m["files"].items())


def is_baseline(mb, m, sci, data):
    lic = m["files"].get("LICENSE")
    return (lic is not None and lic["sha256"] in BASELINE_LICENSES
            and zip_size(mb, m) <= 67108864 and len(m["files"]) <= 1000
            and len(sci.get("display", [])) <= 3 and len(mb) <= 262144
            and m["files"]["sci.json"]["bytes"] <= 65536
            and m["files"]["README.md"]["bytes"] <= 1048576)


# ------------------------------------------------------------ claims, revocations

def rfc3339(s):
    if not isinstance(s, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})", s):
        raise Invalid("expires format")
    return datetime.fromisoformat(s.replace("z", "Z").replace("Z", "+00:00").replace("t", "T"))


def check_domain(h):
    if not isinstance(h,str) or not h.isascii() or h!=h.lower() or h.endswith('.'):
        raise Invalid("domain spelling")
    try: ipaddress.ip_address(h)
    except ValueError: pass
    else: raise Invalid("IP address")
    try:
        if idna.encode(h,uts46=False).decode()!=h: raise Invalid("domain")
    except idna.IDNAError as exc: raise Invalid(str(exc))


def check_record(raw,sig,kind,ctx):
    if ctx.get('transport')=='unavailable': return {'outcome':'incomplete'}
    obj=strict_json(raw)
    check_schema(obj,'sci-key-claim/0.1' if kind=='claim' else 'sci-key-revocation/0.1')
    exact_keys(obj,['schema','public_key','account','expires'] if kind=='claim' else ['schema','public_key'],
               ['note'] if kind=='claim' else [])
    pub=jwk_key(obj['public_key'])
    verify_sig(pub,sig,raw)
    if obj['public_key']['x']!=ctx['expected_public_key_x']:raise Invalid('key mismatch')
    if kind=='claim':
        account=obj['account'];exact_keys(account,['type','id'])
        if not all(isinstance(v,str) for v in account.values()):raise Invalid('account strings')
        if account!=ctx['requested_account']:raise Invalid('account mismatch')
        if account['type']=='domain':check_domain(account['id'])
        elif account['type']=='github':
            if account['id']!=account['id'].lower():raise Invalid('GitHub spelling')
        elif account['type']!='huggingface':raise Invalid('account type')
        if 'note' in obj:check_text(obj['note'],True,True,2500)
        if rfc3339(ctx['now'])>=rfc3339(obj['expires']):raise Invalid('expired')
        if ctx['known_revocation'] or not ctx['retrieved_from_account_location']:raise Invalid('association')
    return {'outcome':'valid'}


def check_publication(artifacts,ctx):
    size=sum(len(b) for b in artifacts.values())
    if ctx.get('transport')=='unavailable' or ctx.get('verification_budget_bytes',size)<size:
        return {'outcome':'incomplete','baseline':None}
    if 'bundle.zip' in artifacts:
        mb,sig,m,data=check_zip(artifacts['bundle.zip'])
    else:
        mb,sig=artifacts['manifest.json'],artifacts['manifest.json.sig']
        m=check_manifest(mb,sig)
        data={p:b for p,b in artifacts.items() if p not in ('manifest.json','manifest.json.sig','manifest.json.sig.tsr')}
    if 'publication_id' in ctx and ctx['publication_id']!='sha256:'+sha(mb):raise Invalid('expected ID')
    if ctx.get('scope')=='manifest':
        n=zip_size(mb,m)
        return {'outcome':'valid','baseline':None,'zip_bytes':n,'within_baseline_zip_limit':n<=67108864}
    if set(data)!=set(m['files']):raise Invalid('file inventory')
    sci=complete_checks(mb,sig,m,data)
    packed=build_zip([('manifest.json',mb),('manifest.json.sig',sig)]+[(p,data[p]) for p in sorted(m['files'])])
    return {'outcome':'valid','baseline':bool(is_baseline(mb,m,sci,data)),
            'zip_bytes':len(packed),'zip_sha256':sha(packed)}


def check_case(case,root):
    artifacts={name:(root/path).read_bytes() for name,path in case['artifacts'].items()}
    try:
        if case['kind']=='publication':return check_publication(artifacts,case.get('context',{}))
        return check_record(artifacts['record.json'],artifacts['record.json.sig'],case['kind'],case['context'])
    except Unsupported:return {'outcome':'unsupported','baseline':None} if case['kind']=='publication' else {'outcome':'unsupported'}
    except (Invalid,ValueError,KeyError,TypeError,struct.error):
        return {'outcome':'invalid','baseline':None} if case['kind']=='publication' else {'outcome':'invalid'}


def main():
    root=ROOT/'test-vectors/0.1'
    cases=json.loads((root/'catalog.json').read_text())['cases']
    selected=[c for c in cases if c['kind'] in ('publication','claim','revocation')]
    failed=[]
    for c in selected:
        got=check_case(c,root)
        if any(got.get(k)!=v for k,v in c['expected'].items()):failed.append((c['id'],c['expected'],got))
    for row in failed:print(*row,sep='\n')
    if failed:raise SystemExit(f'{len(failed)} independent checks failed')
    print(f'Independent checker agrees on all {len(selected)} publication/claim/revocation cases.')


if __name__=='__main__':main()
