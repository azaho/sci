#!/usr/bin/env python3
"""Audit fixture integrity and stated results; not a general SCI implementation.

Text validation uses the fixed RFC 9839 Assignables ranges.
No archive is extracted or content executed. URI/PKI checks remain corpus-focused.
"""
import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import idna
import zlib
from urllib.parse import urlsplit
import zipfile
from asn1crypto import tsp
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding
from cryptography.x509.oid import ExtendedKeyUsageOID
from display_profile import baseline_png

ROOT=Path(__file__).resolve().parents[2]
NOW=datetime(2026,11,7,12,tzinfo=timezone.utc)
P=2**255-19
L=2**252+27742317777372353535851937790883648493
D=(-121665*pow(121666,-1,P))%P


class Invalid(ValueError):
    pass

class Unsupported(ValueError):
    pass


def require(condition,message):
    if not condition:
        raise Invalid(message)


class JsonInteger(int):
    def __new__(cls, token):
        value = super().__new__(cls, token)
        value.token = token
        return value


def check_schema(obj, expected):
    require(isinstance(obj, dict) and isinstance(obj.get('schema'), str), 'missing/non-string schema')
    known = {'sci-publication/0.1', 'sci-key-claim/0.1', 'sci-key-revocation/0.1',
             'sci-index/0.1', 'sci-archive/0.1'}
    if obj['schema'] != expected:
        if obj['schema'] in known: raise Invalid('wrong supported record type')
        raise Unsupported(obj['schema'])


def load_json(raw):
    require(not raw.startswith(b'\xef\xbb\xbf'),'JSON BOM')
    def pairs(values):
        d={}
        for k,v in values:
            require(k not in d,'duplicate decoded JSON key')
            d[k]=v
        return d
    def constant(s):
        raise Invalid('non-JSON constant '+s)
    value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=constant,parse_int=JsonInteger)
    def strings(v):
        if isinstance(v,str):
            require(not any(0xd800<=ord(c)<=0xdfff for c in v),'unpaired surrogate')
        elif isinstance(v,dict):
            for k,x in v.items(): strings(k); strings(x)
        elif isinstance(v,list):
            for x in v: strings(x)
    strings(value)
    return value


def keys(value,required,optional=()):
    require(isinstance(value,dict),'not an object')
    require(set(required)<=value.keys()<=set(required)|set(optional),'missing/unknown fields')


def add(a,b):
    x1,y1,z1,t1=a; x2,y2,z2,t2=b
    aa=(y1-x1)*(y2-x2)%P; bb=(y1+x1)*(y2+x2)%P
    cc=2*D*t1*t2%P; dd=2*z1*z2%P
    e=(bb-aa)%P; f=(dd-cc)%P; g=(dd+cc)%P; h=(bb+aa)%P
    return e*f%P,g*h%P,f*g%P,e*h%P


def multiply(a,n):
    r=(0,1,1,0)
    while n:
        if n&1:r=add(r,a)
        a=add(a,a);n>>=1
    return r


@lru_cache(maxsize=1024)
def strict_point(encoded):
    require(len(encoded)==32,'point length')
    n=int.from_bytes(encoded,'little'); y=n&((1<<255)-1); sign=n>>255
    require(y<P,'noncanonical point')
    x2=(y*y-1)*pow(D*y*y+1,-1,P)%P
    x=pow(x2,(P+3)//8,P)
    if (x*x-x2)%P:x=x*pow(2,(P-1)//4,P)%P
    require((x*x-x2)%P==0,'point not on curve')
    require(x!=0 or sign==0,'noncanonical zero sign')
    if x&1 != sign:x=P-x
    require(not (x==0 and y==1),'identity point')
    xx,yy,zz,_=multiply((x,y,1,x*y%P),L)
    require(xx==0 and (yy-zz)%P==0,'point outside prime-order subgroup')


def pubkey(jwk):
    keys(jwk,('kty','crv','x'))
    require(jwk['kty']=='OKP' and jwk['crv']=='Ed25519','wrong JWK type/curve')
    x=jwk['x'];require(isinstance(x,str) and re.fullmatch('[A-Za-z0-9_-]{43}',x),'base64url syntax')
    raw=base64.urlsafe_b64decode(x+'=')
    require(base64.urlsafe_b64encode(raw).decode().rstrip('=')==x,'base64url pad bits')
    strict_point(raw)
    return raw


def signature(jwk,raw,sig):
    pk=pubkey(jwk)
    require(len(sig)==64,'signature length')
    strict_point(sig[:32]);require(int.from_bytes(sig[32:],'little')<L,'noncanonical scalar')
    ed25519.Ed25519PublicKey.from_public_bytes(pk).verify(sig,raw)


def text(value,maximum=None,blank=False,controls=False):
    require(isinstance(value,str),'not text')
    if maximum is not None:require(len(value)<=maximum,'text code-point limit')
    if not blank:require(any(c not in ' \t\r\n' for c in value),'blank text')
    for c in value:
        cp=ord(c)
        if c in '\t\r\n':
            require(controls,'forbidden text control')
            continue
        require(cp>=0x20 and not 0x7f<=cp<=0x9f and not 0xd800<=cp<=0xdfff
                and not 0xfdd0<=cp<=0xfdef and (cp & 0xffff)<0xfffe,
                'not a Unicode Assignable')


def check_paths(paths):
    files=set();directories={}
    for path in paths:
        require(isinstance(path,str) and path.isascii() and len(path)<=255,'path length/ASCII')
        require(path in ('README.md','sci.json','LICENSE') or path.startswith('files/'),'root path')
        parts=path.split('/')
        for part in parts:
            require(re.fullmatch('[A-Za-z0-9._-]+',part) is not None,'path alphabet/empty component')
            require(part not in ('.','..') and not part.endswith('.'),'path dot component')
            require(re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',part,re.I) is None,'device name')
        folded=path.lower();require(folded not in files,'case collision');files.add(folded)
        for n in range(1,len(parts)):
            directory='/'.join(parts[:n]);lower=directory.lower()
            require(lower not in directories or directories[lower]==directory,'directory case collision')
            directories[lower]=directory
    require(not files.intersection(directories),'file/directory collision')


def check_metadata(m,paths):
    keys(m,('title','abstract'),('categories','keywords','display','citations'))
    text(m['title'],200);text(m['abstract'],2500,controls=True)
    def array(name,maxlen=None):
        a=m.get(name,[]);require(isinstance(a,list),'not an array')
        if maxlen is not None:require(len(a)<=maxlen,'array length')
        return a
    for k in array('keywords',20):text(k,64)
    categories=set()
    for c in array('categories',3):
        keys(c,('scheme','code'))
        require(isinstance(c['scheme'],str) and re.fullmatch('[a-z][a-z0-9.-]{0,63}',c['scheme']),'category scheme')
        text(c['code'],128); pair=(c['scheme'],c['code']);require(pair not in categories,'duplicate category');categories.add(pair)
    displayed=set()
    for d in array('display'):
        keys(d,('path',),('caption',));p=d['path']
        require(isinstance(p,str) and p.startswith('files/') and p in paths and p not in displayed,'display path')
        displayed.add(p)
        if 'caption' in d:text(d['caption'],2000)
    for c in array('citations',1000):
        keys(c,('target',),('relations','reason'));target=c['target'];text(target)
        require(bool(re.fullmatch('sci:sha256:[0-9a-f]{64}',target) or re.fullmatch(r'doi:10\.\d+/\S+',target) or
                     (urlsplit(target).scheme in ('http','https') and urlsplit(target).netloc and not re.search(r'\s',target))),'citation target')
        if 'relations' in c:
            r=c['relations'];require(isinstance(r,list) and all(isinstance(x,str) for x in r),'relations type')
            require(len(r)==len(set(r)) and set(r)<=set(('uses','reviews','supports','challenges','supersedes')),'relations values')
        if 'reason' in c:text(c['reason'],1000,controls=True)


def check_zip(raw):
    """Independent reader: Python zipfile plus direct SCI header/layout checks."""
    require(len(raw)>=22,'short ZIP')
    e=struct.unpack_from('<IHHHHIIH',raw,len(raw)-22)
    magic,disk,startdisk,n1,n2,csize,cstart,comment=e
    require(magic==0x06054b50 and disk==startdisk==comment==0 and n1==n2 and n1<65535,'EOCD fields')
    require(cstart+csize==len(raw)-22,'central extent')
    entries=[];local=0;central=cstart
    for _ in range(n1):
        require(local+30<=cstart and central+46<=len(raw)-22,'header bounds')
        lh=struct.unpack_from('<IHHHHHIIIHH',raw,local)
        ch=struct.unpack_from('<IHHHHHHIIIHHHHHII',raw,central)
        require(lh[0]==0x04034b50 and ch[0]==0x02014b50,'header signatures')
        require(lh[1:6]==(10,0,0,0,33),'local fixed fields')
        require(ch[1:7]==(10,10,0,0,0,33),'central fixed fields')
        require(lh[6:10]==ch[7:11],'shared fields mismatch')
        require(lh[7]==lh[8] and lh[7]<0xffffffff,'stored size')
        require(lh[10]==0 and ch[11:16]==(0,0,0,0,0) and ch[16]==local,'extra/comments/attributes/offset')
        size,namelen=lh[7],lh[9]
        end=local+30+namelen+size
        require(end<=cstart and central+46+namelen<=len(raw)-22,'entry extent')
        name=raw[local+30:local+30+namelen]
        require(name==raw[central+46:central+46+namelen],'filename mismatch')
        entries.append((name.decode('ascii'),raw[local+30+namelen:end]))
        local=end;central+=46+namelen
    require(local==cstart and central==len(raw)-22,'unaccounted ZIP bytes')
    require(len(entries)>=2 and [x[0] for x in entries[:2]]==['manifest.json','manifest.json.sig'],'first entries')
    names=[x[0] for x in entries];require(len(names)==len(set(names)),'duplicate entries')
    require(names[2:]==sorted(names[2:]),'entry ordering')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        require(z.testzip() is None,'CRC mismatch')
        for name,data in entries:require(z.read(name)==data,'independent ZIP reader disagrees')
    return dict(entries)


def canonical_zip(entries):
    """Audit-side encoder, independent of both fixture generators."""
    local=bytearray(); central=bytearray()
    for name,data in entries:
        path=name.encode('ascii'); crc=zlib.crc32(data); n=len(data)
        central.extend(struct.pack('<IHHHHHHIIIHHHHHII',0x02014b50,10,10,0,0,0,33,
                                   crc,n,n,len(path),0,0,0,0,0,len(local))+path)
        local.extend(struct.pack('<IHHHHHIIIHH',0x04034b50,10,0,0,0,33,crc,n,n,len(path),0)+path+data)
    return bytes(local+central+struct.pack('<IHHHHIIH',0x06054b50,0,0,len(entries),len(entries),len(central),len(local),0))


def check_manifest(manifest_raw,sig,context):
    m=load_json(manifest_raw)
    check_schema(m,'sci-publication/0.1')
    keys(m,('schema','public_key','files'))
    if 'publication_id' in context:
        require('sha256:'+hashlib.sha256(manifest_raw).hexdigest()==context['publication_id'],'requested publication ID')
    signature(m['public_key'],manifest_raw,sig)
    listed=m['files'];require(isinstance(listed,dict),'files object')
    check_paths(listed)
    require({'README.md','sci.json'}<=listed.keys(),'required files')
    for p,entry in listed.items():
        keys(entry,('sha256','bytes'))
        n=entry['bytes']
        require(isinstance(n,JsonInteger) and re.fullmatch(r'0|[1-9][0-9]*',n.token)
                and 0<=n<=9007199254740991,'byte count spelling/value')
        require(isinstance(entry['sha256'],str) and re.fullmatch('[0-9a-f]{64}',entry['sha256']),'file hash encoding')
    return m


def check_publication(raw,context):
    size=len(raw) if isinstance(raw,bytes) else sum(map(len,raw.values()))
    if context.get('transport')=='unavailable' or context.get('verification_budget_bytes',size)<size:
        return {'outcome':'incomplete','baseline':None}
    files=check_zip(raw) if isinstance(raw,bytes) else dict(raw)
    manifest_raw=files['manifest.json'];sig=files['manifest.json.sig']
    m=check_manifest(manifest_raw,sig,context);listed=m['files']
    zip_bytes=22+len(manifest_raw)+76+2*len('manifest.json')+len(sig)+76+2*len('manifest.json.sig')
    zip_bytes+=sum(e['bytes']+76+2*len(p) for p,e in listed.items())
    if context.get('scope')=='manifest':
        return {'outcome':'valid','baseline':None,'zip_bytes':zip_bytes,
                'within_baseline_zip_limit':zip_bytes<=67108864}
    if not isinstance(raw,bytes):files.pop('manifest.json.sig.tsr',None)
    require(set(files)==set(listed)|{'manifest.json','manifest.json.sig'},'inventory')
    for p,entry in listed.items():
        require(len(files[p])==entry['bytes'] and hashlib.sha256(files[p]).hexdigest()==entry['sha256'],'file integrity')
    metadata=load_json(files['sci.json']);check_metadata(metadata,listed)
    text(files['README.md'].decode('utf-8'),controls=True)
    if 'LICENSE' in files:text(files['LICENSE'].decode('utf-8'),blank=True,controls=True)
    baseline=(zip_bytes<=67108864 and len(listed)<=1000 and len(manifest_raw)<=262144 and
              len(files['sci.json'])<=65536 and len(files['README.md'])<=1048576 and
              len(metadata.get('display',[]))<=3 and files.get('LICENSE')==(ROOT/'sci-baseline-LICENSE.txt').read_bytes() and
              all(baseline_png(d['path'], files[d['path']]) for d in metadata.get('display', [])))
    z=raw if isinstance(raw,bytes) else canonical_zip([('manifest.json',manifest_raw),('manifest.json.sig',sig)]+[(p,files[p]) for p in sorted(listed)])
    require(len(z)==zip_bytes,'ZIP size formula')
    return {'outcome':'valid','baseline':baseline,'zip_bytes':zip_bytes,'zip_sha256':hashlib.sha256(z).hexdigest()}


def check_record(raw,sig,kind,ctx):
    if ctx.get('transport')=='unavailable':return {'outcome':'incomplete'}
    m=load_json(raw)
    schema='sci-key-revocation/0.1' if kind=='revocation' else 'sci-key-claim/0.1'
    check_schema(m,schema)
    if kind=='revocation':keys(m,('schema','public_key'))
    else:keys(m,('schema','public_key','account','expires'),('note',))
    signature(m['public_key'],raw,sig)
    require(m['public_key']['x']==ctx['expected_public_key_x'],'requested key')
    if kind=='claim':
        keys(m['account'],('type','id'));a=m['account']
        require(a==ctx['requested_account'],'requested account')
        require(isinstance(a['type'],str) and isinstance(a['id'],str),'account strings')
        if a['type']=='domain':
            domain=a['id']
            require(domain==domain.lower() and domain.isascii() and not domain.endswith('.'),'domain spelling')
            try:ipaddress.ip_address(domain)
            except ValueError:pass
            else:raise Invalid('domain cannot be IP')
            require(idna.encode(domain,uts46=False).decode('ascii')==domain,'IDNA2008 domain')
        elif a['type']=='github':require(a['id']==a['id'].lower(),'GitHub lowercase login')
        else:require(a['type']=='huggingface','unknown account method')
        if 'note' in m:text(m['note'],2500,controls=True)
        require(isinstance(m['expires'],str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})',m['expires']),'RFC 3339 expiry')
        require(datetime.fromisoformat(ctx['now'])<datetime.fromisoformat(m['expires'].replace('z','Z')),'claim expired')
        require(not ctx['known_revocation'] and ctx['retrieved_from_account_location'],'revoked/unhosted claim')
    return {'outcome':'valid'}


def check_descriptor(raw,kind):
    obj=load_json(raw)
    check_schema(obj,f'sci-{kind}/0.1')
    fields={'publication_lookup_url':{'id'},'revocation_lookup_url':{'x','file'},'timestamp_lookup_url':{'id'}} if kind=='index' else {
        'bundle_url':{'id','id2'},'entry_url':{'id','id2','+path'},'blob_url':{'sha256'},'list_url':set()}
    if kind=='index':require(fields.keys()<=obj.keys(),'required lookup route')
    fields.update({name:set() for name in ('submit_url','policy_url','docs_url')} if kind=='index' else {})
    for name,allowed in fields.items():
        if name not in obj:continue
        v=obj[name];require(isinstance(v,str) and bool(urlsplit(v).scheme),'absolute URL')
        actual=set(re.findall(r'\{([^}]+)\}',v))
        require(not re.search(r'[{}]',re.sub(r'\{[^{}]+\}','',v)),'malformed URL template')
        require(actual<=allowed,'unknown template expression')
        if kind=='index':require(actual==allowed,'missing template expression')
    return {'outcome':'valid'}


def check_submission(raw,ctx):
    obj=load_json(raw)
    if ctx['phase']=='request':
        keys(obj,('id','descriptors'),('account',))
        require(isinstance(obj['id'],str) and re.fullmatch('[0-9a-f]{64}',obj['id']),'submission ID')
        urls=obj['descriptors']
        require(isinstance(urls,list) and bool(urls),'descriptor list')
        require(all(isinstance(u,str) and bool(urlsplit(u).scheme) and not re.search(r'\s',u) for u in urls),'absolute descriptor URLs')
        require(len(set(urls))==len(urls),'duplicate descriptor URL')
        if 'account' in obj:
            a=obj['account'];keys(a,('type','id'))
            require(all(isinstance(a[k],str) for k in a),'account strings')
    else:
        require(ctx['content_type'].split(';',1)[0]=='application/json','response content type')
        require(isinstance(obj,dict),'response object')
        if ctx['status'] in (200,201):
            require(obj.get('id')==ctx['requested_id'],'response publication ID')
        else:
            require(400<=ctx['status']<=599,'response status')
            error=obj.get('error')
            require(isinstance(error,dict) and isinstance(error.get('code'),str)
                    and isinstance(error.get('message'),str) and type(error.get('retryable')) is bool,'error contract')
    return {'outcome':'valid'}


def frame(data,offset):
    """Read minimal definite DER tag/length with no resynchronization."""
    pos=offset
    if pos>=len(data):return None
    tag=data[pos];pos+=1
    if tag&31==31:
        first=True
        while True:
            if pos>=len(data):return None
            v=data[pos];pos+=1
            if first and v&127==0:return None
            first=False
            if not v&128:break
    if pos>=len(data):return None
    v=data[pos];pos+=1
    if v<128:length=v
    else:
        n=v&127
        if n==0 or n>8 or pos+n>len(data) or data[pos]==0:return None
        length=int.from_bytes(data[pos:pos+n],'big');pos+=n
        if length<128:return None
    if length>len(data)-pos:return None
    return pos+length


def timestamp_value(data,ctx,signature_bytes,out):
    response=tsp.TimeStampResp.load(data,strict=True)
    require(response.dump(force=True)==data,'non-DER encoding')
    require(response['status']['status'].native in ('granted','granted_with_mods'),'timestamp status')
    token=response['time_stamp_token'];require(token.native is not None,'missing token')
    require(token['content_type'].native=='signed_data','CMS content type')
    sd=token['content'];eci=sd['encap_content_info']
    require(eci['content_type'].native=='tst_info','encapsulated content type')
    info=eci['content'].parsed
    imprint=info['message_imprint']
    require(imprint['hash_algorithm']['algorithm'].native=='sha256','SCI imprint algorithm')
    require(imprint['hashed_message'].native==hashlib.sha256(signature_bytes).digest(),'imprint mismatch')
    si=sd['signer_infos'][0]
    cert_asn=sd['certificates'][0].chosen
    cert=x509.load_der_x509_certificate(cert_asn.dump())
    sid=si['sid'].chosen
    require(sid['serial_number'].native==cert.serial_number and sid['issuer'].dump()==cert_asn.issuer.dump(),'signer identity')
    attrs={x['type'].native:x['values'][0] for x in si['signed_attrs']}
    require(attrs['content_type'].native=='tst_info','signed content type')
    require(attrs['message_digest'].native==hashlib.sha256(info.dump()).digest(),'CMS digest')
    require(attrs['signing_certificate_v2']['certs'][0]['cert_hash'].native==hashlib.sha256(cert_asn.dump()).digest(),'ESS certificate binding')
    cert.public_key().verify(si['signature'].native,si['signed_attrs'].untag().dump(),padding.PKCS1v15(),hashes.SHA256())
    anchors=ctx['trust_anchors']
    if not anchors:return 'untrusted'
    root=x509.load_pem_x509_certificate((out/anchors[0]).read_bytes())
    require(cert.issuer==root.subject,'certificate issuer')
    root.public_key().verify(cert.signature,cert.tbs_certificate_bytes,padding.PKCS1v15(),hashes.SHA256())
    eku=cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    require(eku.critical and list(eku.value)==[ExtendedKeyUsageOID.TIME_STAMPING],'TSA EKU')
    now=datetime.fromisoformat(ctx['verification_time'])
    require(cert.not_valid_before_utc<=now<=cert.not_valid_after_utc,'certificate validity')
    for path in ctx['crls']:
        crl=x509.load_pem_x509_crl((out/path).read_bytes())
        require(crl.issuer==root.subject,'CRL issuer')
        root.public_key().verify(crl.signature,crl.tbs_certlist_bytes,padding.PKCS1v15(),hashes.SHA256())
        require(crl.last_update_utc<=now<crl.next_update_utc,'CRL validity')
        require(crl.get_revoked_certificate_by_serial_number(cert.serial_number) is None,'TSA revoked')
    return 'verified'


def check_timestamp(case,out):
    paths=case['artifacts'];ctx=case['context'];data=(out/paths['proofs.tsr']).read_bytes()
    sig=(out/paths['signature.bin']).read_bytes()
    name='revocation.json' if 'revocation.json' in paths else 'manifest.json'
    raw=(out/paths[name]).read_bytes();record=load_json(raw);signature(record['public_key'],raw,sig)
    if name=='manifest.json':require('sha256:'+hashlib.sha256(raw).hexdigest()==ctx['publication_id'],'timestamp publication ID')
    pos=0;proofs=[]
    while pos<len(data):
        end=frame(data,pos)
        if end is None:break
        try:status=timestamp_value(data[pos:end],ctx,sig,out)
        except (ValueError,KeyError,TypeError,InvalidSignature):status='invalid'
        proof={'offset':pos,'bytes':end-pos,'result':status}
        if status=='verified':
            info=tsp.TimeStampResp.load(data[pos:end])['time_stamp_token']['content']['encap_content_info']['content'].parsed
            proof.update(gen_time=info['gen_time'].native.isoformat().replace('+00:00','Z'),accuracy_seconds=info['accuracy']['seconds'].native)
        proofs.append(proof);pos=end
    framing='complete' if pos==len(data) and data else 'malformed'
    allgood=framing=='complete' and all(x['result']=='verified' for x in proofs)
    anygood=any(x['result']=='verified' for x in proofs)
    result={'outcome':'valid' if allgood else ('partial' if anygood else ('untrusted' if framing=='complete' and all(x['result']=='untrusted' for x in proofs) else 'invalid')),
            'framing':framing,'proofs':proofs,'stop_offset':pos}
    if name=='manifest.json':result.update(publication_outcome='valid',publication_id_unchanged=True)
    else:result['revocation_outcome']='valid'
    return result


def openssl_crosscheck(case,out):
    """Independently check each complete RFC 3161 response using OpenSSL."""
    data=(out/case['artifacts']['proofs.tsr']).read_bytes()
    ctx=case['context']
    with tempfile.TemporaryDirectory(prefix='sci-ts-audit-') as td:
        tmp=Path(td)
        # Include the signed CRL explicitly; no network revocation fetch is involved.
        trust=b''.join((out/p).read_bytes() for p in ctx['trust_anchors']+ctx['crls'])
        (tmp/'trust.pem').write_bytes(trust)
        for proof in case['expected']['proofs']:
            if proof['result']=='untrusted':continue
            raw=data[proof['offset']:proof['offset']+proof['bytes']]
            (tmp/'response.tsr').write_bytes(raw)
            cmd=['openssl','ts','-verify','-in',str(tmp/'response.tsr'),'-data',str(out/case['artifacts']['signature.bin']),
                 '-CAfile',str(tmp/'trust.pem'),'-attime',str(int(NOW.timestamp())),'-crl_check']
            r=subprocess.run(cmd,capture_output=True)
            if proof['result']=='verified':
                require(r.returncode==0,f'OpenSSL rejected {case["id"]}: {r.stderr.decode()}')
            elif case['id'] in ('timestamp-sha512-imprint','timestamp-sha1-imprint'):
                require(r.returncode==0,'Non-SHA-256 fixture should pass RFC 3161 but fail the SCI imprint rule')
            else:
                # SHA-512 is legal RFC 3161; SCI additionally requires SHA-256.
                require(r.returncode!=0,f'OpenSSL unexpectedly accepted invalid proof in {case["id"]}')


def openssl_signature(raw,sig,jwk):
    with tempfile.TemporaryDirectory(prefix='sci-ed25519-audit-') as td:
        p=Path(td)
        pk=base64.urlsafe_b64decode(jwk['x']+'=')
        pem=ed25519.Ed25519PublicKey.from_public_bytes(pk).public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
        (p/'public.pem').write_bytes(pem);(p/'record').write_bytes(raw);(p/'signature').write_bytes(sig)
        r=subprocess.run(['openssl','pkeyutl','-verify','-rawin','-pubin','-inkey',str(p/'public.pem'),'-in',str(p/'record'),'-sigfile',str(p/'signature')],capture_output=True)
        require(r.returncode==0,'OpenSSL signature check failed: '+r.stderr.decode())


def outcome(fn,*args):
    try:return fn(*args)
    except Unsupported:return {'outcome':'unsupported','baseline':None} if fn==check_publication else {'outcome':'unsupported'}
    except (ValueError,KeyError,TypeError,InvalidSignature,zipfile.BadZipFile,struct.error,OverflowError):
        return {'outcome':'invalid','baseline':None} if fn==check_publication else {'outcome':'invalid'}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--corpus',type=Path,default=ROOT/'test-vectors/0.1');ap.add_argument('--openssl',action='store_true')
    args=ap.parse_args();out=args.corpus
    catalog=json.loads((out/'catalog.json').read_text());inventory=json.loads((out/'inventory.json').read_text())
    actual={str(p.relative_to(out)) for p in out.rglob('*') if p.is_file() and p!=out/'inventory.json'}
    require(actual==set(inventory),'untracked or missing corpus files')
    for name,expected in inventory.items():
        require(not Path(name).is_absolute() and '..' not in Path(name).parts,'unsafe inventory path')
        require(not (out/name).is_symlink(),'symlink artifact')
        data=(out/name).read_bytes()
        require(len(data)==expected['bytes'] and hashlib.sha256(data).hexdigest()==expected['sha256'],'inventory mismatch '+name)
    require(hashlib.sha256((ROOT/'README.md').read_bytes()).hexdigest()==catalog['spec_sha256'],'spec changed; review/update corpus pin')
    ids=[c['id'] for c in catalog['cases']]
    require(len(ids)==len(set(ids)),'duplicate case IDs')
    for case in catalog['cases']:
        require(set(case['artifacts'].values())<=set(inventory),'artifact missing from inventory')
    counts=Counter();declarative=0;failures=[]
    for case in catalog['cases']:
        kind=case['kind'];paths=case['artifacts'];ctx=case.get('context',{})
        require(kind in ('publication','claim','revocation','discovery','submission','timestamp','template','scenario','http'),'unknown case kind')
        def read(name):return (out/paths[name]).read_bytes()
        if kind=='publication':
            payload=read('bundle.zip') if 'bundle.zip' in paths else {name:read(name) for name in paths}
            result=outcome(check_publication,payload,ctx)
            if args.openssl and (case['expected']['outcome']=='valid' or case['id']=='mixed-order-public-key'):
                if isinstance(payload,bytes):
                    with zipfile.ZipFile(io.BytesIO(payload)) as z:
                        raw=z.read('manifest.json');sig=z.read('manifest.json.sig')
                else:raw=payload['manifest.json'];sig=payload['manifest.json.sig']
                openssl_signature(raw,sig,json.loads(raw)['public_key'])
        elif kind in ('claim','revocation'):
            result=outcome(check_record,read('record.json'),read('record.json.sig'),kind,ctx)
            if args.openssl and case['expected']['outcome']=='valid':
                openssl_signature(read('record.json'),read('record.json.sig'),json.loads(read('record.json'))['public_key'])
        elif kind=='discovery':result=outcome(check_descriptor,read('descriptor.json'),ctx['descriptor_kind'])
        elif kind=='submission':result=outcome(check_submission,read('request.json' if ctx['phase']=='request' else 'response.json'),ctx)
        elif kind=='timestamp':
            result=check_timestamp(case,out)
            if args.openssl:openssl_crosscheck(case,out)
        elif kind=='template':
            templates=json.loads(read('templates.json'));variables=json.loads(read('variables.json'))
            result={k:re.sub(r'\{\+?([^}]+)\}',lambda m:variables[m[1]],templates[k]) for k in case['expected']}
        else:
            # Stateful service/HTTP assertions are intentionally declarative adapter inputs.
            # Check their fixture syntax and leave policy execution to an implementation.
            if kind=='scenario':require(isinstance(load_json(read('events.json'))['events'],list),'scenario events')
            if kind=='http':require(type(ctx['status']) is int,'HTTP status')
            declarative+=1;counts[kind]+=1;continue
        if {k:result.get(k) for k in case['expected']}!=case['expected']:failures.append((case['id'],case['expected'],result))
        counts[kind]+=1
    for name,expected,result in failures:
        print(name,'\n expected:',expected,'\n observed:',result)
    require(not failures,f'{len(failures)} fixture expectations failed')
    print(f'Audited {sum(counts.values())} fixtures: {dict(counts)}; {declarative} HTTP/state scenarios are declarative adapter inputs.')
    if args.openssl:print('OpenSSL independently checked positive Ed25519 signatures, the mixed-order-key equation, and complete timestamp proofs with chains and supplied CRLs.')

if __name__=='__main__':main()
