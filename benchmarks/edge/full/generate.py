"""Independent Python fixtures for frozen draft checks. All keys are disposable."""
import argparse,base64,hashlib,io,json,subprocess,zipfile
from pathlib import Path
from datetime import datetime,timedelta,timezone
from cryptography import x509
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ed25519,rsa,padding
from asn1crypto import cms,tsp,x509 as ax509

enc=lambda x:json.dumps(x,separators=(',',':'),ensure_ascii=False).encode()
b64=lambda x:base64.b64encode(x).decode()
def generate(out,small=False):
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
 key=ed25519.Ed25519PrivateKey.generate();raw=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
 pub={'kty':'OKP','crv':'Ed25519','x':base64.urlsafe_b64encode(raw).decode().rstrip('=')}
 configs=[('baseline',3,None,None),('50mb-1000',1000,None,None),('50mb-1000-heavy',1000,'heavy',None),('50mb-2000-heavy',2000,'heavy',None),('50mb-5000-heavy',5000,'heavy',None),('1001-rejected',1001,None,'required_files')]
 faults={
 'json_nodes':'json_nodes','json_depth':'json_depth','metadata_unknown':'metadata_fields','category_duplicate':'category_duplicate','category_scheme':'category_scheme','keyword_limit':'keywords','display_missing':'display_path','display_duplicate':'display_path','citation_relation':'relations','citation_extra':'citation_fields','citation_doi':'doi_syntax','doi_utf8':'doi_utf8','doi_control':'doi_character','citation_count':'citations','title_tab':'text_character','abstract_hidden':'text_character','readme_bidi':'text_character','license_bom':'text_character','text_noncharacter':'text_character','text_blank':'text_blank','title_too_long':'text_length','duplicate_escape':'json_duplicate','surrogate':'json_surrogate','json_bom':'json_bom','json_trailing':'json_trailing','size_exponent':'json_comma','manifest_unknown':'manifest_fields','unsafe_path':'path_component','path_collision':'path_collision','path_prefix':'path_directory','directory_case':'path_directory','path_too_long':'path_limit','hash':'file_hash','signature':'signature','key_identity':'signature','sig_scalar':'signature','key_noncanonical':'jwk','central':'zip_directory','trailing':'zip_end','zip_crc':'zip_crc','zip_extra':'zip_local','zip_compressed':'zip_local','zip_attrs':'zip_directory','zip_disk':'zip_end','manifest_limit':'manifest_limit','metadata_limit':'metadata_limit','text_limit':'text_limit','timestamp_der':'timestamp_der','timestamp_imprint':'timestamp_imprint','timestamp_signature':'timestamp_chain_or_signature','timestamp_ess':'timestamp_ess','timestamp_revoked':'timestamp_chain_or_signature','timestamp_stale':'timestamp_chain_or_signature','timestamp_untrusted':'timestamp_trust'}
 configs += [(name,5,name,error) for name,error in faults.items()]
 cases=[]
 for name,count,fault,expected in configs:
  limit=count if name in ('50mb-2000-heavy','50mb-5000-heavy') else 1000
  size=(50_000_000 if not small else 6_000_000) if name.startswith('50mb') else 100_000
  paths=[f'files/{i:05d}.bin' for i in range(count-3)]
  meta={'title':'Benchmark 科学 😀','abstract':'Synthetic data.\nNot research.','categories':[{'scheme':'arxiv','code':'cs.AI'}],'keywords':['benchmark'],'display':[{'path':paths[0],'caption':'Example'}] if paths else [],'citations':[{'target':'doi:10.6338/JDA.202212%2FSP_17(4).0000','relations':['reviews'],'reason':'Synthetic citation.'}]}
  files={'README.md':b'Synthetic publication.\n','LICENSE':b'Synthetic benchmark fixture; CC0.\n'}
  timestamped=fault=='heavy' or name.startswith('timestamp_')
  if fault=='heavy':
   meta['citations']=[{'target':'sci:sha256:'+hashlib.sha256(str(i).encode()).hexdigest(),'relations':['uses','reviews','supports','challenges','supersedes'],'reason':'文'*1000} for i in range(1000)]
   meta['display']=[{'path':p,'caption':'c'*2000} for p in paths[:100]]
   files['README.md']=b'a'*1_000_000; files['LICENSE']=b'b'*1_000_000
  if fault=='json_nodes':meta['keywords']=['x']*20001
  if fault=='metadata_unknown':meta['unexpected']='x'
  if fault=='category_duplicate':meta['categories']*=2
  if fault=='category_scheme':meta['categories'][0]['scheme']='Bad'
  if fault=='keyword_limit':meta['keywords']=['x']*21
  if fault=='display_missing':meta['display']=[{'path':'files/missing'}]
  if fault=='display_duplicate':meta['display']*=2
  if fault=='citation_relation':meta['citations'][0]['relations']=['reviews','reviews']
  if fault=='citation_extra':meta['citations'][0]['extra']=1
  if fault=='citation_doi':meta['citations'][0]['target']='doi:10.123/a/b'
  if fault=='doi_utf8':meta['citations'][0]['target']='doi:10.123/%FF'
  if fault=='doi_control':meta['citations'][0]['target']='doi:10.123/%00'
  if fault=='citation_count':meta['citations']*=1001
  if fault=='title_tab':meta['title']='hello\tworld'
  if fault=='abstract_hidden':meta['abstract']='zero\u200bwidth'
  if fault=='readme_bidi':files['README.md']='bad\u202etext'.encode()
  if fault=='license_bom':files['LICENSE']=b'\xef\xbb\xbflicense'
  if fault=='text_noncharacter':meta['title']='bad\uffff'
  if fault=='text_blank':meta['title']='   '
  if fault=='title_too_long':meta['title']='😀'*201
  if fault=='text_limit':files['README.md']=b'x'*1_000_001
  files['sci.json']=enc(meta)
  if fault=='heavy':files['sci.json']+=b' '*(4_000_000-len(files['sci.json']))
  if fault=='metadata_limit':files['sci.json']+=b' '*(4_000_001-len(files['sci.json']))
  if fault=='surrogate':files['sci.json']=b'{"title":"\\ud800","abstract":"x"}'
  if fault=='duplicate_escape':files['sci.json']=b'{"title":"x","\\u0074itle":"y","abstract":"x"}'
  if fault=='json_depth':files['sci.json']=b'{"title":'+b'['*40+b'0'+b']'*40+b'}'
  if fault=='json_bom':files['sci.json']=b'\xef\xbb\xbf'+files['sci.json']
  if fault=='json_trailing':files['sci.json']+=b'garbage'
  remaining=max(len(paths),size-sum(map(len,files.values())))
  for i,p in enumerate(paths):
   n=remaining//(len(paths)-i);pattern=hashlib.sha256(p.encode()).digest();files[p]=(pattern*((n+31)//32))[:n];remaining-=n
  if fault=='unsafe_path':files['files/../evil']=files.pop(paths[0])
  if fault=='path_collision':files['files/A']=b'a';files['files/a']=b'a'
  if fault=='path_prefix':files['files/A']=b'a';files['files/a/b']=b'a'
  if fault=='directory_case':files['files/A/x']=b'a';files['files/a/y']=b'a'
  if fault=='path_too_long':files['files/'+'a'*65]=b'a'
  doc={'schema':'sci-publication/0.18','public_key':dict(pub),'files':{p:{'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b)} for p,b in sorted(files.items())}}
  if fault=='manifest_unknown':doc['unknown']=1
  if fault=='key_identity':doc['public_key']['x']=base64.urlsafe_b64encode(b'\1'+b'\0'*31).decode().rstrip('=')
  if fault=='key_noncanonical':doc['public_key']['x']=doc['public_key']['x'][:-1]+'B'
  manifest=enc(doc)
  if fault=='manifest_limit':manifest+=b' '*(1_048_577-len(manifest))
  if fault=='size_exponent':manifest=manifest.replace(b'"bytes":',b'"bytes":1e',1)
  sig=key.sign(manifest)
  if fault=='signature':sig=bytes([sig[0]^1])+sig[1:]
  if fault=='sig_scalar':sig=sig[:32]+b'\xff'*32
  if fault=='hash':files[paths[-1]]=b'X'+files[paths[-1]][1:]
  items=[('manifest.json',manifest),('manifest.json.sig',sig),*sorted(files.items())]
  if timestamped:
   ts=proof(sig,{'timestamp_imprint':'imprint','timestamp_signature':'signature','timestamp_ess':'ess'}.get(fault))
   if fault=='timestamp_der':ts+=b'x'
   items.append(('manifest.json.sig.tsr',ts))
   if name=='50mb-1000-heavy':
    (out/'example.sig').write_bytes(sig);(out/'example.tsr').write_bytes(ts)
  buf=io.BytesIO()
  with zipfile.ZipFile(buf,'w',compression=zipfile.ZIP_STORED,allowZip64=False) as z:
   for path,data in items:
    info=zipfile.ZipInfo(path,date_time=(2026,1,1,0,0,0));info.create_system=0;info.create_version=info.extract_version=10;info.external_attr=0x20;z.writestr(info,data)
  blob=bytearray(buf.getvalue());central=int.from_bytes(blob[-6:-2],'little');pos=central
  while blob[pos:pos+4]==b'PK\1\2':
   blob[pos+38:pos+42]=b'\0'*4;pos+=46+int.from_bytes(blob[pos+28:pos+30],'little')
  if fault=='central':blob[central+46]^=1
  if fault=='trailing':blob+=b'x'
  if fault=='zip_crc':blob[14]^=1;blob[central+16]^=1
  if fault=='zip_extra':blob[28]=1
  if fault=='zip_compressed':blob[8]=8
  if fault=='zip_attrs':blob[central+38]=1
  if fault=='zip_disk':blob[-18]=1
  (out/f'{name}.zip').write_bytes(blob)
  cases.append({'name':name,'id':hashlib.sha256(manifest).hexdigest(),'zip_bytes':len(blob),'payload_bytes':sum(map(len,files.values())),'files':len(files),'manifest_bytes':len(manifest),'expected_error':expected,'file_limit':limit,'trust':{'timestamp_revoked':'revoked','timestamp_stale':'stale','timestamp_untrusted':'untrusted'}.get(fault,'normal')})
 (out/'cases.json').write_bytes(enc(cases))
 # OpenSSL independently verifies a positive timestamp generated by asn1crypto/cryptography.
 subprocess.run(['openssl','ts','-verify','-in',str(out/'example.tsr'),'-data',str(out/'example.sig'),'-CAfile',str(out/'root.pem')],check=True,capture_output=True)
 return cases
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('output',type=Path);p.add_argument('--small',action='store_true');a=p.parse_args();print(len(generate(a.output,a.small)),'fixtures generated; OpenSSL verified the sample timestamp.')
