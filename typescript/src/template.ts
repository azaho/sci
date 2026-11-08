/**
 * Restricted RFC 6570 URI templates (SCI §8): only single-variable simple-string (`{var}`) and
 * reserved (`{+var}`) expressions, limited per field to the expressions the spec lists.
 */
import { isAbsoluteHttpUrl } from './citation.ts';
import { fail } from './errors.ts';

export type TemplatePart =
  | { readonly literal: string }
  | { readonly expression: string; readonly name: string; readonly reserved: boolean };

// RFC 6570 §2.1 literals: any Unicode except CTL, SP, DQUOTE, "'", "%" (aside from pct-encoded),
// "<", ">", "\", "^", "`", "{", "|", "}".
// biome-ignore lint/suspicious/noControlCharactersInRegex: RFC 6570 excludes CTL characters from literals.
const LITERAL_CHAR = /^[^\x00-\x20\x7f"'%<>\\^`{|}]$/u;

/** Parse a template into literal and expression parts. Throws `invalid` for malformed templates. */
export function parseTemplate(template: string, requirement = '§8', path?: string): TemplatePart[] {
  const parts: TemplatePart[] = [];
  let literal = '';
  for (let i = 0; i < template.length; ) {
    const ch = template[i] ?? '';
    if (ch === '{') {
      const close = template.indexOf('}', i + 1);
      const open = template.indexOf('{', i + 1);
      if (close < 0 || (open >= 0 && open < close))
        fail('template.unclosed', requirement, 'Unclosed template expression', { path, observed: template });
      const expr = template.slice(i + 1, close);
      const m = /^(\+?)([A-Za-z0-9_]+)$/.exec(expr);
      if (!m) fail('template.expression', requirement, 'Only {var} and {+var} expressions are permitted', { path, observed: `{${expr}}` });
      if (literal) parts.push({ literal });
      literal = '';
      parts.push({ expression: expr, name: m[2] ?? '', reserved: m[1] === '+' });
      i = close + 1;
      continue;
    }
    if (ch === '}') fail('template.unbalanced', requirement, 'Unbalanced "}" in template', { path, observed: template });
    if (ch === '%') {
      if (!/^%[0-9A-Fa-f]{2}$/.test(template.slice(i, i + 3)))
        fail('template.literal', requirement, 'Invalid percent-encoding in template literal', { path });
      literal += template.slice(i, i + 3);
      i += 3;
      continue;
    }
    const cp = template.codePointAt(i) ?? 0;
    const c = String.fromCodePoint(cp);
    if (!LITERAL_CHAR.test(c)) fail('template.literal', requirement, 'Character not permitted in template literal', { path, observed: c });
    literal += c;
    i += c.length;
  }
  if (literal) parts.push({ literal });
  return parts;
}

const UNRESERVED = /[A-Za-z0-9\-._~]/;
const RESERVED = /[:/?#[\]@!$&'()*+,;=]/;

function encodeValue(value: string, reserved: boolean): string {
  let out = '';
  const bytes = new TextEncoder().encode(value);
  for (let i = 0; i < bytes.length; i++) {
    const b = bytes[i] ?? 0;
    const c = String.fromCharCode(b);
    if (b < 0x80 && UNRESERVED.test(c)) out += c;
    else if (reserved && b < 0x80 && RESERVED.test(c)) out += c;
    else if (reserved && c === '%' && /^[0-9A-Fa-f]{2}$/.test(String.fromCharCode(bytes[i + 1] ?? 0, bytes[i + 2] ?? 0))) out += c;
    else out += `%${b.toString(16).toUpperCase().padStart(2, '0')}`;
  }
  return out;
}

function encodeLiteral(lit: string): string {
  let out = '';
  for (const ch of lit) {
    const cp = ch.codePointAt(0) ?? 0;
    if (cp < 0x80) out += ch;
    else for (const b of new TextEncoder().encode(ch)) out += `%${b.toString(16).toUpperCase().padStart(2, '0')}`;
  }
  return out;
}

/** Expand a parsed template with string variables (RFC 6570 levels 1–2 for single variables). */
export function expandParts(parts: readonly TemplatePart[], vars: Readonly<Record<string, string>>): string {
  let out = '';
  for (const p of parts) {
    if ('literal' in p) out += encodeLiteral(p.literal);
    else {
      const v = vars[p.name];
      if (v === undefined) throw new RangeError(`missing template variable ${p.name}`);
      out += encodeValue(v, p.reserved);
    }
  }
  return out;
}

/** Parse, restrict to `allowed` expressions, and expand a template. */
export function expandTemplate(template: string, vars: Readonly<Record<string, string>>, allowed?: readonly string[]): string {
  const parts = parseTemplate(template);
  if (allowed) {
    for (const p of parts) {
      if ('expression' in p && !allowed.includes(p.expression))
        fail('template.expression', '§8', 'Expression not permitted for this field', { observed: `{${p.expression}}` });
    }
  }
  return expandParts(parts, vars);
}

/**
 * Validate a descriptor URL template: well-formed, only `allowed` expressions, all `required`
 * expressions present, and an absolute HTTP(S) URL after expansion (§8).
 */
export function checkUrlTemplate(
  template: string,
  allowed: readonly string[],
  required: readonly string[],
  requirement: string,
  path: string,
): TemplatePart[] {
  const parts = parseTemplate(template, requirement, path);
  const seen = new Set<string>();
  for (const p of parts) {
    if ('expression' in p) {
      if (!allowed.includes(p.expression))
        fail('template.expression', requirement, 'Template expression not permitted for this field', {
          path,
          observed: `{${p.expression}}`,
        });
      seen.add(p.expression);
    }
  }
  for (const r of required) if (!seen.has(r)) fail('template.missing_expression', requirement, `Template must contain {${r}}`, { path });
  const sample: Record<string, string> = {
    id: 'a'.repeat(64),
    id2: 'aa',
    path: 'files/a',
    sha256: 'a'.repeat(64),
    x: 'a'.repeat(43),
    file: 'revocation.json',
  };
  const expanded = expandParts(parts, sample);
  if (!isAbsoluteHttpUrl(expanded))
    fail('descriptor.url', requirement, 'URL must be an absolute HTTP(S) URL', { path, observed: template });
  return parts;
}
