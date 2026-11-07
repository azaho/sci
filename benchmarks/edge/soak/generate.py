"""Independent Python fixtures for frozen draft checks. All keys are disposable."""
import argparse,base64,hashlib,io,json,subprocess,zipfile,random
from pathlib import Path
from datetime import datetime,timedelta,timezone
from cryptography import x509
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ed25519,rsa,padding
from asn1crypto import cms,tsp,x509 as ax509

enc=lambda x:json.dumps(x,separators=(',',':'),ensure_ascii=False).encode()
b64=lambda x:base64.b64encode(x).decode()
def generate(out,small=False,profiles=None,variants=range(4)):
 out.mkdir(parents=True,exist_ok=True)
 now=datetime.now(timezone.utc).replace(microsecond=0)
 ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 tsa_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 ca_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'SCI disposable benchmark root')])
 tsa_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'SCI disposable benchmark TSA')])
 def cert(subject,issuer,key,signer,serial,ca):
  b=x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key()).serial_number(serial).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=2)).add_extension(x509.BasicConstraints(ca=ca,path_length=0 if ca else None),True).add_extension(x509.KeyUsage(digital_signature=True,content_commitment=False,key_encipherment=False,data_encipherment=False,key_agreement=False,key_cert_sign=ca,crl_sign=ca,encipher_only=None,decipher_only=None),True)
  if not ca:b=b.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]),True)
  return b.sign(signer,hashes.SHA256())
 root=cert(ca_name,ca_name,ca_key,ca_key,1,True); leaf=cert(tsa_name,ca_name,tsa_key,ca_key,2,False)
 def crl(revoked=False,stale=False):
  b=x509.CertificateRevocationListBuilder().issuer_name(ca_name).last_update(now-timedelta(hours=2)).next_update(now+timedelta(days=1) if not stale else now-timedelta(hours=1))
  if revoked:b=b.add_revoked_certificate(x509.RevokedCertificateBuilder().serial_number(2).revocation_date(now-timedelta(hours=1)).build())
  return b.sign(ca_key,hashes.SHA256()).public_bytes(serialization.Encoding.DER)
 root_der=root.public_bytes(serialization.Encoding.DER); leaf_der=leaf.public_bytes(serialization.Encoding.DER); ac= ax509.Certificate.load(leaf_der)
 trusts={'normal':{'roots':[b64(root_der)],'crls':[b64(crl())]},'revoked':{'roots':[b64(root_der)],'crls':[b64(crl(True))]},'stale':{'roots':[b64(root_der)],'crls':[b64(crl(stale=True))]},'untrusted':{'roots':[],'crls':[b64(crl())]}}
 (out/'trust.json').write_bytes(enc(trusts))
 (out/'root.pem').write_bytes(root.public_bytes(serialization.Encoding.PEM))
 def proof(signature,fault=None):
  imprint=hashlib.sha256(signature).digest()
  if fault=='imprint':imprint=b'\0'*32
  info=tsp.TSTInfo({'version':'v1','policy':'1.3.6.1.4.1.55555.1','message_imprint':{'hash_algorithm':{'algorithm':'sha256'},'hashed_message':imprint},'serial_number':3,'gen_time':now,'accuracy':{'seconds':1,'millis':1}})
  attrs=cms.CMSAttributes([{'type':'content_type','values':['tst_info']},{'type':'message_digest','values':[hashlib.sha256(info.dump()).digest()]},{'type':'signing_certificate_v2','values':[{'certs':[{'cert_hash':hashlib.sha256(leaf_der).digest() if fault!='ess' else b'\0'*32}]}]}])
  signed=tsa_key.sign(attrs.dump(),padding.PKCS1v15(),hashes.SHA256())
  if fault=='signature':signed=bytes([signed[0]^1])+signed[1:]
  signer=cms.SignerInfo({'version':'v1','sid':{'issuer_and_serial_number':{'issuer':ac.issuer,'serial_number':2}},'digest_algorithm':{'algorithm':'sha256'},'signed_attrs':attrs,'signature_algorithm':{'algorithm':'sha256_rsa'},'signature':signed})
  sd=cms.SignedData({'version':'v3','digest_algorithms':[{'algorithm':'sha256'}],'encap_content_info':{'content_type':'tst_info','content':info},'certificates':[ac],'signer_infos':[signer]})
  return tsp.TimeStampResp({'status':{'status':'granted'},'time_stamp_token':{'content_type':'signed_data','content':sd}}).dump()
 cases=[]
 for profile in (profiles or ['ordinary','unicode','ascii','longpaths','skewed']):
  for variant in variants:
   name=f'{profile}-{variant}'
   rng=random.Random(name)
   key=ed25519.Ed25519PrivateKey.generate()
   raw=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
   pub={'kty':'OKP','crv':'Ed25519','x':base64.urlsafe_b64encode(raw).decode().rstrip('=')}
   base='files/'
   if profile=='longpaths':base+='a'*60+'/'+'b'*60+'/'+'d'*40+'/'
   paths=[base+f'{i:05d}-'+('c'*54 if profile=='longpaths' else f'v{variant}')+'.bin' for i in range(997)]
   heavy=profile in ['unicode','ascii','longpaths']
   meta={'title':f'Synthetic soak {name} 科学 😀','abstract':'Synthetic data.\nNot a research claim.',
         'categories':[{'scheme':'arxiv','code':'cs.AI'},{'scheme':'anzsrc-for-2020','code':'4602'},{'scheme':'custom','code':'test'}],
         'keywords':[f'keyword{i}' for i in range(20)],'display':[{'path':paths[0],'caption':'Example'}],
         'citations':[{'target':'doi:10.6338/JDA.202212%2FSP_17(4).0000','relations':['reviews'],'reason':'Synthetic citation.'}]}
   files={'README.md':f'Synthetic soak {name}.\n'.encode(),'LICENSE':b'Synthetic fixture; CC0.\n'}
   if heavy:
    reason='文' if profile=='unicode' else chr(65+variant)
    meta['citations']=[{'target':'sci:sha256:'+hashlib.sha256(f'{name}-{i}'.encode()).hexdigest(),
                       'relations':['uses','reviews','supports','challenges','supersedes'],'reason':reason*1000} for i in range(1000)]
    meta['display']=[{'path':p,'caption':chr(97+variant)*2000} for p in paths[:100 if profile=='unicode' else 997]]
    meta['title']='T'*200;meta['abstract']='A'*2500
    files['README.md']=(b'a'*1_000_000 if variant%2==0 else ('文'*333333+'\n').encode())
    files['LICENSE']=b'L'*1_000_000
   files['sci.json']=enc(meta)
   if heavy:
    assert len(files['sci.json'])<=4_000_000
    files['sci.json']+=b' '*(4_000_000-len(files['sci.json']))
   payload=7_000_000 if small else 50_000_000
   remaining=payload-sum(map(len,files.values()))
   for i,p in enumerate(paths):
    left=len(paths)-i
    if left==1:n=remaining
    elif profile=='skewed' and i==0:n=remaining*9//10
    else:n=rng.randrange(max(1,remaining//left*2))
    n=min(n,remaining);pattern=hashlib.sha256(f'{name}-{p}'.encode()).digest()
    files[p]=(pattern*((n+31)//32))[:n];remaining-=n
   assert len(files)==1000 and sum(map(len,files.values()))==payload
   doc={'schema':'sci-publication/0.18','public_key':pub,'files':{p:{'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b)} for p,b in sorted(files.items())}}
   manifest=enc(doc)
   if heavy:manifest+=b' '*(1_048_576-len(manifest))
   sig=key.sign(manifest)
   items=[('manifest.json',manifest),('manifest.json.sig',sig),*sorted(files.items())]
   timestamped=heavy or variant%2==1
   if timestamped:
    ts=proof(sig);items.append(('manifest.json.sig.tsr',ts))
    (out/'example.sig').write_bytes(sig);(out/'example.tsr').write_bytes(ts)
   buf=io.BytesIO()
   with zipfile.ZipFile(buf,'w',compression=zipfile.ZIP_STORED,allowZip64=False) as z:
    for path,data in items:
     info=zipfile.ZipInfo(path,date_time=(2026,1,1,0,0,0));info.create_system=0;info.create_version=info.extract_version=10;info.external_attr=0x20;z.writestr(info,data)
   blob=bytearray(buf.getvalue());pos=int.from_bytes(blob[-6:-2],'little')
   while blob[pos:pos+4]==b'PK\1\2':
    blob[pos+38:pos+42]=b'\0'*4;pos+=46+int.from_bytes(blob[pos+28:pos+30],'little')
   assert len(blob)<=52_428_800
   (out/f'{name}.zip').write_bytes(blob)
   cases.append({'name':name,'profile':profile,'variant':variant,'id':hashlib.sha256(manifest).hexdigest(),
                 'zip_bytes':len(blob),'payload_bytes':payload,'files':len(files),'manifest_bytes':len(manifest),
                 'metadata_bytes':len(files['sci.json']),'timestamped':timestamped,'expected_error':None,'file_limit':1000,'trust':'normal'})
 (out/'cases.json').write_bytes(enc(cases))
 subprocess.run(['openssl','ts','-verify','-in',str(out/'example.tsr'),'-data',str(out/'example.sig'),'-CAfile',str(out/'root.pem')],check=True,capture_output=True)
 return cases
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('output',type=Path);p.add_argument('--small',action='store_true');a=p.parse_args();print(len(generate(a.output,a.small)),'soak fixtures generated; sample timestamp verified by OpenSSL.')
