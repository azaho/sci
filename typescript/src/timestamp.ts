/**
 * SCI §6.8 timestamp verification: RFC 3161 (as updated by RFC 5816) TimeStampResp values,
 * concatenated in a timestamp file and framed by DER tag/length without scanning ahead.
 */
import { type Budget, resolveBudget } from './budget.ts';
import { equalBytes, toHex } from './bytes.ts';
import { verifyRevocation } from './claims.ts';
import {
  type AlgorithmId,
  algorithmId,
  boolean,
  children,
  frameDer,
  instantLE,
  instantLT,
  integerBytes,
  isContext,
  isUniversal,
  octetString,
  oid,
  parseDer,
  renderName,
  sequence,
  setOf,
  smallInt,
  type Tlv,
  time,
  UNIVERSAL,
} from './der.ts';
import { fail, SciError, type SciIssue, toIssue } from './errors.ts';
import { sha1, sha256, sha384, sha512 } from './hash.ts';
import { verifyManifest } from './manifest.ts';
import { type Instant, instantFromDate } from './time.ts';
import {
  basicConstraints,
  type Certificate,
  type Crl,
  extKeyUsage,
  type HashName,
  hashNameOf,
  keyUsage,
  OID,
  PROCESSED_CERT_EXTENSIONS,
  parseCertificate,
  parseCrl,
  sameAlgorithm,
  subjectKeyIdentifier,
  toDerList,
  verifySignature,
  verifySignedBy,
} from './x509.ts';

const TSP = {
  signedData: '1.2.840.113549.1.7.2',
  tstInfo: '1.2.840.113549.1.9.16.1.4',
  contentType: '1.2.840.113549.1.9.3',
  messageDigest: '1.2.840.113549.1.9.4',
  signingCertificate: '1.2.840.113549.1.9.16.2.12',
  signingCertificateV2: '1.2.840.113549.1.9.16.2.47',
} as const;

export type RevocationPolicy = 'require_current_crl' | 'crl_if_available' | 'none';

export interface TimestampTrustOptions {
  /** Trust anchor certificates (DER bytes, or PEM text/bytes). An empty list trusts nothing. */
  readonly trustAnchors: readonly (Uint8Array | string)[];
  /** Additional intermediate certificates (DER or PEM). */
  readonly intermediates?: readonly (Uint8Array | string)[];
  /** CRLs (DER or PEM). */
  readonly crls?: readonly (Uint8Array | string)[];
  /** Verification time (default: now). */
  readonly verificationTime?: Date;
  /** Revocation checking policy (default `require_current_crl`). */
  readonly revocationPolicy?: RevocationPolicy;
  /** Maximum chain length including the anchor (default 6). */
  readonly maxChainLength?: number;
  readonly budget?: Partial<Budget>;
}

export type ProofResult = 'verified' | 'invalid' | 'untrusted' | 'incomplete';

export interface TimestampProof {
  readonly offset: number;
  readonly bytes: number;
  readonly result: ProofResult;
  /** RFC 3339 rendering of `genTime` (verified proofs only). */
  readonly genTime?: string;
  /** Total stated accuracy in seconds (seconds + millis/1e3 + micros/1e6), if stated. */
  readonly accuracySeconds?: number;
  /** Timestamp authority: signer certificate subject and SHA-256, plus TSTInfo `tsa` if present. */
  readonly tsa?: { readonly subject: string; readonly certificateSha256: string; readonly name?: string };
  readonly serialNumber?: string;
  readonly policy?: string;
  readonly issue?: SciIssue;
}

export type TimestampOutcome = 'valid' | 'partial' | 'invalid' | 'untrusted' | 'incomplete';

export interface TimestampFileResult {
  /** `valid`: complete framing and every proof verified; `partial`: some proof verified. */
  readonly outcome: TimestampOutcome;
  readonly framing: 'complete' | 'malformed';
  readonly proofs: readonly TimestampProof[];
  /** Offset of the first malformed/incomplete tail, or the file length. */
  readonly stopOffset: number;
}

interface ParsedToken {
  readonly statusOk: boolean;
  readonly signedData: Tlv[];
  readonly tstInfoDer: Uint8Array;
  readonly tstInfo: TstInfo;
  readonly certificates: Certificate[];
  readonly signerInfo: Tlv[];
}

interface TstInfo {
  readonly policy: string;
  readonly imprintAlgorithm: AlgorithmId;
  readonly imprint: Uint8Array;
  readonly serial: Uint8Array;
  readonly genTime: { readonly instant: Instant; readonly text: string };
  readonly accuracySeconds?: number;
  readonly tsaName?: string;
}

const REQ = '§6.8, RFC 3161';

function parseTstInfo(der: Uint8Array): TstInfo {
  const parts = sequence(parseDer(der), 'TSTInfo');
  let i = 0;
  const version = smallInt(parts[i++], 'TSTInfo.version');
  if (version !== 1) fail('timestamp.tstinfo', REQ, 'TSTInfo version must be 1');
  const policy = oid(parts[i++], 'policy');
  const mi = sequence(parts[i++], 'MessageImprint');
  if (mi.length !== 2) fail('timestamp.tstinfo', REQ, 'Malformed MessageImprint');
  const imprintAlgorithm = algorithmId(mi[0]);
  const imprint = octetString(mi[1], 'hashedMessage');
  const serial = integerBytes(parts[i++], 'serialNumber');
  const genTime = time(parts[i++], 'genTime');
  if (!isUniversal(parts[i - 1], UNIVERSAL.GeneralizedTime)) fail('timestamp.tstinfo', REQ, 'genTime must be GeneralizedTime');
  let accuracySeconds: number | undefined;
  if (isUniversal(parts[i], UNIVERSAL.SEQUENCE)) {
    let seconds = 0;
    let millis = 0;
    let micros = 0;
    for (const a of sequence(parts[i], 'Accuracy')) {
      if (isUniversal(a, UNIVERSAL.INTEGER)) seconds = smallInt(a, 'seconds');
      else if (isContext(a, 0)) millis = smallInt(a, 'millis', true);
      else if (isContext(a, 1)) micros = smallInt(a, 'micros', true);
      else fail('timestamp.tstinfo', REQ, 'Malformed Accuracy');
    }
    if (millis > 999 || micros > 999) fail('timestamp.tstinfo', REQ, 'Accuracy millis/micros out of range');
    accuracySeconds = seconds + millis / 1000 + micros / 1_000_000;
    i++;
  }
  if (isUniversal(parts[i], UNIVERSAL.BOOLEAN)) {
    if (!boolean(parts[i])) fail('timestamp.der', 'X.690 DER', 'ordering DEFAULT FALSE must be omitted');
    i++;
  }
  if (isUniversal(parts[i], UNIVERSAL.INTEGER)) {
    integerBytes(parts[i], 'nonce');
    i++;
  }
  let tsaName: string | undefined;
  if (isContext(parts[i], 0)) {
    const gn = children(parts[i] as Tlv)[0];
    if (gn && isContext(gn, 4)) {
      const nameT = children(gn)[0];
      if (nameT) tsaName = renderName(nameT.bytes);
    } else if (gn && gn.cls === 2 && !gn.constructed) {
      tsaName = new TextDecoder().decode(gn.content);
    }
    i++;
  }
  if (isContext(parts[i], 1)) {
    for (const ext of children(parts[i] as Tlv)) {
      const e = sequence(ext, 'Extension');
      if (e.length === 3 && boolean(e[1])) fail('timestamp.tstinfo', REQ, 'Unsupported critical TSTInfo extension');
    }
    i++;
  }
  if (i !== parts.length) fail('timestamp.tstinfo', REQ, 'Unexpected TSTInfo field');
  return {
    policy,
    imprintAlgorithm,
    imprint,
    serial,
    genTime,
    ...(accuracySeconds !== undefined ? { accuracySeconds } : {}),
    ...(tsaName !== undefined ? { tsaName } : {}),
  };
}

function parseResponse(der: Uint8Array, maxCerts: number): ParsedToken {
  const resp = sequence(parseDer(der), 'TimeStampResp');
  if (resp.length < 1 || resp.length > 2) fail('timestamp.structure', REQ, 'Malformed TimeStampResp');
  const statusInfo = sequence(resp[0], 'PKIStatusInfo');
  const status = smallInt(statusInfo[0], 'PKIStatus');
  if (status !== 0 && status !== 1)
    fail('timestamp.status', REQ, 'Response status is not granted or grantedWithMods', { observed: status });
  if (!resp[1]) fail('timestamp.no_token', REQ, 'Granted response has no TimeStampToken');
  const ci = sequence(resp[1], 'ContentInfo');
  if (ci.length !== 2 || oid(ci[0]) !== TSP.signedData || !isContext(ci[1], 0))
    fail('timestamp.structure', REQ, 'TimeStampToken must be CMS SignedData');
  const sdT = children(ci[1] as Tlv);
  if (sdT.length !== 1) fail('timestamp.structure', REQ, 'Malformed ContentInfo');
  const sd = sequence(sdT[0], 'SignedData');
  let i = 0;
  smallInt(sd[i++], 'SignedData.version');
  setOf(sd[i++], 'digestAlgorithms');
  const eci = sequence(sd[i++], 'EncapsulatedContentInfo');
  if (oid(eci[0]) !== TSP.tstInfo) fail('timestamp.structure', REQ, 'eContentType must be id-ct-TSTInfo');
  if (!isContext(eci[1], 0) || eci.length !== 2) fail('timestamp.structure', REQ, 'eContent missing');
  const tstInfoDer = octetString(children(eci[1] as Tlv)[0], 'eContent');
  const certificates: Certificate[] = [];
  if (isContext(sd[i], 0)) {
    for (const c of children(sd[i] as Tlv)) {
      if (!isUniversal(c, UNIVERSAL.SEQUENCE)) continue; // other CertificateChoices are ignored
      if (certificates.length >= maxCerts) fail('timestamp.certificates', '§6', 'Too many certificates in token');
      certificates.push(parseCertificate(c.bytes));
    }
    i++;
  }
  if (isContext(sd[i], 1)) i++; // embedded CRLs are not used; trust data comes from local policy
  const signerInfos = setOf(sd[i++], 'signerInfos');
  if (i !== sd.length) fail('timestamp.structure', REQ, 'Unexpected SignedData field');
  if (signerInfos.length !== 1) fail('timestamp.structure', REQ, 'TimeStampToken must have exactly one signer');
  return {
    statusOk: true,
    signedData: sd,
    tstInfoDer,
    tstInfo: parseTstInfo(tstInfoDer),
    certificates,
    signerInfo: sequence(signerInfos[0], 'SignerInfo'),
  };
}

function digest(hash: HashName, data: Uint8Array): Uint8Array {
  return hash === 'SHA-256' ? sha256(data) : hash === 'SHA-384' ? sha384(data) : sha512(data);
}

interface EssCertRef {
  readonly hash: Uint8Array;
  readonly hashKind: 'SHA-1' | HashName;
  readonly issuerSerial?: { readonly issuer: Uint8Array; readonly serial: Uint8Array };
}

function parseIssuerSerial(t: Tlv | undefined): { issuer: Uint8Array; serial: Uint8Array } | undefined {
  if (!t) return undefined;
  const parts = sequence(t, 'IssuerSerial');
  const names = sequence(parts[0], 'GeneralNames');
  const dir = names.find((n) => isContext(n, 4));
  const nameT = dir ? children(dir)[0] : undefined;
  if (!nameT) fail('timestamp.ess', REQ, 'IssuerSerial must contain a directoryName');
  return { issuer: nameT.bytes, serial: integerBytes(parts[1], 'serialNumber') };
}

function parseEss(attr: Tlv, v2: boolean): EssCertRef {
  const sc = sequence(attr, v2 ? 'SigningCertificateV2' : 'SigningCertificate');
  const certs = sequence(sc[0], 'certs');
  const first = certs[0];
  if (!first) fail('timestamp.ess', REQ, 'ESS certs must not be empty');
  const parts = sequence(first, 'ESSCertID');
  let i = 0;
  let hashKind: 'SHA-1' | HashName = v2 ? 'SHA-256' : 'SHA-1';
  if (v2 && isUniversal(parts[0], UNIVERSAL.SEQUENCE)) {
    const h = hashNameOf(algorithmId(parts[0]).oid);
    if (!h) fail('timestamp.ess', REQ, 'Unsupported ESSCertIDv2 hash algorithm');
    hashKind = h;
    i = 1;
  }
  const hash = octetString(parts[i++], 'certHash');
  const issuerSerial = parseIssuerSerial(parts[i]);
  return { hash, hashKind, ...(issuerSerial ? { issuerSerial } : {}) };
}

function isTrustAnchor(cert: Certificate, anchors: readonly Certificate[]): boolean {
  return anchors.some((a) => equalBytes(a.der, cert.der));
}

function checkValidityAt(cert: Certificate, at: Instant, what: string): void {
  if (!instantLE(cert.notBefore.instant, at) || !instantLE(at, cert.notAfter.instant)) {
    fail('timestamp.cert_validity', 'RFC 5280', `${what} certificate is not valid at the required time`, {
      observed: renderName(cert.subject),
    });
  }
}

function checkCriticalExtensions(cert: Certificate): void {
  for (const [id, ext] of cert.extensions) {
    if (ext.critical && !PROCESSED_CERT_EXTENSIONS.has(id))
      fail('timestamp.cert_extension', 'RFC 5280', 'Unprocessed critical certificate extension', { observed: id });
  }
}

/** Build and verify a chain from `leaf` to a trust anchor. Returns the path (leaf first), or null if no anchor is reachable. */
async function buildChain(
  leaf: Certificate,
  pool: readonly Certificate[],
  anchors: readonly Certificate[],
  maxLen: number,
): Promise<Certificate[] | null> {
  const path: Certificate[] = [leaf];
  let current = leaf;
  for (let depth = 0; depth < maxLen; depth++) {
    if (isTrustAnchor(current, anchors)) return path;
    const issuers = [...anchors, ...pool].filter((c) => equalBytes(c.subject, current.issuer) && !equalBytes(c.der, current.der));
    let next: Certificate | undefined;
    for (const candidate of issuers) {
      if (!sameAlgorithm(current.signatureAlgorithm, current.tbsSignatureAlgorithm)) {
        fail('timestamp.cert_algorithm', 'RFC 5280', 'Certificate signature algorithm fields differ');
      }
      if (await verifySignedBy(current.tbs, current.signatureAlgorithm, current.signature, candidate)) {
        next = candidate;
        break;
      }
    }
    if (!next) {
      if (issuers.some((c) => isTrustAnchor(c, anchors)))
        fail('timestamp.cert_signature', 'RFC 5280', 'Certificate signature does not verify under its trust anchor issuer');
      return null;
    }
    path.push(next);
    current = next;
  }
  return isTrustAnchor(current, anchors) ? path : null;
}

async function checkRevocation(
  path: readonly Certificate[],
  crls: readonly Crl[],
  policy: RevocationPolicy,
  now: Instant,
): Promise<'ok' | 'unavailable'> {
  if (policy === 'none') return 'ok';
  // Every certificate except the anchor needs revocation status from its issuer.
  for (let i = 0; i + 1 < path.length; i++) {
    const cert = path[i] as Certificate;
    const issuer = path[i + 1] as Certificate;
    let current = false;
    for (const crl of crls) {
      if (!equalBytes(crl.issuer, issuer.subject)) continue;
      if (crl.hasUnprocessedCritical) continue;
      if (!sameAlgorithm(crl.signatureAlgorithm, crl.tbsSignatureAlgorithm)) continue;
      const ku = keyUsage(issuer);
      if (ku && !ku.has(6)) continue;
      if (!(await verifySignedBy(crl.tbs, crl.signatureAlgorithm, crl.signature, issuer))) continue;
      if (!instantLE(crl.thisUpdate.instant, now)) continue;
      if (!crl.nextUpdate || !instantLT(now, crl.nextUpdate.instant)) continue;
      if (crl.revoked.some((r) => equalBytes(r.serial, cert.serial))) {
        fail('timestamp.cert_revoked', '§6.8, RFC 5280', 'Certificate is revoked by a current CRL', { observed: renderName(cert.subject) });
      }
      current = true;
    }
    if (!current && policy === 'require_current_crl') return 'unavailable';
  }
  return 'ok';
}

interface VerifyContext {
  readonly anchors: readonly Certificate[];
  readonly intermediates: readonly Certificate[];
  readonly crls: readonly Crl[];
  readonly now: Instant;
  readonly policy: RevocationPolicy;
  readonly maxChain: number;
  readonly maxCerts: number;
}

async function verifyProof(
  der: Uint8Array,
  signatureBytes: Uint8Array,
  ctx: VerifyContext,
): Promise<Omit<TimestampProof, 'offset' | 'bytes'>> {
  const token = parseResponse(der, ctx.maxCerts);
  const info = token.tstInfo;
  // SCI: SHA-256 imprint of the verified signature bytes.
  if (info.imprintAlgorithm.oid !== OID.sha256)
    fail('timestamp.imprint_algorithm', '§6.8', 'messageImprint must use SHA-256', { observed: info.imprintAlgorithm.oid });
  if (
    info.imprintAlgorithm.params &&
    !(isUniversal(info.imprintAlgorithm.params, UNIVERSAL.NULL) && info.imprintAlgorithm.params.content.length === 0)
  ) {
    fail('timestamp.imprint_algorithm', '§6.8', 'Unexpected SHA-256 parameters');
  }
  if (!equalBytes(info.imprint, sha256(signatureBytes)))
    fail('timestamp.imprint_mismatch', '§6.8', 'messageImprint does not match the SHA-256 of the signature bytes');

  // CMS SignerInfo.
  const si = token.signerInfo;
  let i = 0;
  const siVersion = smallInt(si[i++], 'SignerInfo.version');
  const sid = si[i++];
  const digestAlg = algorithmId(si[i++]);
  const digestHash = hashNameOf(digestAlg.oid);
  if (!digestHash) fail('timestamp.digest_algorithm', REQ, 'Unsupported or weak CMS digest algorithm', { observed: digestAlg.oid });
  const signedAttrsT = si[i];
  if (!isContext(signedAttrsT, 0)) fail('timestamp.signed_attrs', REQ, 'SignerInfo must contain signed attributes');
  i++;
  const sigAlg = algorithmId(si[i++]);
  const signature = octetString(si[i++], 'signature');
  if (isContext(si[i], 1)) i++;
  if (i !== si.length) fail('timestamp.structure', REQ, 'Unexpected SignerInfo field');

  const attrs = new Map<string, Tlv[]>();
  for (const a of children(signedAttrsT)) {
    const [typeT, valuesT] = sequence(a, 'Attribute');
    const type = oid(typeT);
    if (attrs.has(type)) fail('timestamp.signed_attrs', 'RFC 5652', 'Duplicate signed attribute', { observed: type });
    attrs.set(type, setOf(valuesT, 'AttributeValues'));
  }
  const single = (type: string, what: string): Tlv => {
    const v = attrs.get(type);
    if (v?.length !== 1 || !v[0]) fail('timestamp.signed_attrs', REQ, `Signed attribute ${what} must be present with one value`);
    return v[0];
  };
  if (oid(single(TSP.contentType, 'content-type')) !== TSP.tstInfo)
    fail('timestamp.signed_attrs', REQ, 'content-type attribute must be id-ct-TSTInfo');
  if (!equalBytes(octetString(single(TSP.messageDigest, 'message-digest')), digest(digestHash, token.tstInfoDer))) {
    fail('timestamp.message_digest', REQ, 'message-digest does not match the TSTInfo');
  }

  // Identify the signer certificate.
  const pool = [...token.certificates, ...ctx.intermediates];
  let signer: Certificate | undefined;
  if (isUniversal(sid, UNIVERSAL.SEQUENCE)) {
    if (siVersion !== 1) fail('timestamp.structure', 'RFC 5652', 'issuerAndSerialNumber requires SignerInfo version 1');
    const [issuerT, serialT] = sequence(sid, 'IssuerAndSerialNumber');
    const serial = integerBytes(serialT);
    signer = pool.find((c) => equalBytes(c.issuer, (issuerT as Tlv).bytes) && equalBytes(c.serial, serial));
  } else if (isContext(sid, 0) && !sid.constructed) {
    if (siVersion !== 3) fail('timestamp.structure', 'RFC 5652', 'subjectKeyIdentifier requires SignerInfo version 3');
    signer = pool.find((c) => {
      const ski = subjectKeyIdentifier(c);
      return ski !== undefined && equalBytes(ski, sid.content);
    });
  } else {
    fail('timestamp.structure', 'RFC 5652', 'Malformed SignerIdentifier');
  }
  if (!signer) fail('timestamp.signer_certificate', REQ, 'Signer certificate not found in the token or supplied intermediates');

  // ESS signing certificate binding (RFC 2634 / RFC 5035 / RFC 5816).
  const essV2 = attrs.get(TSP.signingCertificateV2);
  const essV1 = attrs.get(TSP.signingCertificate);
  if (!essV2 && !essV1) fail('timestamp.ess', REQ, 'SigningCertificate or SigningCertificateV2 attribute is required');
  for (const [values, v2] of [
    [essV2, true],
    [essV1, false],
  ] as const) {
    if (!values) continue;
    if (values.length !== 1 || !values[0]) fail('timestamp.ess', REQ, 'ESS attribute must have one value');
    const ref = parseEss(values[0], v2);
    const actual = ref.hashKind === 'SHA-1' ? sha1(signer.der) : digest(ref.hashKind, signer.der);
    if (!equalBytes(ref.hash, actual))
      fail('timestamp.ess_mismatch', REQ, 'ESS certificate hash does not identify the signing certificate');
    if (ref.issuerSerial && !(equalBytes(ref.issuerSerial.issuer, signer.issuer) && equalBytes(ref.issuerSerial.serial, signer.serial))) {
      fail('timestamp.ess_mismatch', REQ, 'ESS issuerSerial does not identify the signing certificate');
    }
  }

  // CMS signature over the DER SET OF signed attributes.
  const signedData = new Uint8Array(signedAttrsT.bytes);
  signedData[0] = 0x31;
  if (!(await verifySignature(signer.spki, signer.publicKeyAlgorithm, sigAlg, signedData, signature, digestHash))) {
    fail('timestamp.signature', REQ, 'CMS signature does not verify');
  }

  // TSA certificate requirements (RFC 3161 §2.3).
  const eku = extKeyUsage(signer);
  if (!eku?.critical || eku.purposes.length !== 1 || eku.purposes[0] !== OID.timeStamping) {
    fail('timestamp.tsa_eku', 'RFC 3161 §2.3', 'TSA certificate must have a critical extKeyUsage containing only id-kp-timeStamping');
  }
  const ku = keyUsage(signer);
  if (ku && !ku.has(0) && !ku.has(1)) fail('timestamp.tsa_key_usage', 'RFC 5280', 'TSA key usage does not permit signatures');
  checkCriticalExtensions(signer);
  checkValidityAt(signer, info.genTime.instant, 'TSA (at genTime)');

  const tsa = {
    subject: renderName(signer.subject),
    certificateSha256: toHex(sha256(signer.der)),
    ...(info.tsaName !== undefined ? { name: info.tsaName } : {}),
  };
  const base = {
    genTime: info.genTime.text,
    ...(info.accuracySeconds !== undefined ? { accuracySeconds: info.accuracySeconds } : {}),
    tsa,
    serialNumber: toHex(info.serial),
    policy: info.policy,
  };

  // Chain to a configured trust anchor.
  const path = ctx.anchors.length === 0 ? null : await buildChain(signer, pool, ctx.anchors, ctx.maxChain);
  if (!path) return { result: 'untrusted', tsa, serialNumber: base.serialNumber, policy: base.policy };
  for (let k = 0; k < path.length; k++) {
    const cert = path[k] as Certificate;
    checkValidityAt(cert, ctx.now, k === 0 ? 'TSA' : 'Issuer');
    checkCriticalExtensions(cert);
    if (k > 0) {
      const bc = basicConstraints(cert);
      if (!bc?.ca) fail('timestamp.ca', 'RFC 5280', 'Issuer certificate is not a CA');
      if (bc.pathLen !== undefined && bc.pathLen < k - 1) fail('timestamp.path_length', 'RFC 5280', 'Path length constraint exceeded');
      const cku = keyUsage(cert);
      if (cku && !cku.has(5)) fail('timestamp.ca', 'RFC 5280', 'Issuer key usage does not permit certificate signing');
    }
  }
  const revocation = await checkRevocation(path, ctx.crls, ctx.policy, ctx.now);
  if (revocation === 'unavailable') {
    return {
      result: 'incomplete',
      ...base,
      issue: {
        code: 'timestamp.revocation_unavailable',
        outcome: 'incomplete',
        requirement: '§6.8',
        message: 'No current CRL is available for a certificate in the chain',
      },
    };
  }
  return { result: 'verified', ...base };
}

/**
 * Verify every complete DER value in a timestamp file (§6.8) against the exact signature bytes
 * (`manifest.json.sig` or `revocation.json.sig`). The caller must already have completed the
 * manifest (§6.1) or revocation (§6.7) checks for those bytes; see {@link verifyPublicationTimestamps}.
 */
export async function verifyTimestampFile(
  file: Uint8Array,
  signatureBytes: Uint8Array,
  options: TimestampTrustOptions,
): Promise<TimestampFileResult> {
  const budget = resolveBudget(options.budget);
  const parseAll = <T>(list: readonly (Uint8Array | string)[] | undefined, label: string, parse: (d: Uint8Array) => T): T[] => {
    const out: T[] = [];
    for (const item of list ?? []) for (const der of toDerList(item, label)) out.push(parse(der));
    return out;
  };
  const ctx: VerifyContext = {
    anchors: parseAll(options.trustAnchors, 'CERTIFICATE', parseCertificate),
    intermediates: parseAll(options.intermediates, 'CERTIFICATE', parseCertificate),
    crls: parseAll(options.crls, 'X509 CRL', parseCrl),
    now: instantFromDate(options.verificationTime ?? new Date()),
    policy: options.revocationPolicy ?? 'require_current_crl',
    maxChain: options.maxChainLength ?? 6,
    maxCerts: budget.maxCertificates,
  };
  const proofs: TimestampProof[] = [];
  let pos = 0;
  const limit = Math.min(file.length, budget.maxTimestampBytes);
  while (pos < file.length) {
    if (pos >= limit || proofs.length >= budget.maxTimestampProofs) break;
    const end = frameDer(file, pos);
    if (end === null) break;
    const value = file.subarray(pos, end);
    let proof: TimestampProof;
    try {
      proof = { offset: pos, bytes: end - pos, ...(await verifyProof(value, signatureBytes, ctx)) };
    } catch (error) {
      const issue = toIssue(error);
      proof = {
        offset: pos,
        bytes: end - pos,
        result: error instanceof SciError && issue.outcome === 'incomplete' ? 'incomplete' : 'invalid',
        issue,
      };
    }
    proofs.push(proof);
    pos = end;
  }
  const framing = pos === file.length && file.length > 0 ? 'complete' : 'malformed';
  const results = proofs.map((p) => p.result);
  let outcome: TimestampOutcome;
  if (framing === 'complete' && results.every((r) => r === 'verified')) outcome = 'valid';
  else if (results.includes('verified')) outcome = 'partial';
  else if (framing === 'complete' && results.length > 0 && results.every((r) => r === 'untrusted')) outcome = 'untrusted';
  else if (results.includes('incomplete') || (pos < file.length && (pos >= limit || proofs.length >= budget.maxTimestampProofs)))
    outcome = 'incomplete';
  else outcome = 'invalid';
  return { outcome, framing, proofs, stopOffset: pos };
}

/** Count complete DER values in a timestamp file without verifying them (for lookup responses). */
export function frameTimestampFile(file: Uint8Array): {
  readonly values: number;
  readonly stopOffset: number;
  readonly framing: 'complete' | 'malformed';
} {
  let pos = 0;
  let values = 0;
  for (;;) {
    const end = frameDer(file, pos);
    if (end === null) break;
    values++;
    pos = end;
  }
  return { values, stopOffset: pos, framing: pos === file.length && file.length > 0 ? 'complete' : 'malformed' };
}

/** Earliest verified genTime among proofs (useful for display), or undefined. */
export function earliestVerifiedTime(result: TimestampFileResult): string | undefined {
  let best: string | undefined;
  for (const p of result.proofs) if (p.result === 'verified' && p.genTime && (!best || p.genTime < best)) best = p.genTime;
  return best;
}

/** Options for {@link verifyPublicationTimestamps}. */
export interface PublicationTimestampOptions extends TimestampTrustOptions {
  /** Expected publication ID (any protocol spelling). */
  readonly expectedId?: string;
}

/**
 * §6.8 for a publication: complete the §6.1 manifest checks, then verify the timestamp file
 * against the exact `manifest.json.sig` bytes. A failed timestamp never invalidates the manifest.
 */
export async function verifyPublicationTimestamps(
  manifestBytes: Uint8Array,
  signature: Uint8Array,
  file: Uint8Array,
  options: PublicationTimestampOptions,
): Promise<TimestampFileResult & { readonly publicationId: string }> {
  const manifest = verifyManifest(manifestBytes, signature, { expectedId: options.expectedId, budget: options.budget });
  return { ...(await verifyTimestampFile(file, signature, options)), publicationId: manifest.id };
}

/** §6.8 for a revocation: complete the §6.7 checks, then verify the timestamp file against `revocation.json.sig`. */
export async function verifyRevocationTimestamps(
  record: Uint8Array,
  signature: Uint8Array,
  file: Uint8Array,
  options: TimestampTrustOptions & { readonly expectedPublicKeyX?: string },
): Promise<TimestampFileResult & { readonly publicKeyX: string }> {
  const rev = verifyRevocation(record, signature, {
    ...(options.expectedPublicKeyX !== undefined ? { expectedPublicKeyX: options.expectedPublicKeyX } : {}),
    ...(options.budget !== undefined ? { budget: options.budget } : {}),
  });
  return { ...(await verifyTimestampFile(file, signature, options)), publicKeyX: rev.publicKey.x };
}
