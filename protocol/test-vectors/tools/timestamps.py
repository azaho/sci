"""Offline RFC 3161 fixtures with synthetic, explicitly public test-only PKI."""
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from asn1crypto import cms, tsp, x509 as ax509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

ROOT=Path(__file__).resolve().parents[2]
UTC=timezone.utc
WHEN=datetime(2026,11,7,12,0,tzinfo=UTC)
BEGIN=datetime(2026,1,1,tzinfo=UTC)
END=datetime(2036,1,1,tzinfo=UTC)


def key(name):
    return serialization.load_pem_private_key((ROOT/'test-vectors/test-keys'/f'{name}.pem').read_bytes(),None)


def generate_pki(out):
    rootkey=key('root')
    rootname=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'SCI TEST ONLY Root - Never Trust In Production')])
    root=(x509.CertificateBuilder().subject_name(rootname).issuer_name(rootname).public_key(rootkey.public_key())
          .serial_number(1).not_valid_before(BEGIN).not_valid_after(END)
          .add_extension(x509.BasicConstraints(ca=True,path_length=0),True)
          .add_extension(x509.KeyUsage(False,False,False,False,False,True,True,False,False),True)
          .sign(rootkey,hashes.SHA256()))
    certs={}
    for i,name in enumerate(('tsa-a','tsa-b'),2):
        subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'SCI TEST ONLY '+name)])
        certs[name]=(x509.CertificateBuilder().subject_name(subject).issuer_name(rootname).public_key(key(name).public_key())
                     .serial_number(i).not_valid_before(BEGIN).not_valid_after(END)
                     .add_extension(x509.BasicConstraints(ca=False,path_length=None),True)
                     .add_extension(x509.KeyUsage(True,False,False,False,False,False,False,False,False),True)
                     .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]),True)
                     .sign(rootkey,hashes.SHA256()))
    crl=(x509.CertificateRevocationListBuilder().issuer_name(rootname).last_update(datetime(2026,11,1,tzinfo=UTC))
         .next_update(datetime(2026,12,1,tzinfo=UTC)).sign(rootkey,hashes.SHA256()))
    revoked=(x509.RevokedCertificateBuilder().serial_number(2).revocation_date(datetime(2026,11,6,tzinfo=UTC))
             .add_extension(x509.CRLReason(x509.ReasonFlags.key_compromise),False).build())
    badcrl=(x509.CertificateRevocationListBuilder().issuer_name(rootname).last_update(datetime(2026,11,7,tzinfo=UTC))
            .next_update(datetime(2026,12,1,tzinfo=UTC)).add_revoked_certificate(revoked).sign(rootkey,hashes.SHA256()))
    folder=out/'pki';folder.mkdir(exist_ok=True)
    for name,cert in {'root':root,**certs}.items():
        (folder/f'{name}.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder/'good.crl.pem').write_bytes(crl.public_bytes(serialization.Encoding.PEM))
    (folder/'revoked-a.crl.pem').write_bytes(badcrl.public_bytes(serialization.Encoding.PEM))
    return root,certs


def response(cert,signer,signature,serial=1,imprint=None,imprint_algorithm='sha256',status='granted',bad_signature=False,bad_cert_hash=False):
    rawcert=cert.public_bytes(serialization.Encoding.DER)
    certasn=ax509.Certificate.load(rawcert)
    info=tsp.TSTInfo({'version':'v1','policy':'1.3.6.1.4.1.55555.1',
                     'message_imprint':{'hash_algorithm':{'algorithm':imprint_algorithm},
                                        'hashed_message':hashlib.sha256(signature).digest() if imprint is None else imprint},
                     'serial_number':serial,'gen_time':WHEN,'accuracy':{'seconds':1},'ordering':False})
    attrs=cms.CMSAttributes([
        {'type':'content_type','values':['tst_info']},
        {'type':'message_digest','values':[hashlib.sha256(info.dump()).digest()]},
        {'type':'signing_certificate_v2','values':[{'certs':[{'cert_hash':b'\0'*32 if bad_cert_hash else hashlib.sha256(rawcert).digest()}]}]},
    ])
    signed=signer.sign(attrs.dump(),padding.PKCS1v15(),hashes.SHA256())
    if bad_signature:
        signed=bytes([signed[0]^1])+signed[1:]
    si=cms.SignerInfo({'version':'v1','sid':{'issuer_and_serial_number':{'issuer':certasn.issuer,'serial_number':certasn.serial_number}},
                       'digest_algorithm':{'algorithm':'sha256'},'signed_attrs':attrs,
                       'signature_algorithm':{'algorithm':'rsassa_pkcs1v15'},'signature':signed})
    token=cms.ContentInfo({'content_type':'signed_data','content':{
        'version':'v3','digest_algorithms':[{'algorithm':'sha256'}],
        'encap_content_info':{'content_type':'tst_info','content':info},
        'certificates':[certasn],'signer_infos':[si]}})
    return tsp.TimeStampResp({'status':{'status':status},'time_stamp_token':token}).dump()


def generate_timestamps(out,emit,bundle,publisher,jwk):
    _,certs=generate_pki(out)
    _,manifest,signature=bundle()
    a=response(certs['tsa-a'],key('tsa-a'),signature,1)
    b=response(certs['tsa-b'],key('tsa-b'),signature,2)
    wrong=response(certs['tsa-a'],key('tsa-a'),signature,3,imprint=b'\x11'*32)
    broken=response(certs['tsa-a'],key('tsa-a'),signature,4,bad_signature=True)
    wrong_alg=response(certs['tsa-a'],key('tsa-a'),signature,5,imprint=hashlib.sha512(signature).digest(),imprint_algorithm='sha512')
    sha1=response(certs['tsa-a'],key('tsa-a'),signature,9,imprint=hashlib.sha1(signature).digest(),imprint_algorithm='sha1')
    ess=response(certs['tsa-a'],key('tsa-a'),signature,6,bad_cert_hash=True)
    rejected=b'\x30\x05\x30\x03\x02\x01\x02'  # DER TimeStampResp: rejection, no token.
    granted_no_token=b'\x30\x05\x30\x03\x02\x01\x00'
    empty_sequence=b'\x30\x00'
    granted_mod=response(certs['tsa-a'],key('tsa-a'),signature,7,status='granted_with_mods')
    context={'publication_id':'sha256:'+hashlib.sha256(manifest).hexdigest(),'verification_time':WHEN.isoformat().replace('+00:00','Z'),
             'trust_anchors':['pki/root.pem'],'crls':['pki/good.crl.pem'],'revocation_policy':'require_current_crl',
             'expected_imprint_sha256':hashlib.sha256(signature).hexdigest()}
    def case(name,parts,results,tail=b'',reason='',extra=None,record=None):
        offset=0;proofs=[]
        for data,result in zip(parts,results,strict=True):
            proof={'offset':offset,'bytes':len(data),'result':result}
            if result=='verified':
                proof.update(gen_time='2026-11-07T12:00:00Z',accuracy_seconds=1)
            proofs.append(proof);offset+=len(data)
        framed=not tail and bool(parts)
        all_good=framed and all(x=='verified' for x in results)
        any_good='verified' in results
        expected={'outcome':'valid' if all_good else ('partial' if any_good else ('untrusted' if framed and all(x=='untrusted' for x in results) else 'invalid')),
                  'framing':'complete' if framed else 'malformed','proofs':proofs,
                  'stop_offset':offset,'publication_outcome':'valid','publication_id_unchanged':True}
        artifacts={'proofs.tsr':b''.join(parts)+tail,'manifest.json':manifest,'signature.bin':signature}
        ctx={**context,**(extra or {})}
        if record:
            artifacts={'proofs.tsr':b''.join(parts)+tail,'revocation.json':record[0],'signature.bin':record[1]}
            ctx.pop('publication_id',None)
            ctx['record_type']='revocation'
            ctx['expected_imprint_sha256']=hashlib.sha256(record[1]).hexdigest()
            expected.pop('publication_id_unchanged')
            expected.pop('publication_outcome')
            expected['revocation_outcome']='valid'
        return emit(name,'timestamp','6.8,8.1.3',reason,artifacts,expected,ctx)
    case('timestamp-one',[a],['verified'],reason='One real RSA-signed RFC 3161 proof and offline certificate/CRL trust data.')
    case('timestamp-two-authorities',[a,b],['verified','verified'],reason='Two proofs are concatenated without separators; both remain independently verifiable.')
    case('timestamp-duplicate',[a,a],['verified','verified'],reason='Duplicate proofs are redundant but not prohibited by the file format.')
    case('timestamp-granted-with-mods',[granted_mod],['verified'],reason='grantedWithMods is an accepted status.')
    case('timestamp-wrong-imprint',[wrong],['invalid'],reason='Valid TSA signature, wrong publication-signature hash.')
    case('timestamp-sha512-imprint',[wrong_alg],['invalid'],reason='SCI requires SHA-256 for the imprint, even when another RFC 3161 algorithm is valid.')
    case('timestamp-sha1-imprint',[sha1],['invalid'],reason='SHA-1 is an RFC 3161 imprint algorithm, but SCI requires SHA-256.')
    case('timestamp-bad-signature',[broken],['invalid'],reason='Correct framing and imprint, invalid CMS signature.')
    case('timestamp-bad-certificate-binding',[ess],['invalid'],reason='ESSCertIDv2 must identify the actual signing certificate.')
    case('timestamp-rejected-status',[rejected],['invalid'],reason='A complete rejection response is not a timestamp proof.')
    case('timestamp-granted-missing-token',[granted_no_token],['invalid'],reason='Granted status without a token is not a proof.')
    case('timestamp-invalid-middle',[a,broken,b],['verified','invalid','verified'],reason='A complete invalid proof does not prevent checking the next value.')
    case('timestamp-failed-then-valid',[wrong,a],['invalid','verified'],reason='A failed first proof must not prevent verification of the next complete value.')
    case('timestamp-nonresponse-middle',[a,empty_sequence,b],['verified','invalid','verified'],reason='Complete DER SEQUENCE with wrong schema is skipped by its length.')
    case('timestamp-nonsequence-middle',[a,b'\x02\x01\x01',b],['verified','invalid','verified'],reason='Complete DER INTEGER is not TimeStampResp but does not break framing.')
    case('timestamp-untrusted',[a],['untrusted'],reason='A valid token is not trusted without an accepted trust anchor.',extra={'trust_anchors':[]})
    case('timestamp-revoked-tsa',[a,b],['invalid','verified'],reason='Synthetic CRL revokes TSA A for compromise before genTime; TSA B remains usable.',extra={'crls':['pki/revoked-a.crl.pem']})
    for name,tail in [('truncated-tag',b'\x30'),('truncated-length',b'\x30\x82\x01'),('truncated-value',b'\x30\x82\x10\x00abc'),
                      ('indefinite-length',b'\x30\x80'),('nonminimal-length',b'\x30\x81\x00'),('huge-length',b'\x30\x88'+b'\xff'*8)]:
        case('timestamp-'+name,[a],['verified'],tail=tail,reason='Stop at malformed/incomplete DER; earlier verified proof remains usable.')
    case('timestamp-no-scan-ahead',[a],['verified'],tail=b'\x30\x84\x7f\xff\xff\xff'+b,reason='A later valid proof hidden after an incomplete declared value must not be scanned for.')
    case('timestamp-newline-not-separator',[a],['verified'],tail=b'\n',reason='A trailing newline is incomplete DER, not a separator.')
    case('timestamp-trailing-zero',[a],['verified'],tail=b'\0',reason='A trailing zero byte is incomplete DER, not padding.')
    case('timestamp-empty',[],[],reason='An empty timestamp file has no values; use HTTP 404 for no retained proofs.')
    case('timestamp-malformed-first',[],[],tail=b'\x30\x84\x7f\xff\xff\xff'+a,reason='Do not recover by scanning for a valid proof after a malformed first value.')
    import json
    rev=json.dumps({'schema':'sci-key-revocation/0.1','public_key':jwk},separators=(',',':')).encode()
    revsig=publisher.sign(rev)
    revproof=response(certs['tsa-a'],key('tsa-a'),revsig,8)
    case('timestamp-revocation',[revproof],['verified'],reason='Revocation timestamps bind the revocation signature, not the manifest signature.',record=(rev,revsig))
    # A real lookup body shared with the one-proof case; verdict is never supplied by the server.
    emit('timestamp-lookup-200','http','8.1.3','Timestamp lookup returns raw concatenated DER bytes, not JSON.',
         {'response.bin':a+b},{'proofs':2,'readers_must_verify':True},
         {'status':200,'content_type':'application/octet-stream','publication_id':context['publication_id'],
          'timestamp_case':'timestamp-two-authorities'})
