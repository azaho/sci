import * as asn1js from 'npm:asn1js@3.0.6';
// @deno-types="npm:pkijs@3.3.3"
import * as pkijs from './vendor/pkijs.js';
import { requireThat } from './metadata.ts';
export type TimestampTrust = { roots: string[]; crls: string[]; now?: string };
const oidTst='1.2.840.113549.1.9.16.1.4', oidSha256='2.16.840.1.101.3.4.2.1';
const equal=(a: Uint8Array,b: Uint8Array)=>a.length===b.length && a.every((v,i)=>v===b[i]);
const from64=(s: string)=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
// Bound and check DER before allowing the ASN.1 library to build an object tree.
export function der(bytes: Uint8Array): any {
  requireThat(bytes.length<=262144,'timestamp_limit'); let nodes=0;
  const visit=(at:number,end:number,depth:number):number=>{
    requireThat(++nodes<=10000 && depth<=24 && at+2<=end,'timestamp_der');
    const tag=bytes[at++]; requireThat((tag&31)!==31,'timestamp_der');
    let len=bytes[at++];
    if (len&128) {
      const n=len&127; requireThat(n>0 && n<=3 && at+n<=end && bytes[at]!==0,'timestamp_der');
      len=0;for(let i=0;i<n;i++) len=len*256+bytes[at++];requireThat(len>=128,'timestamp_der');
    }
    const stop=at+len;requireThat(stop<=end,'timestamp_der');
    if (tag&32) {
      requireThat((tag&192)!==0 || tag===48 || tag===49,'timestamp_der');
      let prev:Uint8Array|undefined;
      while(at<stop) {
        const next=visit(at,stop,depth+1);const child=bytes.subarray(at,next);
        if(tag===49 && prev) {
          let cmp=0;for(let i=0;i<Math.min(prev.length,child.length);i++)if(prev[i]!==child[i]){cmp=prev[i]-child[i];break;}
          requireThat(cmp<0 || (cmp===0 && prev.length<=child.length),'timestamp_der');
        }
        prev=child;at=next;
      }
    } else if ((tag&192)===0) {
      requireThat(tag!==0 && tag!==16 && tag!==17,'timestamp_der');
      if(tag===1)requireThat(len===1 && (bytes[at]===0 || bytes[at]===255),'timestamp_der');
      if(tag===2 || tag===10)requireThat(len>0 && !(len>1 && ((bytes[at]===0 && bytes[at+1]<128)||(bytes[at]===255 && bytes[at+1]>=128))),'timestamp_der');
      if(tag===3)requireThat(len>0 && bytes[at]<=7 && (len>1 ? (bytes[stop-1]&((1<<bytes[at])-1))===0 : bytes[at]===0),'timestamp_der');
      if(tag===5)requireThat(len===0,'timestamp_der');
      if(tag===6){requireThat(len>0 && bytes[stop-1]<128,'timestamp_der'); for(let i=at;i<stop;i++)requireThat(!(bytes[i]===128 && (i===at || bytes[i-1]<128)),'timestamp_der');}
      if(tag===23 || tag===24) {
        const s=new TextDecoder().decode(bytes.subarray(at,stop));
        requireThat((tag===23 ? /^\d{12}Z$/ : /^\d{14}(?:\.\d*[1-9])?Z$/).test(s),'timestamp_der');
      }
    }
    return stop;
  };
  requireThat(visit(0,bytes.length,0)===bytes.length,'timestamp_der');
  const decoded=asn1js.fromBER(bytes);requireThat(decoded.offset===bytes.length && !decoded.result.error,'timestamp_der');return decoded.result;
}
function attr(attrs:any[],oid:string,required=true):any {
  const a=attrs.filter(a=>a.type===oid);requireThat(a.length<2 && (!required || a.length===1),'timestamp_attribute');
  if(!a.length)return undefined;requireThat(a[0].values.length===1,'timestamp_attribute');return a[0].values[0];
}
export async function timestamp(raw:Uint8Array,signature:Uint8Array,trust:TimestampTrust) {
  const response=new pkijs.TimeStampResp({schema:der(raw)});
  requireThat([0,1].includes(response.status.status) && response.timeStampToken?.contentType===pkijs.ContentInfo.SIGNED_DATA,'timestamp_status');
  const cms=new pkijs.SignedData({schema:response.timeStampToken!.content});
  requireThat(cms.version===3 && cms.signerInfos.length===1 && (cms.certificates?.length||0)<=8 &&
    cms.encapContentInfo.eContentType===oidTst && cms.encapContentInfo.eContent,'timestamp_token');
  const tst=new pkijs.TSTInfo({schema:der(new Uint8Array(cms.encapContentInfo.eContent!.getValue()))});
  requireThat(tst.version===1 && tst.messageImprint.hashAlgorithm.algorithmId===oidSha256,'timestamp_imprint');
  const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',signature as BufferSource));
  requireThat(equal(digest,tst.messageImprint.hashedMessage.valueBlock.valueHexView),'timestamp_imprint');
  requireThat(tst.serialNumber.valueBlock.valueHexView.length>0 && tst.serialNumber.valueBlock.valueHexView[0]<128,'timestamp_serial');
  if(tst.accuracy) for(const name of ['millis','micros'] as const) if(tst.accuracy[name]!==undefined)requireThat(tst.accuracy[name]!>=1 && tst.accuracy[name]!<=999,'timestamp_accuracy');
  if(tst.accuracy?.seconds!==undefined)requireThat(tst.accuracy.seconds>=0,'timestamp_accuracy');
  requireThat(!tst.extensions?.some(e=>e.critical),'timestamp_extension');
  const now=trust.now ? new Date(trust.now) : new Date();
  requireThat(Number.isFinite(tst.genTime.getTime()) && tst.genTime.getTime()<=now.getTime()+300000,'timestamp_time');
  const signer=cms.signerInfos[0];
  requireThat(signer.signedAttrs && !signer.unsignedAttrs,'timestamp_attributes'); // Bounded local policy: no unsigned attributes.
  const attrs=signer.signedAttrs!.attributes;
  requireThat(attr(attrs,'1.2.840.113549.1.9.3').valueBlock.toString()===oidTst,'timestamp_content_type');
  attr(attrs,'1.2.840.113549.1.9.4');
  const certs=(cms.certificates||[]).filter(c=>c instanceof pkijs.Certificate) as pkijs.Certificate[];
  requireThat(signer.sid instanceof pkijs.IssuerAndSerialNumber,'timestamp_signer_policy');
  const sid=signer.sid as pkijs.IssuerAndSerialNumber;
  const matching=certs.filter(c=>c.issuer.isEqual(sid.issuer) && c.serialNumber.isEqual(sid.serialNumber));
  requireThat(matching.length===1,'timestamp_signer');const cert=matching[0];
  const eku=cert.extensions?.filter(e=>e.extnID==='2.5.29.37')||[];
  requireThat(eku.length===1 && eku[0].critical && eku[0].parsedValue.keyPurposes.length===1 && eku[0].parsedValue.keyPurposes[0]==='1.3.6.1.5.5.7.3.8','timestamp_eku');
  if(tst.tsa) {
    const names=cert.extensions?.find(e=>e.extnID==='2.5.29.17')?.parsedValue.altNames||[];
    requireThat((tst.tsa.type===4 && tst.tsa.value.isEqual(cert.subject)) || names.some((n:any)=>equal(new Uint8Array(n.toSchema().toBER()),new Uint8Array((tst.tsa!.toSchema() as any).toBER()))),'timestamp_tsa');
  }
  // PKI.js verifies CMS and chain, but ESS certificate binding needs an explicit check.
  const v1=attr(attrs,'1.2.840.113549.1.9.16.2.12',false),v2=attr(attrs,'1.2.840.113549.1.9.16.2.47',false);
  requireThat(v1 || v2,'timestamp_ess');
  for(const [value,isV2] of [[v1,false],[v2,true]] as const) {
    if(!value)continue;
    const sc=value.valueBlock.value;requireThat(sc?.length>=1 && sc.length<=2,'timestamp_ess');
    const ids=sc[0].valueBlock.value;requireThat(ids?.length===1,'timestamp_ess_policy');
    const parts=[...ids[0].valueBlock.value];let algorithm=isV2 ? 'SHA-256':'SHA-1';
    if(isV2 && parts[0] instanceof asn1js.Sequence) {
      const alg=new pkijs.AlgorithmIdentifier({schema:parts.shift()});
      const algorithms:Record<string,string>={'1.3.14.3.2.26':'SHA-1',[oidSha256]:'SHA-256','2.16.840.1.101.3.4.2.2':'SHA-384','2.16.840.1.101.3.4.2.3':'SHA-512'};
      algorithm=algorithms[alg.algorithmId];requireThat(algorithm,'timestamp_ess_algorithm');
    }
    requireThat(parts.length>=1 && parts.length<=2 && parts[0] instanceof asn1js.OctetString,'timestamp_ess');
    const certHash=new Uint8Array(await crypto.subtle.digest(algorithm,cert.toSchema().toBER() as ArrayBuffer));
    requireThat(equal(certHash,parts[0].valueBlock.valueHexView),'timestamp_ess');
    if(parts[1]) {
      const issuerSerial=parts[1].valueBlock.value;
      requireThat(issuerSerial?.length===2 && cert.serialNumber.isEqual(issuerSerial[1]),'timestamp_ess');
      const names=new pkijs.GeneralNames({schema:issuerSerial[0]});
      requireThat(names.names.some(n=>n.type===4 && n.value.isEqual(cert.issuer)),'timestamp_ess');
    }
  }
  requireThat(trust.roots.length>0,'timestamp_trust');
  const roots=trust.roots.map(s=>new pkijs.Certificate({schema:der(from64(s))}));
  const crls=trust.crls.map(s=>new pkijs.CertificateRevocationList({schema:der(from64(s))}));
  cms.crls=crls;
  let checked:any;
  try { checked=await cms.verify({signer:0,data:signature.slice().buffer as ArrayBuffer,trustedCerts:roots,checkChain:true,passedWhenNotRevValues:false,extendedMode:true}); }
  catch {throw new Error('timestamp_chain_or_signature');}
  requireThat(checked.signatureVerified && checked.signerCertificateVerified,'timestamp_chain_or_signature');
  // Require current, signed revocation evidence for each non-anchor certificate.
  for(const c of checked.certificatePath as pkijs.Certificate[]) {
    if(roots.some(r=>equal(new Uint8Array(r.toSchema().toBER()),new Uint8Array(c.toSchema().toBER()))))continue;
    const issuer=(checked.certificatePath as pkijs.Certificate[]).find(i=>c.issuer.isEqual(i.subject));
    requireThat(issuer,'timestamp_chain');
    let good=false;
    for(const crl of crls) {
      if(!crl.issuer.isEqual(c.issuer))continue;
      if(crl.thisUpdate.value>now || !crl.nextUpdate || crl.nextUpdate.value<=now)continue;
      if(!(await crl.verify({issuerCertificate:issuer})))continue;
      requireThat(!crl.revokedCertificates?.some(r=>r.userCertificate.isEqual(c.serialNumber)),'timestamp_revoked');good=true;
    }
    requireThat(good,'timestamp_revocation_unknown');
  }
  return {status:'verified',genTime:tst.genTime.toISOString(),accuracy:tst.accuracy?.toJSON()||null,
    authority:cert.subject.toJSON(),policy:tst.policy};
}
