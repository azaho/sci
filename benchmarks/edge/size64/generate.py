"""Exact 64 MiB canonical ZIPs using a valid subset of the current 0.1 spec."""
import argparse,base64,hashlib,importlib.util,io,json,zipfile,shutil
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
spec=importlib.util.spec_from_file_location('seed',Path(__file__).parent.parent/'soak/generate.py')
seed=importlib.util.module_from_spec(spec);spec.loader.exec_module(seed)
TARGET=67_108_864

def generate(out):
 out.mkdir(parents=True,exist_ok=True);seed_dir=out/'seed'
 seed.generate(seed_dir,profiles=['ordinary','ascii','skewed'],variants=[0])
 cases=[]
 for profile,source in [('ordinary','ordinary-0'),('heavy','ascii-0'),('single-large-file','ordinary-0'),('large-readme','ordinary-0')]:
  with zipfile.ZipFile(seed_dir/f'{source}.zip') as z:
   files={n:z.read(n) for n in z.namelist() if n not in ['manifest.json','manifest.json.sig','manifest.json.sig.tsr']}
  key=ed25519.Ed25519PrivateKey.generate();raw=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
  pub={'kty':'OKP','crv':'Ed25519','x':base64.urlsafe_b64encode(raw).decode().rstrip('=')}
  supporting=sorted(n for n in files if n.startswith('files/'))
  if profile in ['single-large-file','large-readme']:
   for n in supporting:files[n]=b''
  fill='README.md' if profile=='large-readme' else supporting[-1]
  def pack():
   manifest=seed.enc({'schema':'sci-publication/0.1','public_key':pub,'files':{n:{'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b)} for n,b in sorted(files.items())}})
   if profile=='heavy':manifest+=b' '*(1_048_576-len(manifest))
   sig=key.sign(manifest);buf=io.BytesIO()
   with zipfile.ZipFile(buf,'w',compression=zipfile.ZIP_STORED,allowZip64=False) as z:
    for name,data in [('manifest.json',manifest),('manifest.json.sig',sig),*sorted(files.items())]:
     info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));info.create_system=0;info.create_version=info.extract_version=10;info.external_attr=0x20;z.writestr(info,data)
   blob=bytearray(buf.getvalue());pos=int.from_bytes(blob[-6:-2],'little')
   while blob[pos:pos+4]==b'PK\1\2':
    blob[pos+38:pos+42]=b'\0'*4;pos+=46+int.from_bytes(blob[pos+28:pos+30],'little')
   return blob,manifest
  for _ in range(5):
   blob,manifest=pack();delta=TARGET-len(blob)
   if delta==0:break
   n=len(files[fill])+delta;assert n>=0
   files[fill]=b'a'*n
  assert len(blob)==TARGET and len(files)==1000
  with zipfile.ZipFile(io.BytesIO(blob)) as z:assert z.testzip() is None
  (out/f'{profile}.zip').write_bytes(blob)
  cases.append({'name':profile,'id':hashlib.sha256(manifest).hexdigest(),'zip_bytes':len(blob),'payload_bytes':sum(map(len,files.values())),'manifest_bytes':len(manifest),'metadata_bytes':len(files['sci.json']),'readme_bytes':len(files['README.md']),'largest_file_bytes':max(map(len,files.values())),'files':1000,'file_limit':1000,'expected_error':None,'trust':'normal'})
 (out/'cases.json').write_bytes(seed.enc(cases));shutil.rmtree(seed_dir)
 return cases
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('output',type=Path);a=p.parse_args();print(json.dumps(generate(a.output),indent=2))
