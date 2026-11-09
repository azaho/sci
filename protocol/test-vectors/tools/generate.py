#!/usr/bin/env python3
"""Deterministic SCI 0.1 fixture builder. Never use these public test keys in production."""
import argparse
import base64
import copy
import hashlib
import json
import struct
import sys
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[2]
SPEC_COMMIT = '0164db28435d7ca461c041039d2ed2fafcfa026f'
SPEC_SHA256 = '3048ed37bc77cf0a000781a6727eefe09fa383d8e7305e3277bc9e8e19fecdb1'
SEED = hashlib.sha256(b'SCI conformance fixtures: PUBLIC TEST KEY, NOT FOR PRODUCTION').digest()
KEY = Ed25519PrivateKey.from_private_bytes(SEED)
PUBLIC = KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
JWK = {'kty': 'OKP', 'crv': 'Ed25519', 'x': base64.urlsafe_b64encode(PUBLIC).decode().rstrip('=')}
META = {'title': 'Deterministic SCI test publication', 'abstract': 'Synthetic conformance fixture; no research claim.'}
LICENSE = (ROOT / 'sci-baseline-LICENSE.txt').read_bytes()
CASES = []
OUT = None


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def emit(name, kind, section, reason, artifacts, expected, context=None):
    folder = OUT / kind / name
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for filename, data in artifacts.items():
        (folder / filename).parent.mkdir(parents=True, exist_ok=True)
        (folder / filename).write_bytes(data)
        paths[filename] = str((folder / filename).relative_to(OUT))
    case = {'id': name, 'kind': kind, 'sections': section.split(','), 'description': reason,
            'artifacts': paths, 'expected': expected}
    if context is not None:
        case['context'] = context
    CASES.append(case)
    return case


def zip_bytes(entries):
    """Write exactly the specified SCI ZIP records, without filesystem extraction."""
    import zlib
    local, central = bytearray(), bytearray()
    for name, data in entries:
        n = name.encode('utf-8')
        crc, size, offset = zlib.crc32(data), len(data), len(local)
        local += struct.pack('<IHHHHHIIIHH', 0x04034b50, 10, 0, 0, 0, 33, crc, size, size, len(n), 0)
        local += n + data
        central += struct.pack('<IHHHHHHIIIHHHHHII', 0x02014b50, 10, 10, 0, 0, 0, 33,
                               crc, size, size, len(n), 0, 0, 0, 0, 0, offset)
        central += n
    return bytes(local + central + struct.pack('<IHHHHIIH', 0x06054b50, 0, 0, len(entries), len(entries),
                                               len(central), len(local), 0))


def bundle(files=None, metadata=None, readme=b'R\n', license_bytes=LICENSE,
           manifest_change=None, manifest_bytes=None, signature_change=None, entries_change=None):
    f = {'README.md': readme, 'sci.json': encode(META if metadata is None else metadata)}
    if license_bytes is not None:
        f['LICENSE'] = license_bytes
    f.update(files or {})
    m = {'schema': 'sci-publication/0.1', 'public_key': copy.deepcopy(JWK),
         'files': {p: {'sha256': sha(b), 'bytes': len(b)} for p, b in sorted(f.items())}}
    if manifest_change:
        manifest_change(m)
    raw = encode(m)
    if manifest_bytes:
        raw = manifest_bytes(raw)
    sig = KEY.sign(raw)
    if signature_change:
        sig = signature_change(sig)
    entries = [('manifest.json', raw), ('manifest.json.sig', sig)] + sorted(f.items())
    if entries_change:
        entries = entries_change(entries)
    return zip_bytes(entries), raw, sig


def pub(name, section, reason, outcome='valid', baseline=True, mutate=None, context=None, **kwargs):
    z, m, sig = bundle(**kwargs)
    if mutate:
        z = mutate(z)
    expected = {'outcome': outcome, 'baseline': baseline if outcome == 'valid' else None}
    ctx = {'publication_id': 'sha256:' + sha(m)}
    ctx.update(context or {})
    return emit(name, 'publication', section, reason, {'bundle.zip': z}, expected, ctx)


def patch_zip(z, offsets):
    b = bytearray(z)
    central = struct.unpack_from('<I', b, len(b) - 6)[0]
    for where, offset, fmt, value in offsets:
        base = {'local': 0, 'central': central, 'end': len(b) - 22}[where]
        struct.pack_into(fmt, b, base + offset, value)
    return bytes(b)


def generate_publications():
    pub('minimal-baseline', '2.1,6.4', 'Small complete signed baseline publication with the approved license.')
    pub('no-license', '2.1,3', 'LICENSE is optional for format validity but required for baseline.', license_bytes=None, baseline=False)
    pub('custom-license', '2.1', 'Other valid license text does not match an approved baseline license.', license_bytes=b'Custom test terms.\n', baseline=False)
    pub('license-extra-newline', '2.1', 'A one-byte license change is valid text but not the approved exact license.', license_bytes=LICENSE+b'\n', baseline=False)
    pub('empty-readme', '2,4.2', 'README.md must be nonblank.', outcome='invalid', readme=b'')
    pub('empty-supporting-file', '3', 'Supporting files may contain zero bytes.', files={'files/empty.bin': b''})
    pub('opaque-supporting-bytes', '4.2,6', 'Text restrictions do not apply to arbitrary supporting bytes; do not execute content.', files={'files/opaque.bin': b'\x00\xff\xfe\x01'})
    pub('nested-archive-opaque', '6', 'A nested archive is hashed as opaque bytes, never recursively expanded.', files={'files/archive.zip': zip_bytes([('outside.txt', b'opaque')])})
    pub('json-whitespace-identity', '2,7', 'Whitespace changes the signed manifest ID; verify the original bytes.', manifest_bytes=lambda b: b' \n'+b+b'\n')
    pub('manifest-reordered', '3,7', 'Manifest file-member ordering is SHOULD, not MUST; ZIP entries remain sorted.', manifest_change=lambda m: m.update(files=dict(reversed(list(m['files'].items())))))
    pub('renamed-zip', '2.2', 'The fixture is named bundle.zip rather than its digest; readers still verify it.')
    pub('unknown-schema', '5.1,6', 'An unknown schema is unsupported, not a bad signature.', outcome='unsupported', manifest_change=lambda m: m.update(schema='sci-publication/99.0'))
    pub('wrong-expected-id', '6.1', 'A valid bundle does not match the ID requested by the reader.', outcome='invalid', context={'publication_id':'sha256:'+'0'*64})
    pub('unavailable-input', '6', 'A transport failure is incomplete verification, not invalid publication bytes.', outcome='incomplete', context={'transport':'unavailable'})
    pub('local-budget-exceeded', '6', 'A simulated local resource limit is incomplete, not proof of invalidity.', outcome='incomplete', context={'verification_budget_bytes':1})
    for name, change, section, reason in [
        ('manifest-extra-field', lambda m:m.update(extra=True), '3', 'Manifest fields are closed.'),
        ('manifest-missing-readme', lambda m:m['files'].pop('README.md'), '3', 'The manifest must list README.md.'),
        ('manifest-missing-metadata', lambda m:m['files'].pop('sci.json'), '3', 'The manifest must list sci.json.'),
        ('manifest-self-entry', lambda m:m['files'].update({'manifest.json':{'sha256':'0'*64,'bytes':0}}), '3', 'The manifest cannot list itself.'),
        ('manifest-uppercase-hash', lambda m:m['files']['README.md'].update(sha256=m['files']['README.md']['sha256'].upper()), '3', 'File hashes require lowercase hexadecimal.'),
        ('manifest-wrong-hash', lambda m:m['files']['README.md'].update(sha256='0'*64), '6.2', 'The signed file hash differs from actual content.'),
        ('manifest-wrong-size', lambda m:m['files']['README.md'].update(bytes=3), '6.2', 'The signed byte count differs from actual content.'),
        ('manifest-negative-size', lambda m:m['files']['README.md'].update(bytes=-1), '3', 'Byte counts cannot be negative.'),
        ('manifest-unsafe-integer', lambda m:m['files']['README.md'].update(bytes=9007199254740992), '3', 'Byte counts must not exceed 2^53-1; reject before allocation.'),
        ('manifest-boolean-size', lambda m:m['files']['README.md'].update(bytes=True), '3', 'A boolean is not an integer byte count.'),
        ('jwk-extra-field', lambda m:m['public_key'].update(alg='EdDSA'), '5.1', 'Public JWK fields are closed.'),
        ('jwk-wrong-curve', lambda m:m['public_key'].update(crv='X25519'), '5.1', 'The curve must be Ed25519.'),
        ('jwk-base64-padding', lambda m:m['public_key'].update(x=JWK['x']+'='), '5.1', 'Padded base64url is not canonical.'),
        ('jwk-nonzero-pad-bits', lambda m:m['public_key'].update(x=JWK['x'][:-1]+'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'[('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'.index(JWK['x'][-1]) | 1)]), '5.1', 'Nonzero unused base64url bits are not canonical.'),
    ]:
        pub(name, section, reason, outcome='invalid', manifest_change=change)
    for name, transform, reason in [
        ('manifest-bom', lambda b:b'\xef\xbb\xbf'+b, 'JSON forbids UTF-8 BOM.'),
        ('manifest-duplicate-key', lambda b:b.replace(b'{', b'{"schema":"sci-publication/0.1",',1), 'Duplicate keys are prohibited after decoding.'),
        ('manifest-escaped-duplicate', lambda b:b.replace(b'{', b'{"schem\\u0061":"sci-publication/0.1",',1), 'Escaped duplicate keys count as duplicates.'),
        ('manifest-exponent-size', lambda b:b.replace(b'"bytes":2}',b'"bytes":2e0}',1), 'Byte count lexemes must use digits only.'),
        ('manifest-fraction-size', lambda b:b.replace(b'"bytes":2}',b'"bytes":2.0}',1), 'A mathematically integral decimal is not the required integer spelling.'),
        ('manifest-leading-zero-size', lambda b:b.replace(b'"bytes":2}',b'"bytes":02}',1), 'Leading zeros are invalid JSON and invalid byte-count spelling.'),
    ]:
        pub(name, '3,7', reason, outcome='invalid', manifest_bytes=transform)
    pub('signature-flipped', '5.1', 'A modified signature fails Ed25519 verification.', outcome='invalid', signature_change=lambda s:bytes([s[0]^1])+s[1:])
    pub('signature-short', '5.1', 'The raw signature must contain exactly 64 bytes.', outcome='invalid', signature_change=lambda s:s[:-1])
    pub('signature-long', '5.1', 'A 65-byte signature is prohibited.', outcome='invalid', signature_change=lambda s:s+b'\0')
    order=2**252+27742317777372353535851937790883648493
    pub('signature-noncanonical-scalar', '5.1', 'S+L is noncanonical even when the group equation is unchanged.', outcome='invalid', signature_change=lambda s:s[:32]+(int.from_bytes(s[32:],'little')+order).to_bytes(32,'little'))
    identity=b'\x01'+b'\0'*31
    pub('identity-public-key', '5.1', 'Reject the identity public key, even for a signature satisfying the group equation.', outcome='invalid', manifest_change=lambda m:m['public_key'].update(x=base64.urlsafe_b64encode(identity).decode().rstrip('=')), signature_change=lambda s:bytes.fromhex('58'+'66'*31)+(1).to_bytes(32,'little'))
    pub('identity-signature-point', '5.1', 'The signature R point must be a nonidentity prime-order point.', outcome='invalid', signature_change=lambda s:identity+s[32:])
    pub('signature-stale-after-whitespace', '5.1,7', 'Do not parse and reserialize JSON before checking the signature.', outcome='invalid', entries_change=lambda es:[(p,b+b' ' if p=='manifest.json' else b) for p,b in es], context={'publication_id':'sha256:'+sha(bundle()[1]+b' ')})
    pub('small-order-public-key', '5.1', 'The all-zero encoded point has small order and is not an admissible public key.', outcome='invalid', manifest_change=lambda m:m['public_key'].update(x='A'*43))
    # Add the order-two point (0,-1) to the legitimate public key. For an even
    # challenge, a correctly adjusted signature satisfies the ordinary equation;
    # the SCI prime-subgroup requirement must still reject the key.
    prime=2**255-19
    encoded=int.from_bytes(PUBLIC,'little')
    mixed=((-(encoded&((1<<255)-1)))%prime | ((1-(encoded>>255))<<255)).to_bytes(32,'little')
    mixed_x=base64.urlsafe_b64encode(mixed).decode().rstrip('=')
    change=lambda m:m['public_key'].update(x=mixed_x)
    raw=bundle(manifest_change=change)[1]
    scalar=int.from_bytes(hashlib.sha512(SEED).digest()[:32],'little')
    scalar=(scalar & ((1<<254)-8)) | (1<<254)
    while True:
        original=KEY.sign(raw)
        h=int.from_bytes(hashlib.sha512(original[:32]+PUBLIC+raw).digest(),'little')%order
        hm=int.from_bytes(hashlib.sha512(original[:32]+mixed+raw).digest(),'little')%order
        if hm%2==0:break
        raw+=b' '
    mixed_sig=original[:32]+((int.from_bytes(original[32:],'little')+(hm-h)*scalar)%order).to_bytes(32,'little')
    pub('mixed-order-public-key', '5.1', 'The signature satisfies the uncofactored equation, but A is outside the prime-order subgroup.', outcome='invalid', manifest_change=change, manifest_bytes=lambda b:raw, signature_change=lambda sig:mixed_sig)
    for name, path in [('path-absolute','/tmp/a'),('path-parent','files/../a'),('path-dot','files/./a'),('path-empty-component','files//a'),('path-backslash','files/a\\b'),('path-space','files/a b'),('path-nonascii','files/café'),('path-trailing-dot','files/a.'),('path-device','files/CON'),('path-device-extension','files/cOm1.txt'),('path-device-directory','files/LPT9/a'),('path-root-other','other.txt'),('path-256','files/'+('a'*250))]:
        pub(name, '3.1', 'Reject prohibited full path '+repr(path)+'.', outcome='invalid', files={path:b'x'})
    pub('path-255', '3.1', 'Full relative ASCII path is exactly 255 bytes.', files={'files/'+'a'*249:b'x'})
    for name, files in [
        ('path-case-collision',{'files/a':b'a','files/A':b'b'}),
        ('path-file-directory-collision',{'files/a':b'a','files/a/b':b'b'}),
        ('path-directory-case-collision',{'files/Data/a':b'a','files/data/b':b'b'})]:
        pub(name,'3.1','Case-insensitive file/directory paths conflict.',outcome='invalid',files=files)
    for name,field,value in [
        ('title-201','title','a'*201),('title-blank','title','   '),('title-tab','title','a\tb'),
        ('abstract-2501','abstract','a'*2501),('abstract-blank','abstract',' \r\n\t'),
        ('keywords-21','keywords',['a']*21),('keyword-65','keywords',['a'*65]),
        ('categories-4','categories',[{'scheme':'arxiv','code':str(i)} for i in range(4)]),
        ('categories-duplicate','categories',[{'scheme':'arxiv','code':'cs.AI'}]*2),
        ('category-bad-scheme','categories',[{'scheme':'Arxiv','code':'cs.AI'}]),
        ('category-code-129','categories',[{'scheme':'arxiv','code':'a'*129}]),
        ('metadata-extra-field','extra','x'),('title-wrong-type','title',1),
    ]:
        pub(name,'4','Metadata field violates its schema or limit.',outcome='invalid',metadata={**META,field:value})
    pub('metadata-boundary-fields','4','Title, abstract, keyword, and category lengths exactly at their limits.',metadata={'title':'a'*200,'abstract':'a'*2500,'keywords':['a'*64]*20,'categories':[{'scheme':'a'*64,'code':'a'*128}]})
    pub('title-200-nonbmp','4','Code points, not UTF-16 code units or bytes, determine the title limit.',metadata={**META,'title':'😀'*200})
    pub('abstract-controls','4.2','TAB, LF, and CR are allowed in abstract.',metadata={**META,'abstract':'a\tb\nc\rd'})
    for name,value in [('nbsp','x\u00a0y'),('bom-character','x\ufeffy'),('zero-width-space','x\u200by'),('bidi-control','x\u202ey'),('variation-selector','x\ufe0fy'),('combining-grapheme-joiner','x\u034fy'),('private-use','x\ue000y'),('nul','x\0y'),('invalid-zwj','a\u200db'),('invalid-zwnj','a\u200cb')]:
        pub('text-'+name,'4.2','RFC 9839 Assignables permits this text except for NUL.',outcome='invalid' if name=='nul' else 'valid',readme=value.encode())
    pub('readme-invalid-utf8','2,4.2','Invalid UTF-8 must be rejected.',outcome='invalid',readme=b'a\xffb')
    pub('license-nbsp','4.2','NBSP is assignable; this license is valid but not baseline.',baseline=False,license_bytes=b'x\xc2\xa0y')
    for name,value in [('zwj-after-virama','\u0915\u094d\u200d\u0915'),('zwnj-after-virama','\u0915\u094d\u200c\u0915'),('zwnj-arabic-context','\u0628\u200c\u0628'),('combining-mark','e\u0301'),('ordinary-symbols','数学 ∑ 😀')]:
        pub('text-'+name,'4.2','Permitted text; joiners no longer require contextual checks.',readme=value.encode())
    for name,raw in [('metadata-duplicate-key',b'{"title":"A","title":"B","abstract":"C"}'),('metadata-escaped-duplicate',b'{"title":"A","ti\\u0074le":"B","abstract":"C"}'),('metadata-unpaired-surrogate',b'{"title":"\\ud800","abstract":"C"}'),('metadata-bom',b'\xef\xbb\xbf'+encode(META)),('metadata-duplicate-nested-key',b'{"title":"A","abstract":"B","categories":[{"scheme":"arxiv","code":"a","code":"b"}]}')]:
        pub(name,'7','Reject invalid JSON encoding or duplicate decoded keys at any depth.',outcome='invalid',files={'sci.json':raw})
    pub('metadata-surrogate-pair','7','A valid surrogate pair decodes to one allowed non-BMP code point.',files={'sci.json':b'{"title":"\\ud83d\\ude00","abstract":"C"}'})
    supporting={f'files/{i}.txt':b'x' for i in range(4)}
    for n in (3,4):
        pub(f'display-{n}','2.1,4.3','Non-PNG display items remain valid but are not baseline.',baseline=False,files=supporting,metadata={**META,'display':[{'path':f'files/{i}.txt'} for i in range(n)]})
    for name,items in [('display-duplicate',[{'path':'files/0.txt'}]*2),('display-missing',[{'path':'files/missing'}]),('display-root',[{'path':'README.md'}]),('display-case',[{'path':'files/0.TXT'}]),('display-caption-2001',[{'path':'files/0.txt','caption':'a'*2001}]),('display-extra-field',[{'path':'files/0.txt','extra':True}])]:
        pub(name,'4.3','Display items require unique exact supporting paths and valid captions.',outcome='invalid',files=supporting,metadata={**META,'display':items})
    pub('display-caption-2000','2.1,4.3','Caption length exactly 2,000 code points; the text display file is non-baseline.',baseline=False,files=supporting,metadata={**META,'display':[{'path':'files/0.txt','caption':'a'*2000}]})
    targets=['sci:sha256:'+'a'*64,'doi:10.1000/182','https://example.org/paper?q=1#results','http://example.org/data']
    pub('citation-targets','4.4','SCI, DOI, HTTP and HTTPS targets; multiple relation claims.',metadata={**META,'citations':[{'target':t,'relations':['uses','supports'],'reason':'a\tb\nc\rd'} for t in targets]})
    for name,citations in [('citation-invalid-sci-hash',[{'target':'sci:sha256:'+'A'*64}]),('citation-invalid-scheme',[{'target':'ftp://example.org/a'}]),('citation-duplicate-relations',[{'target':targets[0],'relations':['uses','uses']}]),('citation-unknown-relation',[{'target':targets[0],'relations':['cites']}]),('citation-reason-1001',[{'target':targets[0],'reason':'a'*1001}]),('citation-extra-field',[{'target':targets[0],'extra':True}]),('citations-1001',[{'target':targets[0]}]*1001)]:
        pub(name,'4.4','Citation schema or count violation.',outcome='invalid',metadata={**META,'citations':citations})
    pub('citations-1000','4,2.1','1,000 SCI citations are format-valid but exceed the baseline metadata byte limit.',baseline=False,metadata={**META,'citations':[{'target':targets[0]}]*1000})
    pub('citation-reason-1000','4.4','Citation reason exactly 1,000 code points.',metadata={**META,'citations':[{'target':targets[0],'reason':'a'*1000}]})
    for n in (1000,1001):
        pub(f'listed-files-{n}','2.1','Listed count includes metadata, README and LICENSE, excludes manifest/signature.',baseline=n==1000,files={f'files/f{i:04}.txt':b'' for i in range(n-3)})
    for n in (65536,65537):
        pub(f'metadata-bytes-{n}','2.1','Exact serialized metadata size includes JSON whitespace.',baseline=n==65536,files={'sci.json':encode(META).ljust(n,b' ')})
    for n in (262144,262145):
        pub(f'manifest-bytes-{n}','2.1','Exact manifest size includes signed JSON whitespace.',baseline=n==262144,manifest_bytes=lambda b,n=n:b.ljust(n,b' '))
    for n in (1048576,1048577):
        pub(f'readme-bytes-{n}','2.1','README byte limit, not code-point limit; non-ASCII text at the boundary.',baseline=n==1048576,readme=('界'*(n//3)).encode()+b'a'*(n%3))
    for n in (67108864,67108865):
        payload=n-23000
        while True:
            actual=len(bundle(files={'files/payload.bin':b'\0'*payload})[0])
            if actual==n:
                break
            payload+=n-actual
        pub(f'zip-bytes-{n}','2.1,2.2','Total includes ZIP headers, manifest, signature, all file contents and final record.',baseline=n==67108864,files={'files/payload.bin':b'\0'*payload})
    # Mutations keep the signed manifest intact. They deliberately violate the ZIP envelope.
    mutations=[
        ('zip-method', [('local',8,'H',8),('central',10,'H',8)]),
        ('zip-flags',[('local',6,'H',8),('central',8,'H',8)]),
        ('zip-version-needed',[('local',4,'H',20),('central',6,'H',20)]),
        ('zip-version-made-by',[('central',4,'H',0x0314)]),
        ('zip-date',[('local',12,'H',34),('central',14,'H',34)]),
        ('zip-time',[('local',10,'H',1),('central',12,'H',1)]),
        ('zip-external-attributes',[('central',38,'I',0o120777<<16)]),
        ('zip-internal-attributes',[('central',36,'H',1)]),
        ('zip-local-central-mismatch',[('central',12,'H',1)]),
        ('zip-central-offset',[('central',42,'I',1)]),
        ('zip-wrong-crc',[('local',14,'I',0),('central',16,'I',0)]),
        ('zip-disk-number',[('end',4,'H',1)]),
        ('zip-entry-count',[('end',8,'H',1),('end',10,'H',1)]),
        ('zip64-sentinel',[('end',16,'I',0xffffffff)]),
    ]
    for name,patches in mutations:
        pub(name,'2.2','Noncanonical or inconsistent ZIP header field.',outcome='invalid',mutate=lambda z,p=patches:patch_zip(z,p))
    pub('zip-prefix','2.2','Every byte must belong to the canonical ZIP layout starting at zero.',outcome='invalid',mutate=lambda z:b'junk'+z)
    pub('zip-trailing-bytes','2.2','The end record must end at the final byte.',outcome='invalid',mutate=lambda z:z+b'junk')
    pub('zip-truncated','2.2','A completed transport containing a truncated ZIP is malformed.',outcome='invalid',mutate=lambda z:z[:-1])
    pub('zip-extra-entry','2.2,3','Unlisted ZIP entries are forbidden.',outcome='invalid',entries_change=lambda e:e+[('files/extra',b'x')])
    pub('zip-missing-entry','2.2,3','Every signed listed file must be present.',outcome='invalid',entries_change=lambda e:[x for x in e if x[0]!='README.md'])
    pub('zip-duplicate-entry','2.2','Duplicate ZIP entries are forbidden.',outcome='invalid',entries_change=lambda e:e+[e[-1]])
    pub('zip-directory-entry','2.2','Explicit directory records are forbidden.',outcome='invalid',entries_change=lambda e:e+[('files/',b'')])
    pub('zip-first-entry-order','2.2','Manifest must be first and signature second.',outcome='invalid',entries_change=lambda e:[e[1],e[0]]+e[2:])
    pub('zip-listed-entry-order','2.2','Listed entries must be sorted by full ASCII path.',outcome='invalid',entries_change=lambda e:e[:2]+list(reversed(e[2:])))
    pub('zip-timestamp-entry','2.2','Timestamp proofs stay outside the ZIP.',outcome='invalid',entries_change=lambda e:e+[('manifest.json.sig.tsr',b'\x30\x00')])
    pub('zip-file-content-tamper','6.2','CRC is updated but signed file digest no longer matches.',outcome='invalid',entries_change=lambda e:[(p,b'X\n' if p=='README.md' else b) for p,b in e])


def generate_records():
    rev={'schema':'sci-key-revocation/0.1','public_key':copy.deepcopy(JWK)}
    def record(name,obj,kind='revocation',outcome='valid',context=None,raw=None,sig_transform=None,reason='Valid signed record.',section='5.3,6.7'):
        b=encode(obj) if raw is None else raw
        sig=KEY.sign(b)
        if sig_transform:
            sig=sig_transform(sig)
        return emit(name,kind,section,reason,{'record.json':b,'record.json.sig':sig},
                    {'outcome':outcome},context or {'expected_public_key_x':JWK['x']})
    record('revocation-valid',rev)
    record('revocation-extra-field',{**rev,'expires':'2026-12-01T00:00:00Z'},outcome='invalid',reason='Revocation fields are closed.')
    record('revocation-bad-signature',rev,outcome='invalid',sig_transform=lambda s:s[:-1]+bytes([s[-1]^1]),reason='Invalid revocation signature.')
    record('revocation-wrong-key',rev,outcome='invalid',context={'expected_public_key_x':'A'*43},reason='Lookup key must match the signed record.')
    record('revocation-unknown-schema',{**rev,'schema':'sci-key-revocation/99.0'},outcome='unsupported',reason='Unknown record schema is unsupported.')
    record('revocation-cross-type',{'schema':'sci-publication/0.1','public_key':JWK},outcome='invalid',reason='A supported publication schema is the wrong record type for revocation.')
    record('revocation-duplicate-key',rev,raw=encode(rev).replace(b'{',b'{"schema":"sci-key-revocation/0.1",',1),outcome='invalid',reason='Duplicate decoded JSON fields are forbidden.')
    for method,account in [('domain','example.org'),('github','example-user'),('huggingface','ExampleOrg')]:
        claim={'schema':'sci-key-claim/0.1','public_key':JWK,'account':{'type':method,'id':account},'expires':'2026-12-01T00:00:00Z','note':'Synthetic test claim.'}
        ctx={'expected_public_key_x':JWK['x'],'requested_account':claim['account'],'now':'2026-11-07T12:00:00Z','known_revocation':False,'retrieved_from_account_location':True}
        record('claim-'+method,claim,'claim',context=ctx,section='5.2,6.6')
        if method=='domain':
            for name,change,extra in [
                ('expired',{'expires':'2026-11-07T11:59:59Z'},{}),
                ('expiry-equality',{'expires':'2026-11-07T12:00:00Z'},{}),
                ('wrong-account',{}, {'requested_account':{'type':'domain','id':'other.example'}}),
                ('revoked',{}, {'known_revocation':True}),
                ('wrong-key',{}, {'expected_public_key_x':'A'*43}),
                ('unhosted',{}, {'retrieved_from_account_location':False}),
                ('uppercase-domain',{'account':{'type':'domain','id':'Example.org'}},{}),
                ('domain-ip',{'account':{'type':'domain','id':'127.0.0.1'}},{}),
                ('domain-trailing-dot',{'account':{'type':'domain','id':'example.org.'}},{}),
            ]:
                c={**claim,**change}; cx={**ctx,**extra}
                if 'account' in change:
                    cx['requested_account']=change['account']
                record('claim-'+name,c,'claim',outcome='invalid',context=cx,reason='Association must not be reported as verified.',section='5.2,6.6')
            for label, name, outcome in [
                ('name', 'Andrii Zahorodnii', 'valid'),
                ('name-unicode', '李明', 'valid'),
                ('name-boundary', '\U00010400' * 80, 'valid'),
                ('name-too-long', '\U00010400' * 81, 'invalid'),
                ('name-newline', 'First\nLast', 'invalid'),
                ('name-bidi', 'First\u202eLast', 'valid'),
                ('name-blank', ' ', 'invalid'),
            ]:
                record('claim-' + label, {**claim, 'name': name}, 'claim', outcome=outcome,
                       context=ctx, section='5.2.1,6.6', reason='Publisher display name validation.')
            record('claim-bad-signature',claim,'claim',outcome='invalid',context=ctx,sig_transform=lambda s:bytes([s[0]^1])+s[1:],section='5.2,6.6',reason='Invalid claim signature.')
            record('claim-missing-source',claim,'claim',outcome='incomplete',context={**ctx,'transport':'unavailable'},section='6',reason='Unavailable hosted evidence is not an invalid signed claim.')
    # Explicit temporal/service scenarios, consumed by adapters rather than cryptographic validators.
    for name,events,expect in [
        ('revocation-before-admission',['verify_revocation','attempt_admission'],{'new_admission':'reject','reason':'revoked_key'}),
        ('revocation-after-admission',['admit_publication','verify_revocation'],{'catalog_entry':'retained','key_status':'revoked'}),
        ('revocation-source-disappears',['verify_revocation','source_returns_404'],{'key_status':'revoked'}),
        ('timestamp-before-revocation',['verify_earlier_timestamp','verify_revocation','attempt_admission'],{'new_admission':'reject','reason':'revoked_key'}),
        ('timestamp-acquisition-fails',['admit_publication','timestamp_authority_unavailable'],{'catalog_entry':'retained'}),
        ('revocation-lookup-404',['lookup_revocation_404'],{'key_status':'unknown','must_not_infer':'never_revoked'}),
        ('timestamp-lookup-404',['lookup_timestamp_404'],{'timestamp_status':'unknown','must_not_infer':'no_timestamp_exists'}),
    ]:
        emit(name,'scenario','5.3,6.5,8.1', 'Stateful policy/absence semantics; execute events in order.', {'events.json':encode({'events':events})},expect,{'publication':'minimal-baseline','key':JWK['x']})


def generate_discovery():
    index={'schema':'sci-index/0.1','publication_lookup_url':'https://index.example/publications/{id}',
           'revocation_lookup_url':'https://index.example/keys/{x}/{file}',
           'timestamp_lookup_url':'https://index.example/timestamps/{id}'}
    archive={'schema':'sci-archive/0.1','bundle_url':'https://archive.example/{id2}/{id}.zip',
             'entry_url':'https://archive.example/{id}/{+path}','blob_url':'https://archive.example/blobs/{sha256}',
             'list_url':'https://archive.example/list.txt'}
    for name,obj,kind,outcome,reason in [
        ('index-valid',index,'index','valid','Required lookup routes including timestamps.'),
        ('index-extension',{**index,'custom_submit_url':'https://index.example/submit'},'index','valid','Additional descriptor fields are allowed.'),
        ('index-missing-timestamp',{k:v for k,v in index.items() if k!='timestamp_lookup_url'},'index','invalid','Timestamp lookup is required in the merged draft.'),
        ('index-relative-url',{**index,'publication_lookup_url':'/publications/{id}'},'index','invalid','Descriptor URLs must be absolute.'),
        ('index-missing-expression',{**index,'publication_lookup_url':'https://index.example/all'},'index','invalid','Required template expression is absent.'),
        ('index-unknown-schema',{**index,'schema':'sci-index/99.0'},'index','unsupported','Unknown schema must not be treated as invalid bytes.'),
        ('archive-valid',archive,'archive','valid','Complete set of retrieval routes.'),
        ('archive-entry-only',{'schema':'sci-archive/0.1','entry_url':archive['entry_url']},'archive','valid','Loose-file archives need not provide a bundle URL.'),
        ('archive-bundle-only',{'schema':'sci-archive/0.1','bundle_url':archive['bundle_url']},'archive','valid','ZIP archives need not provide entry or blob routes.'),
        ('archive-wrong-expression',{**archive,'entry_url':'https://archive.example/{unknown}'},'archive','invalid','Only listed expressions are permitted.'),
    ]:
        emit(name,'discovery','8',reason,{'descriptor.json':encode(obj)},{'outcome':outcome},{'descriptor_kind':kind})
    emit('index-duplicate-schema','discovery','7,8','Duplicate descriptor keys are prohibited.',{'descriptor.json':encode(index).replace(b'{',b'{"schema":"sci-index/0.1",',1)},{'outcome':'invalid'},{'descriptor_kind':'index'})
    digest=CASES[0]['context']['publication_id'].split(':')[1]
    variables={'id':digest,'id2':digest[:2],'path':'files/Data/a.txt','sha256':'b'*64,'x':JWK['x'],'file':'revocation.json.sig'}
    expanded={
        'bundle_url':f'https://archive.example/{digest[:2]}/{digest}.zip',
        'entry_url':f'https://archive.example/{digest}/files/Data/a.txt',
        'timestamp_lookup_url':f'https://index.example/timestamps/{digest}',
        'revocation_lookup_url':f'https://index.example/keys/{JWK["x"]}/revocation.json.sig',
        'blob_url':'https://archive.example/blobs/'+'b'*64,
    }
    emit('template-expansions','template','8','Expand full IDs without sha256: prefixes and preserve slash in {+path}.',{'templates.json':encode({**index,**archive}),'variables.json':encode(variables)},expanded)
    for name,status,body,expected in [
        ('publication-lookup-known',200,encode({'descriptors':['https://archive.example/.well-known/sci/archive.json']}),{'sources':['https://archive.example/.well-known/sci/archive.json']}),
        ('publication-lookup-empty',200,b'{"descriptors":[]}',{'sources':[]}),
        ('publication-lookup-404',404,b'',{'sources':[]}),
        ('timestamp-lookup-404-http',404,b'',{'proofs_retained':False,'timestamp_existence':'unknown'}),
    ]:
        emit(name,'http','8.1','Offline HTTP exchange; no live service or network needed.',{'response.bin':body},expected,{'status':status,'publication_id':digest})


def main():
    global OUT
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'test-vectors/0.1')
    args=parser.parse_args()
    OUT=args.output.resolve()
    OUT.mkdir(parents=True,exist_ok=True)
    spec=(ROOT/'README.md').read_bytes()
    if sha(spec)!=SPEC_SHA256:
        raise SystemExit('Spec differs from pinned revision; review expected outcomes and update the pin first.')
    CASES.clear()
    generate_publications()
    generate_records()
    generate_discovery()
    from supplemental import generate_supplemental
    generate_supplemental(OUT, emit)
    from updates import generate_updates
    generate_updates(pub, emit, bundle, KEY, JWK, encode)
    from display_vectors import generate_display_vectors
    generate_display_vectors(pub, emit, bundle)
    from timestamps import generate_timestamps
    generate_timestamps(OUT,emit,bundle,KEY,JWK)
    catalog={'schema':'sci-test-vectors/1','protocol':'sci-publication/0.1','spec_commit':SPEC_COMMIT,
             'spec_sha256':sha(spec),'test_public_key':JWK,'fixed_time':'2026-11-07T12:00:00Z','cases':CASES}
    (OUT/'catalog.json').write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n')
    inventory={str(p.relative_to(OUT)):{'bytes':p.stat().st_size,'sha256':sha(p.read_bytes())}
               for p in sorted(OUT.rglob('*')) if p.is_file() and p!=OUT/'inventory.json'}
    (OUT/'inventory.json').write_text(json.dumps(inventory,indent=2)+'\n')
    total=sum(v['bytes'] for v in inventory.values())
    print(f'{len(CASES)} cases; {len(inventory)} inventoried files; {total:,} bytes')

if __name__=='__main__':
    main()
