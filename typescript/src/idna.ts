/**
 * Domain-name checks for the SCI §5.2.2 domain method: lowercase ASCII DNS host names whose
 * internationalized labels are valid IDNA2008 A-labels (RFC 5890 §2.3.2.1).
 *
 * A-label validation: RFC 3492 Punycode decoding, round-trip re-encoding, RFC 5891 §5.4 label
 * rules, RFC 5892 derived properties (computed from the JavaScript engine's Unicode property
 * escapes, plus the RFC 5892 exception table), CONTEXTJ/CONTEXTO rules, and an approximation of
 * the RFC 5893 Bidi rule.
 *
 * Documented limitations (no bundled Unicode tables):
 * - Derived properties follow the engine's Unicode version, not a fixed IDNA table version.
 * - The "Unstable" property uses `toLowerCase()` in place of full case folding.
 * - CONTEXTJ accepts ZWNJ/ZWJ only after a virama (Canonical_Combining_Class=9, fixed list from
 *   Unicode 16.0); the ZWNJ joining-type rule is not evaluated, so such labels are rejected.
 * - Bidi classes are approximated by script membership.
 */

const BASE = 36;
const TMIN = 1;
const TMAX = 26;
const SKEW = 38;
const DAMP = 700;
const INITIAL_BIAS = 72;
const INITIAL_N = 128;
const MAXINT = 0x7fffffff;

function adapt(delta: number, numPoints: number, firstTime: boolean): number {
  let d = firstTime ? Math.floor(delta / DAMP) : delta >> 1;
  d += Math.floor(d / numPoints);
  let k = 0;
  while (d > ((BASE - TMIN) * TMAX) >> 1) {
    d = Math.floor(d / (BASE - TMIN));
    k += BASE;
  }
  return k + Math.floor(((BASE - TMIN + 1) * d) / (d + SKEW));
}

function digitValue(c: number): number {
  if (c >= 0x30 && c <= 0x39) return c - 22; // '0'..'9' -> 26..35
  if (c >= 0x41 && c <= 0x5a) return c - 0x41;
  if (c >= 0x61 && c <= 0x7a) return c - 0x61;
  return BASE;
}

function digitChar(d: number): string {
  return String.fromCharCode(d < 26 ? d + 0x61 : d + 22);
}

/** RFC 3492 decode of the part after `xn--`. Returns null on any error. */
export function punycodeDecode(input: string): string | null {
  const output: number[] = [];
  const delim = input.lastIndexOf('-');
  const basic = Math.max(delim, 0);
  for (let j = 0; j < basic; j++) {
    const c = input.charCodeAt(j);
    if (c >= 0x80) return null;
    output.push(c);
  }
  let n = INITIAL_N;
  let bias = INITIAL_BIAS;
  let i = 0;
  for (let pos = delim < 0 ? 0 : delim + 1; pos < input.length; ) {
    const oldi = i;
    let w = 1;
    for (let k = BASE; ; k += BASE) {
      if (pos >= input.length) return null;
      const digit = digitValue(input.charCodeAt(pos++));
      if (digit >= BASE) return null;
      if (digit > Math.floor((MAXINT - i) / w)) return null;
      i += digit * w;
      const t = k <= bias ? TMIN : k >= bias + TMAX ? TMAX : k - bias;
      if (digit < t) break;
      if (w > Math.floor(MAXINT / (BASE - t))) return null;
      w *= BASE - t;
    }
    const len = output.length + 1;
    bias = adapt(i - oldi, len, oldi === 0);
    if (Math.floor(i / len) > MAXINT - n) return null;
    n += Math.floor(i / len);
    i %= len;
    if (n > 0x10ffff || (n >= 0xd800 && n <= 0xdfff)) return null;
    output.splice(i, 0, n);
    i++;
  }
  return String.fromCodePoint(...output);
}

/** RFC 3492 encode. Returns null on overflow. */
export function punycodeEncode(input: string): string | null {
  const cps = [...input].map((c) => c.codePointAt(0) ?? 0);
  let out = '';
  for (const c of cps) if (c < 0x80) out += String.fromCharCode(c);
  const b = out.length;
  let h = b;
  if (b > 0) out += '-';
  let n = INITIAL_N;
  let delta = 0;
  let bias = INITIAL_BIAS;
  while (h < cps.length) {
    let m = MAXINT;
    for (const c of cps) if (c >= n && c < m) m = c;
    if (m - n > Math.floor((MAXINT - delta) / (h + 1))) return null;
    delta += (m - n) * (h + 1);
    n = m;
    for (const c of cps) {
      if (c < n && ++delta > MAXINT) return null;
      if (c === n) {
        let q = delta;
        for (let k = BASE; ; k += BASE) {
          const t = k <= bias ? TMIN : k >= bias + TMAX ? TMAX : k - bias;
          if (q < t) break;
          out += digitChar(t + ((q - t) % (BASE - t)));
          q = Math.floor((q - t) / (BASE - t));
        }
        out += digitChar(q);
        bias = adapt(delta, h + 1, h === b);
        delta = 0;
        h++;
      }
    }
    delta++;
    n++;
  }
  return out;
}

type Derived = 'PVALID' | 'CONTEXTJ' | 'CONTEXTO' | 'DISALLOWED' | 'UNASSIGNED';

const EXCEPTIONS = new Map<number, Derived>([
  [0x00df, 'PVALID'],
  [0x03c2, 'PVALID'],
  [0x06fd, 'PVALID'],
  [0x06fe, 'PVALID'],
  [0x0f0b, 'PVALID'],
  [0x3007, 'PVALID'],
  [0x00b7, 'CONTEXTO'],
  [0x0375, 'CONTEXTO'],
  [0x05f3, 'CONTEXTO'],
  [0x05f4, 'CONTEXTO'],
  [0x30fb, 'CONTEXTO'],
  [0x0640, 'DISALLOWED'],
  [0x07fa, 'DISALLOWED'],
  [0x302e, 'DISALLOWED'],
  [0x302f, 'DISALLOWED'],
  [0x3031, 'DISALLOWED'],
  [0x3032, 'DISALLOWED'],
  [0x3033, 'DISALLOWED'],
  [0x3034, 'DISALLOWED'],
  [0x3035, 'DISALLOWED'],
  [0x303b, 'DISALLOWED'],
]);
for (let c = 0x0660; c <= 0x0669; c++) EXCEPTIONS.set(c, 'CONTEXTO');
for (let c = 0x06f0; c <= 0x06f9; c++) EXCEPTIONS.set(c, 'CONTEXTO');

const RE_UNASSIGNED = /^\p{Cn}$/u;
const RE_IGNORABLE = /^[\p{Default_Ignorable_Code_Point}\p{White_Space}\p{Noncharacter_Code_Point}]$/u;
const RE_LETTER_DIGIT = /^[\p{Ll}\p{Lu}\p{Lo}\p{Nd}\p{Lm}\p{Mn}\p{Mc}]$/u;

function derivedProperty(cp: number): Derived {
  const ex = EXCEPTIONS.get(cp);
  if (ex) return ex;
  const ch = String.fromCodePoint(cp);
  if (RE_UNASSIGNED.test(ch)) return 'UNASSIGNED';
  if ((cp >= 0x61 && cp <= 0x7a) || (cp >= 0x30 && cp <= 0x39) || cp === 0x2d) return 'PVALID';
  if (cp === 0x200c || cp === 0x200d) return 'CONTEXTJ';
  if (ch.normalize('NFKC').toLowerCase().normalize('NFKC') !== ch) return 'DISALLOWED';
  if (RE_IGNORABLE.test(ch)) return 'DISALLOWED';
  if ((cp >= 0x20d0 && cp <= 0x20ff) || (cp >= 0x1d100 && cp <= 0x1d24f)) return 'DISALLOWED';
  if ((cp >= 0x1100 && cp <= 0x11ff) || (cp >= 0xa960 && cp <= 0xa97f) || (cp >= 0xd7b0 && cp <= 0xd7ff)) return 'DISALLOWED';
  if (RE_LETTER_DIGIT.test(ch)) return 'PVALID';
  return 'DISALLOWED';
}

const VIRAMA: readonly [number, number][] = [
  [0x94d, 0x94d],
  [0x9cd, 0x9cd],
  [0xa4d, 0xa4d],
  [0xacd, 0xacd],
  [0xb4d, 0xb4d],
  [0xbcd, 0xbcd],
  [0xc4d, 0xc4d],
  [0xccd, 0xccd],
  [0xd3b, 0xd3c],
  [0xd4d, 0xd4d],
  [0xdca, 0xdca],
  [0xe3a, 0xe3a],
  [0xeba, 0xeba],
  [0xf84, 0xf84],
  [0x1039, 0x103a],
  [0x1714, 0x1715],
  [0x1734, 0x1734],
  [0x17d2, 0x17d2],
  [0x1a60, 0x1a60],
  [0x1b44, 0x1b44],
  [0x1baa, 0x1bab],
  [0x1bf2, 0x1bf3],
  [0x2d7f, 0x2d7f],
  [0xa806, 0xa806],
  [0xa82c, 0xa82c],
  [0xa8c4, 0xa8c4],
  [0xa953, 0xa953],
  [0xa9c0, 0xa9c0],
  [0xaaf6, 0xaaf6],
  [0xabed, 0xabed],
  [0x10a3f, 0x10a3f],
  [0x11046, 0x11046],
  [0x11070, 0x11070],
  [0x1107f, 0x1107f],
  [0x110b9, 0x110b9],
  [0x11133, 0x11134],
  [0x111c0, 0x111c0],
  [0x11235, 0x11235],
  [0x112ea, 0x112ea],
  [0x1134d, 0x1134d],
  [0x113ce, 0x113d0],
  [0x11442, 0x11442],
  [0x114c2, 0x114c2],
  [0x115bf, 0x115bf],
  [0x1163f, 0x1163f],
  [0x116b6, 0x116b6],
  [0x1172b, 0x1172b],
  [0x11839, 0x11839],
  [0x1193d, 0x1193e],
  [0x119e0, 0x119e0],
  [0x11a34, 0x11a34],
  [0x11a47, 0x11a47],
  [0x11a99, 0x11a99],
  [0x11c3f, 0x11c3f],
  [0x11d44, 0x11d45],
  [0x11d97, 0x11d97],
  [0x11f41, 0x11f42],
  [0x1612f, 0x1612f],
];

function isVirama(cp: number | undefined): boolean {
  return cp !== undefined && VIRAMA.some(([a, b]) => cp >= a && cp <= b);
}

const RE_GREEK = /^\p{Script=Greek}$/u;
const RE_HEBREW = /^\p{Script=Hebrew}$/u;
const RE_HKH = /^[\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Han}]$/u;
const RE_RTL =
  /^[\p{Script=Hebrew}\p{Script=Arabic}\p{Script=Syriac}\p{Script=Thaana}\p{Script=Nko}\p{Script=Samaritan}\p{Script=Mandaic}\p{Script=Adlam}\p{Script=Mende_Kikakui}\p{Script=Imperial_Aramaic}\p{Script=Phoenician}\p{Script=Kharoshthi}\p{Script=Avestan}\p{Script=Old_South_Arabian}\p{Script=Old_North_Arabian}\p{Script=Nabataean}\p{Script=Palmyrene}\p{Script=Hatran}\p{Script=Manichaean}\p{Script=Psalter_Pahlavi}\p{Script=Inscriptional_Pahlavi}\p{Script=Inscriptional_Parthian}\p{Script=Lydian}\p{Script=Cypriot}\p{Script=Old_Hungarian}]$/u;
const RE_NSM = /^[\p{Mn}\p{Me}]$/u;

type Bidi = 'L' | 'R' | 'AN' | 'EN' | 'ES' | 'NSM';

function bidiClass(cp: number): Bidi {
  const ch = String.fromCodePoint(cp);
  if (cp === 0x2d) return 'ES';
  if (cp >= 0x30 && cp <= 0x39) return 'EN';
  if (cp >= 0x06f0 && cp <= 0x06f9) return 'EN';
  if ((cp >= 0x0660 && cp <= 0x0669) || (cp >= 0x10d30 && cp <= 0x10d39)) return 'AN';
  if (RE_NSM.test(ch)) return 'NSM';
  if (RE_RTL.test(ch)) return 'R';
  return 'L';
}

/** Validate a U-label under RFC 5891 §5.4 / RFC 5892 (Bidi checked separately). */
function checkULabel(u: string): boolean {
  if (u.length === 0 || u !== u.normalize('NFC')) return false;
  const cps = [...u].map((c) => c.codePointAt(0) ?? 0);
  if (cps[0] === 0x2d || cps[cps.length - 1] === 0x2d) return false;
  if (cps[2] === 0x2d && cps[3] === 0x2d) return false;
  if (/^\p{M}$/u.test(String.fromCodePoint(cps[0] ?? 0))) return false;
  for (let i = 0; i < cps.length; i++) {
    const cp = cps[i] ?? 0;
    const prop = derivedProperty(cp);
    if (prop === 'PVALID') continue;
    if (prop === 'CONTEXTJ') {
      if (!isVirama(cps[i - 1])) return false;
      continue;
    }
    if (prop === 'CONTEXTO') {
      if (cp === 0x00b7) {
        if (cps[i - 1] !== 0x6c || cps[i + 1] !== 0x6c) return false;
      } else if (cp === 0x0375) {
        const next = cps[i + 1];
        if (next === undefined || !RE_GREEK.test(String.fromCodePoint(next))) return false;
      } else if (cp === 0x05f3 || cp === 0x05f4) {
        const prev = cps[i - 1];
        if (prev === undefined || !RE_HEBREW.test(String.fromCodePoint(prev))) return false;
      } else if (cp === 0x30fb) {
        if (!cps.some((c) => c !== 0x30fb && RE_HKH.test(String.fromCodePoint(c)))) return false;
      } else if (cp >= 0x0660 && cp <= 0x0669) {
        if (cps.some((c) => c >= 0x06f0 && c <= 0x06f9)) return false;
      } else if (cp >= 0x06f0 && cp <= 0x06f9) {
        if (cps.some((c) => c >= 0x0660 && c <= 0x0669)) return false;
      } else {
        return false;
      }
      continue;
    }
    return false;
  }
  return true;
}

/** RFC 5893 Bidi rule (approximate classes) for one label of a Bidi domain name. */
function checkBidiLabel(u: string): boolean {
  const classes = [...u].map((c) => bidiClass(c.codePointAt(0) ?? 0));
  const first = classes[0];
  if (first !== 'L' && first !== 'R') return false;
  let end = classes.length - 1;
  while (end > 0 && classes[end] === 'NSM') end--;
  const last = classes[end];
  if (first === 'R') {
    if (classes.includes('L')) return false;
    if (last !== 'R' && last !== 'EN' && last !== 'AN') return false;
    if (classes.includes('EN') && classes.includes('AN')) return false;
  } else {
    if (classes.includes('R') || classes.includes('AN')) return false;
    if (last !== 'L' && last !== 'EN') return false;
  }
  return true;
}

/** Validate one lowercase A-label (`xn--…`). Returns the U-label or null. */
export function decodeALabel(label: string): string | null {
  if (!label.startsWith('xn--') || label.length > 63 || !/^[a-z0-9-]+$/.test(label)) return null;
  const u = punycodeDecode(label.slice(4));
  if (u === null || [...u].every((c) => (c.codePointAt(0) ?? 0) < 0x80)) return null;
  if (!checkULabel(u)) return null;
  if (punycodeEncode(u) !== label.slice(4)) return null;
  return u;
}

/**
 * Validate a §5.2.2 domain `account.id`: lowercase ASCII DNS host name, no final dot, no IP
 * address, LDH labels or valid IDNA2008 A-labels.
 */
export function isValidClaimDomain(domain: string): boolean {
  if (domain.length === 0 || domain.length > 253 || domain.endsWith('.')) return false;
  if (!/^[a-z0-9.-]+$/.test(domain)) return false;
  const labels = domain.split('.');
  const uLabels: string[] = [];
  for (const label of labels) {
    if (label.length === 0 || label.length > 63) return false;
    if (!/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label)) return false;
    if (label[2] === '-' && label[3] === '-') {
      const u = decodeALabel(label);
      if (u === null) return false;
      uLabels.push(u);
    } else {
      uLabels.push(label);
    }
  }
  // IP addresses are not permitted; an all-numeric final label never names a DNS host.
  if (/^[0-9]+$/.test(labels[labels.length - 1] ?? '')) return false;
  const isBidiDomain = uLabels.some((u) =>
    [...u].some((c) => {
      const k = bidiClass(c.codePointAt(0) ?? 0);
      return k === 'R' || k === 'AN';
    }),
  );
  if (isBidiDomain && !uLabels.every(checkBidiLabel)) return false;
  return true;
}
