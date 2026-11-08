import * as asn1 from "asn1js";
import {
  TimeStampResp,
  SignedData,
  TSTInfo,
  Certificate,
  CertificateRevocationList,
  CertificateChainValidationEngine,
} from "pkijs";
import { hash, requireValue, Invalid, type ObjectValue } from "./index.js";

export function frame(data: Uint8Array, offset: number): number | null {
  let pos = offset;
  if (pos >= data.length) return null;
  const tag = data[pos++];
  if ((tag & 31) === 31) {
    let first = true;
    for (;;) {
      if (pos >= data.length) return null;
      const v = data[pos++];
      if (first && (v & 127) === 0) return null;
      first = false;
      if (!(v & 128)) break;
    }
  }
  if (pos >= data.length) return null;
  const v = data[pos++];
  let length: number;
  if (v < 128) length = v;
  else {
    const n = v & 127;
    if (n === 0 || n > 6 || pos + n > data.length || data[pos] === 0)
      return null;
    length = 0;
    for (let i = 0; i < n; i++) length = length * 256 + data[pos++];
    if (length < 128) return null;
  }
  return length <= data.length - pos ? pos + length : null;
}
function parse(raw: Uint8Array): asn1.AsnType {
  const a = asn1.fromBER(raw);
  requireValue(a.offset === raw.length, "ASN.1 value");
  return a.result;
}
function pem(raw: Uint8Array): Uint8Array {
  const s = new TextDecoder().decode(raw);
  if (!s.startsWith("-----BEGIN")) return raw;
  return Uint8Array.from(
    atob(s.replace(/-----[^-]+-----/g, "").replace(/\s/g, "")),
    (c) => c.charCodeAt(0),
  );
}
export interface TimestampTrust {
  anchors: Uint8Array[];
  crls: Uint8Array[];
  now: Date;
}
async function proof(
  raw: Uint8Array,
  signature: Uint8Array,
  trust: TimestampTrust,
): Promise<ObjectValue> {
  const response = new TimeStampResp({ schema: parse(raw) });
  requireValue(
    [0, 1].includes(response.status.status) && response.timeStampToken,
    "timestamp status/token",
  );
  requireValue(
    response.timeStampToken.contentType === "1.2.840.113549.1.7.2",
    "CMS content",
  );
  const signed = new SignedData({ schema: response.timeStampToken.content });
  requireValue(
    signed.encapContentInfo.eContentType === "1.2.840.113549.1.9.16.1.4" &&
      signed.encapContentInfo.eContent,
    "TSTInfo",
  );
  const info = new TSTInfo({
    schema: parse(new Uint8Array(signed.encapContentInfo.eContent.getValue())),
  });
  requireValue(
    info.messageImprint.hashAlgorithm.algorithmId === "2.16.840.1.101.3.4.2.1",
    "SHA-256 timestamp imprint",
  );
  requireValue(
    hash(signature) ===
      Array.from(
        info.messageImprint.hashedMessage.valueBlock.valueHexView,
        (b) => b.toString(16).padStart(2, "0"),
      ).join(""),
    "timestamp imprint",
  );
  requireValue(signed.signerInfos.length === 1, "single timestamp signer");
  const result = await signed.verify({
    signer: 0,
    data: new Uint8Array(signature).buffer,
    checkChain: false,
    extendedMode: true,
  });
  requireValue(
    result.signatureVerified && result.signerCertificate,
    "CMS signature",
  );
  const signer = result.signerCertificate;
  const attrs = signed.signerInfos[0].signedAttrs?.attributes ?? [];
  const binding =
    attrs.find((a) => a.type === "1.2.840.113549.1.9.16.2.47") ??
    attrs.find((a) => a.type === "1.2.840.113549.1.9.16.2.12");
  requireValue(
    binding && binding.values.length === 1,
    "ESS certificate binding required",
  );
  const ess = (binding.values[0] as asn1.Sequence).valueBlock
    .value[0] as asn1.Sequence;
  const first = ess.valueBlock.value[0] as asn1.Sequence;
  let alg = binding.type.endsWith(".47") ? "SHA-256" : "SHA-1",
    index = 0;
  if (first.valueBlock.value[0] instanceof asn1.Sequence) {
    const oid = (
      (first.valueBlock.value[0] as asn1.Sequence).valueBlock
        .value[0] as asn1.ObjectIdentifier
    ).valueBlock.toString();
    const algorithms: Record<string, string> = {
      "2.16.840.1.101.3.4.2.1": "SHA-256",
      "2.16.840.1.101.3.4.2.2": "SHA-384",
      "2.16.840.1.101.3.4.2.3": "SHA-512",
      "1.3.14.3.2.26": "SHA-1",
    };
    requireValue(algorithms[oid], "ESS hash algorithm");
    alg = algorithms[oid];
    index = 1;
  }
  const expected = (first.valueBlock.value[index] as asn1.OctetString)
    .valueBlock.valueHexView;
  const actual = new Uint8Array(
    await crypto.subtle.digest(alg, signer.toSchema(true).toBER(false)),
  );
  requireValue(
    expected.length === actual.length &&
      actual.every((b, i) => b === expected[i]),
    "ESS certificate hash",
  );
  const eku = signer.extensions?.find((e) => e.extnID === "2.5.29.37");
  requireValue(
    eku?.critical &&
      eku.parsedValue?.keyPurposes?.length === 1 &&
      eku.parsedValue.keyPurposes[0] === "1.3.6.1.5.5.7.3.8",
    "critical timestamp EKU",
  );
  if (!trust.anchors.length) return { result: "untrusted" };
  if (!trust.crls.length) return { result: "untrusted" };
  const anchors = trust.anchors.map(
    (b) => new Certificate({ schema: parse(pem(b)) }),
  );
  const crls = trust.crls.map(
    (b) => new CertificateRevocationList({ schema: parse(pem(b)) }),
  );
  for (const crl of crls)
    requireValue(
      crl.thisUpdate.value <= trust.now &&
        crl.nextUpdate &&
        trust.now < crl.nextUpdate.value,
      "CRL validity",
    );
  requireValue(
    signer.notBefore.value <= trust.now && trust.now <= signer.notAfter.value,
    "certificate validity",
  );
  const engine = new CertificateChainValidationEngine({
    trustedCerts: anchors,
    certs: [
      ...(signed.certificates ?? []).filter(
        (c): c is Certificate => c instanceof Certificate && c !== signer,
      ),
      signer,
    ],
    crls,
    checkDate: trust.now,
  });
  const checked = await engine.verify({ passedWhenNotRevValues: false });
  requireValue(checked.result, "timestamp chain and revocation");
  return {
    result: "verified",
    gen_time: info.genTime.toISOString().replace(".000Z", "Z"),
    accuracy_seconds: info.accuracy?.seconds ?? null,
  };
}
export async function verifyProofs(
  data: Uint8Array,
  signature: Uint8Array,
  trust: TimestampTrust,
): Promise<ObjectValue> {
  if (data.length > 1048576) throw new Invalid("timestamp sidecar limit");
  let pos = 0;
  const proofs: ObjectValue[] = [];
  while (pos < data.length) {
    const end = frame(data, pos);
    if (end === null) break;
    let checked: ObjectValue;
    try {
      checked = await proof(data.subarray(pos, end), signature, trust);
    } catch {
      checked = { result: "invalid" };
    }
    proofs.push({ offset: pos, bytes: end - pos, ...checked });
    pos = end;
  }
  const framing = pos === data.length && data.length ? "complete" : "malformed";
  const all =
      framing === "complete" && proofs.every((p) => p.result === "verified"),
    any = proofs.some((p) => p.result === "verified"),
    untrusted =
      framing === "complete" && proofs.every((p) => p.result === "untrusted");
  return {
    outcome: all
      ? "valid"
      : any
        ? "partial"
        : untrusted
          ? "untrusted"
          : "invalid",
    framing,
    proofs,
    stop_offset: pos,
  };
}
