/** X.509 certificate and CRL parsing (RFC 5280) and signature verification via WebCrypto. */
import { ed25519 } from '@noble/curves/ed25519.js';
import { base64DecodeStandard, equalBytes } from './bytes.ts';
import {
  type AlgorithmId,
  algorithmId,
  bitStringBits,
  bitStringBytes,
  boolean,
  children,
  type DerTime,
  integerBytes,
  isContext,
  isUniversal,
  octetString,
  oid,
  parseDer,
  sequence,
  smallInt,
  type Tlv,
  time,
  UNIVERSAL,
} from './der.ts';
import { fail } from './errors.ts';

export const OID = {
  rsaEncryption: '1.2.840.113549.1.1.1',
  sha256WithRSA: '1.2.840.113549.1.1.11',
  sha384WithRSA: '1.2.840.113549.1.1.12',
  sha512WithRSA: '1.2.840.113549.1.1.13',
  rsassaPss: '1.2.840.113549.1.1.10',
  mgf1: '1.2.840.113549.1.1.8',
  ecPublicKey: '1.2.840.10045.2.1',
  ecdsaSha256: '1.2.840.10045.4.3.2',
  ecdsaSha384: '1.2.840.10045.4.3.3',
  ecdsaSha512: '1.2.840.10045.4.3.4',
  p256: '1.2.840.10045.3.1.7',
  p384: '1.3.132.0.34',
  p521: '1.3.132.0.35',
  ed25519: '1.3.101.112',
  sha1: '1.3.14.3.2.26',
  sha256: '2.16.840.1.101.3.4.2.1',
  sha384: '2.16.840.1.101.3.4.2.2',
  sha512: '2.16.840.1.101.3.4.2.3',
  basicConstraints: '2.5.29.19',
  keyUsage: '2.5.29.15',
  extKeyUsage: '2.5.29.37',
  subjectKeyIdentifier: '2.5.29.14',
  authorityKeyIdentifier: '2.5.29.35',
  subjectAltName: '2.5.29.17',
  crlDistributionPoints: '2.5.29.31',
  authorityInfoAccess: '1.3.6.1.5.5.7.1.1',
  crlNumber: '2.5.29.20',
  crlReason: '2.5.29.21',
  timeStamping: '1.3.6.1.5.5.7.3.8',
} as const;

export type HashName = 'SHA-256' | 'SHA-384' | 'SHA-512';

export function hashNameOf(o: string): HashName | undefined {
  return o === OID.sha256 ? 'SHA-256' : o === OID.sha384 ? 'SHA-384' : o === OID.sha512 ? 'SHA-512' : undefined;
}

export interface Extension {
  readonly critical: boolean;
  readonly value: Uint8Array;
}

export interface Certificate {
  readonly der: Uint8Array;
  readonly tbs: Uint8Array;
  readonly version: number;
  /** Serial number INTEGER content octets. */
  readonly serial: Uint8Array;
  readonly tbsSignatureAlgorithm: AlgorithmId;
  readonly signatureAlgorithm: AlgorithmId;
  readonly signature: Uint8Array;
  /** DER of the issuer Name. */
  readonly issuer: Uint8Array;
  /** DER of the subject Name. */
  readonly subject: Uint8Array;
  readonly notBefore: DerTime;
  readonly notAfter: DerTime;
  /** DER of SubjectPublicKeyInfo. */
  readonly spki: Uint8Array;
  readonly publicKeyAlgorithm: AlgorithmId;
  readonly publicKey: Uint8Array;
  readonly extensions: ReadonlyMap<string, Extension>;
}

function parseExtensions(t: Tlv): Map<string, Extension> {
  const out = new Map<string, Extension>();
  for (const ext of sequence(t, 'Extensions')) {
    const parts = sequence(ext, 'Extension');
    const id = oid(parts[0]);
    let critical = false;
    let i = 1;
    if (isUniversal(parts[1], UNIVERSAL.BOOLEAN)) {
      critical = boolean(parts[1]);
      if (!critical) fail('x509.der', 'X.690 DER', 'DEFAULT FALSE must be omitted');
      i = 2;
    }
    const value = octetString(parts[i], 'extnValue');
    if (parts.length !== i + 1) fail('x509.extension', 'RFC 5280', 'Malformed extension');
    if (out.has(id)) fail('x509.extension', 'RFC 5280', 'Duplicate extension');
    out.set(id, { critical, value });
  }
  return out;
}

export function parseCertificate(der: Uint8Array): Certificate {
  const top = sequence(parseDer(der), 'Certificate');
  if (top.length !== 3) fail('x509.structure', 'RFC 5280', 'Certificate must have three parts');
  const [tbsT, sigAlgT, sigT] = top as [Tlv, Tlv, Tlv];
  const tbs = sequence(tbsT, 'TBSCertificate');
  let i = 0;
  let version = 1;
  if (isContext(tbs[0], 0)) {
    version = smallInt(children(tbs[0])[0], 'version') + 1;
    i = 1;
  }
  const serial = integerBytes(tbs[i++], 'serialNumber');
  const tbsSignatureAlgorithm = algorithmId(tbs[i++]);
  const issuerT = tbs[i++];
  const validity = sequence(tbs[i++], 'Validity');
  const subjectT = tbs[i++];
  const spkiT = tbs[i++];
  if (!isUniversal(issuerT, UNIVERSAL.SEQUENCE) || !isUniversal(subjectT, UNIVERSAL.SEQUENCE) || !spkiT) {
    fail('x509.structure', 'RFC 5280', 'Malformed TBSCertificate');
  }
  const spkiParts = sequence(spkiT, 'SubjectPublicKeyInfo');
  const publicKeyAlgorithm = algorithmId(spkiParts[0]);
  const publicKey = bitStringBytes(spkiParts[1], 'subjectPublicKey');
  let extensions = new Map<string, Extension>();
  for (; i < tbs.length; i++) {
    const t = tbs[i];
    if (isContext(t, 1) || isContext(t, 2)) continue; // unique identifiers
    if (isContext(t, 3)) extensions = parseExtensions(children(t)[0] as Tlv);
    else fail('x509.structure', 'RFC 5280', 'Unexpected TBSCertificate field');
  }
  return {
    der,
    tbs: tbsT.bytes,
    version,
    serial,
    tbsSignatureAlgorithm,
    signatureAlgorithm: algorithmId(sigAlgT),
    signature: bitStringBytes(sigT, 'signatureValue'),
    issuer: issuerT.bytes,
    subject: subjectT.bytes,
    notBefore: time(validity[0], 'notBefore'),
    notAfter: time(validity[1], 'notAfter'),
    spki: spkiT.bytes,
    publicKeyAlgorithm,
    publicKey,
    extensions,
  };
}

export interface BasicConstraints {
  readonly ca: boolean;
  readonly pathLen?: number;
}

export function basicConstraints(cert: Certificate): BasicConstraints | undefined {
  const e = cert.extensions.get(OID.basicConstraints);
  if (!e) return undefined;
  const parts = sequence(parseDer(e.value), 'BasicConstraints');
  let ca = false;
  let i = 0;
  if (isUniversal(parts[0], UNIVERSAL.BOOLEAN)) {
    ca = boolean(parts[0]);
    i = 1;
  }
  return parts[i] ? { ca, pathLen: smallInt(parts[i], 'pathLenConstraint') } : { ca };
}

/** keyUsage bits (0 digitalSignature, 1 nonRepudiation, 5 keyCertSign, 6 cRLSign), or undefined if absent. */
export function keyUsage(cert: Certificate): Set<number> | undefined {
  const e = cert.extensions.get(OID.keyUsage);
  return e ? bitStringBits(parseDer(e.value)) : undefined;
}

export function extKeyUsage(cert: Certificate): { readonly critical: boolean; readonly purposes: readonly string[] } | undefined {
  const e = cert.extensions.get(OID.extKeyUsage);
  if (!e) return undefined;
  return { critical: e.critical, purposes: sequence(parseDer(e.value), 'ExtKeyUsage').map((t) => oid(t)) };
}

export function subjectKeyIdentifier(cert: Certificate): Uint8Array | undefined {
  const e = cert.extensions.get(OID.subjectKeyIdentifier);
  return e ? octetString(parseDer(e.value)) : undefined;
}

/** Extensions this implementation processes; any other critical extension makes a certificate unusable. */
export const PROCESSED_CERT_EXTENSIONS: ReadonlySet<string> = new Set([
  OID.basicConstraints,
  OID.keyUsage,
  OID.extKeyUsage,
  OID.subjectKeyIdentifier,
  OID.authorityKeyIdentifier,
  OID.subjectAltName,
]);

// ---------------------------------------------------------------------------------------------
// CRLs

export interface RevokedEntry {
  readonly serial: Uint8Array;
  readonly revocationDate: DerTime;
}

export interface Crl {
  readonly der: Uint8Array;
  readonly tbs: Uint8Array;
  readonly tbsSignatureAlgorithm: AlgorithmId;
  readonly signatureAlgorithm: AlgorithmId;
  readonly signature: Uint8Array;
  readonly issuer: Uint8Array;
  readonly thisUpdate: DerTime;
  readonly nextUpdate?: DerTime;
  readonly revoked: readonly RevokedEntry[];
  readonly extensions: ReadonlyMap<string, Extension>;
  /** True if any CRL or entry extension is critical and unprocessed. */
  readonly hasUnprocessedCritical: boolean;
}

export function parseCrl(der: Uint8Array): Crl {
  const top = sequence(parseDer(der), 'CertificateList');
  if (top.length !== 3) fail('x509.crl', 'RFC 5280', 'CRL must have three parts');
  const [tbsT, sigAlgT, sigT] = top as [Tlv, Tlv, Tlv];
  const tbs = sequence(tbsT, 'TBSCertList');
  let i = 0;
  if (isUniversal(tbs[0], UNIVERSAL.INTEGER)) {
    const v = smallInt(tbs[0], 'version');
    if (v !== 1) fail('x509.crl', 'RFC 5280', 'Unsupported CRL version');
    i = 1;
  }
  const tbsSignatureAlgorithm = algorithmId(tbs[i++]);
  const issuerT = tbs[i++];
  if (!isUniversal(issuerT, UNIVERSAL.SEQUENCE)) fail('x509.crl', 'RFC 5280', 'Malformed CRL issuer');
  const thisUpdate = time(tbs[i++], 'thisUpdate');
  let nextUpdate: DerTime | undefined;
  const t = tbs[i];
  if (t && t.cls === 0 && (t.tag === UNIVERSAL.UTCTime || t.tag === UNIVERSAL.GeneralizedTime)) {
    nextUpdate = time(t, 'nextUpdate');
    i++;
  }
  const revoked: RevokedEntry[] = [];
  let unprocessed = false;
  if (isUniversal(tbs[i], UNIVERSAL.SEQUENCE)) {
    for (const entry of sequence(tbs[i], 'revokedCertificates')) {
      const parts = sequence(entry, 'RevokedCertificate');
      revoked.push({ serial: integerBytes(parts[0], 'userCertificate'), revocationDate: time(parts[1], 'revocationDate') });
      if (parts[2]) {
        for (const [id, ext] of parseExtensions(parts[2])) if (ext.critical && id !== OID.crlReason) unprocessed = true;
      }
    }
    i++;
  }
  let extensions = new Map<string, Extension>();
  if (isContext(tbs[i], 0)) {
    extensions = parseExtensions(children(tbs[i] as Tlv)[0] as Tlv);
    i++;
  }
  if (i !== tbs.length) fail('x509.crl', 'RFC 5280', 'Unexpected TBSCertList field');
  for (const [id, ext] of extensions) if (ext.critical && id !== OID.crlNumber && id !== OID.authorityKeyIdentifier) unprocessed = true;
  return {
    der,
    tbs: tbsT.bytes,
    tbsSignatureAlgorithm,
    signatureAlgorithm: algorithmId(sigAlgT),
    signature: bitStringBytes(sigT, 'signatureValue'),
    issuer: issuerT.bytes,
    thisUpdate,
    ...(nextUpdate ? { nextUpdate } : {}),
    revoked,
    extensions,
    hasUnprocessedCritical: unprocessed,
  };
}

// ---------------------------------------------------------------------------------------------
// PEM

/** Decode every PEM block with the given label (e.g. `CERTIFICATE`, `X509 CRL`). */
export function pemBlocks(text: string, label: string): Uint8Array[] {
  const re = new RegExp(`-----BEGIN ${label}-----([\\s\\S]*?)-----END ${label}-----`, 'g');
  const out: Uint8Array[] = [];
  for (const m of text.matchAll(re)) out.push(base64DecodeStandard(m[1] ?? ''));
  return out;
}

/** Accept DER bytes or PEM text (as bytes or string) and return DER blocks. */
export function toDerList(input: Uint8Array | string, label: string): Uint8Array[] {
  if (typeof input === 'string') return pemBlocks(input, label);
  if (input[0] === 0x30) return [input];
  return pemBlocks(new TextDecoder().decode(input), label);
}

// ---------------------------------------------------------------------------------------------
// Signature verification

function subtle(): SubtleCrypto {
  const s = globalThis.crypto?.subtle;
  if (!s) throw new Error('WebCrypto subtle API is unavailable');
  return s;
}

function buf(b: Uint8Array): ArrayBuffer {
  return b.slice().buffer as ArrayBuffer;
}

/** Convert a DER ECDSA-Sig-Value to the raw r||s form WebCrypto expects. */
function ecdsaDerToRaw(sig: Uint8Array, size: number): Uint8Array | null {
  try {
    const [r, s] = sequence(parseDer(sig), 'ECDSA-Sig-Value').map((t) => integerBytes(t));
    if (!r || !s) return null;
    const out = new Uint8Array(size * 2);
    const put = (v: Uint8Array, o: number) => {
      const trimmed = v[0] === 0 ? v.subarray(1) : v;
      if (trimmed.length > size) throw new RangeError('ECDSA integer too large');
      out.set(trimmed, o + size - trimmed.length);
    };
    put(r, 0);
    put(s, size);
    return out;
  } catch {
    return null;
  }
}

function pssParams(params: Tlv | undefined): { hash: HashName; saltLength: number } | null {
  let hash: HashName | undefined;
  let mgfHash: HashName | undefined;
  let saltLength = 20;
  if (!params) return null;
  for (const p of sequence(params, 'RSASSA-PSS-params')) {
    const inner = children(p)[0];
    if (isContext(p, 0)) hash = hashNameOf(algorithmId(inner).oid);
    else if (isContext(p, 1)) {
      const mgf = algorithmId(inner);
      if (mgf.oid !== OID.mgf1) return null;
      mgfHash = hashNameOf(algorithmId(mgf.params).oid);
    } else if (isContext(p, 2)) saltLength = smallInt(inner, 'saltLength');
    else if (isContext(p, 3)) {
      if (smallInt(inner, 'trailerField') !== 1) return null;
    }
  }
  // RFC 4055 defaults (SHA-1) are rejected as weak; both hashes must be explicit and equal.
  if (!hash || hash !== mgfHash) return null;
  return { hash, saltLength };
}

/**
 * Verify `signature` over `data` using a SubjectPublicKeyInfo and a signature algorithm.
 * For CMS `rsaEncryption` signer infos, pass the digest algorithm's hash as `digestHash`.
 * SHA-1-based signatures are rejected. Returns false on any failure.
 */
export async function verifySignature(
  spki: Uint8Array,
  keyAlgorithm: AlgorithmId,
  sigAlgorithm: AlgorithmId,
  data: Uint8Array,
  signature: Uint8Array,
  digestHash?: HashName,
): Promise<boolean> {
  try {
    const s = sigAlgorithm.oid;
    if (s === OID.ed25519) {
      if (keyAlgorithm.oid !== OID.ed25519) return false;
      const keyBits = bitStringBytes(sequence(parseDer(spki), 'SPKI')[1]);
      return ed25519.verify(signature, data, keyBits, { zip215: false });
    }
    let rsaHash: HashName | undefined;
    if (s === OID.sha256WithRSA) rsaHash = 'SHA-256';
    else if (s === OID.sha384WithRSA) rsaHash = 'SHA-384';
    else if (s === OID.sha512WithRSA) rsaHash = 'SHA-512';
    else if (s === OID.rsaEncryption) rsaHash = digestHash;
    if (rsaHash) {
      if (keyAlgorithm.oid !== OID.rsaEncryption) return false;
      const key = await subtle().importKey('spki', buf(spki), { name: 'RSASSA-PKCS1-v1_5', hash: rsaHash }, false, ['verify']);
      return await subtle().verify('RSASSA-PKCS1-v1_5', key, buf(signature), buf(data));
    }
    if (s === OID.rsassaPss) {
      const p = pssParams(sigAlgorithm.params);
      if (!p || (keyAlgorithm.oid !== OID.rsaEncryption && keyAlgorithm.oid !== OID.rsassaPss)) return false;
      const key = await subtle().importKey('spki', buf(spki), { name: 'RSA-PSS', hash: p.hash }, false, ['verify']);
      return await subtle().verify({ name: 'RSA-PSS', saltLength: p.saltLength }, key, buf(signature), buf(data));
    }
    let ecHash: HashName | undefined;
    if (s === OID.ecdsaSha256) ecHash = 'SHA-256';
    else if (s === OID.ecdsaSha384) ecHash = 'SHA-384';
    else if (s === OID.ecdsaSha512) ecHash = 'SHA-512';
    if (ecHash) {
      if (keyAlgorithm.oid !== OID.ecPublicKey || !keyAlgorithm.params) return false;
      const curveOid = oid(keyAlgorithm.params);
      const curve = curveOid === OID.p256 ? 'P-256' : curveOid === OID.p384 ? 'P-384' : curveOid === OID.p521 ? 'P-521' : undefined;
      if (!curve) return false;
      const size = curve === 'P-256' ? 32 : curve === 'P-384' ? 48 : 66;
      const raw = ecdsaDerToRaw(signature, size);
      if (!raw) return false;
      const key = await subtle().importKey('spki', buf(spki), { name: 'ECDSA', namedCurve: curve }, false, ['verify']);
      return await subtle().verify({ name: 'ECDSA', hash: ecHash }, key, buf(raw), buf(data));
    }
    return false;
  } catch {
    return false;
  }
}

/** Verify that `cert` (or a CRL's TBS) was signed by `issuer`. */
export function verifySignedBy(tbs: Uint8Array, sigAlg: AlgorithmId, signature: Uint8Array, issuer: Certificate): Promise<boolean> {
  return verifySignature(issuer.spki, issuer.publicKeyAlgorithm, sigAlg, tbs, signature);
}

export function sameAlgorithm(a: AlgorithmId, b: AlgorithmId): boolean {
  if (a.oid !== b.oid) return false;
  if (!a.params || !b.params) return !a.params && !b.params;
  return equalBytes(a.params.bytes, b.params.bytes);
}
