/**
 * SCI §4.2 text character rules using the RFC 9839 §4.3 "Unicode Assignables" intervals.
 * No runtime Unicode category data is consulted.
 */
import { utf8DecodeStrict } from './bytes.ts';
import { check, fail } from './errors.ts';

/**
 * RFC 9839 §4.3 Unicode Assignables, as a predicate over code points:
 *   %x9 / %xA / %xD / %x20-7E / %xA0-D7FF / %xE000-FDCF / %xFDF0-FFFD /
 *   %x10000-1FFFD / %x20000-2FFFD / ... / %x100000-10FFFD
 */
export function isUnicodeAssignable(cp: number): boolean {
  if (cp === 0x09 || cp === 0x0a || cp === 0x0d) return true;
  if (cp < 0x20) return false;
  if (cp <= 0x7e) return true;
  if (cp < 0xa0) return false;
  if (cp <= 0xd7ff) return true;
  if (cp < 0xe000) return false;
  if (cp <= 0xfdcf) return true;
  if (cp < 0xfdf0) return false;
  if (cp > 0x10ffff) return false;
  return (cp & 0xfffe) !== 0xfffe;
}

export interface TextRule {
  /** Permit TAB, LF, and CR (abstract, citation reason, note, README.md, LICENSE). */
  readonly controls?: boolean;
  /** Require at least one permitted character other than SPACE/TAB/LF/CR. */
  readonly nonblank?: boolean;
  /** Maximum number of Unicode code points. */
  readonly maxCodePoints?: number;
  readonly path?: string;
  readonly requirement?: string;
}

/** Count code points in a well-formed JS string. */
export function codePointLength(s: string): number {
  let n = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length) {
      const d = s.charCodeAt(i + 1);
      if (d >= 0xdc00 && d <= 0xdfff) i++;
    }
    n++;
  }
  return n;
}

/** Validate decoded text against §4.2. Throws `invalid` issues. */
export function checkText(value: string, rule: TextRule = {}): void {
  const requirement = rule.requirement ?? '§4.2';
  const path = rule.path;
  let count = 0;
  let nonblank = false;
  for (let i = 0; i < value.length; i++) {
    let cp = value.charCodeAt(i);
    if (cp >= 0xd800 && cp <= 0xdbff && i + 1 < value.length) {
      const d = value.charCodeAt(i + 1);
      if (d >= 0xdc00 && d <= 0xdfff) {
        cp = 0x10000 + ((cp - 0xd800) << 10) + (d - 0xdc00);
        i++;
      }
    }
    count++;
    if (cp === 0x09 || cp === 0x0a || cp === 0x0d) {
      if (!rule.controls) {
        fail('text.forbidden_control', requirement, 'TAB, LF, and CR are not permitted in this field', {
          path,
          observed: `U+${cp.toString(16).toUpperCase().padStart(4, '0')}`,
        });
      }
      continue;
    }
    if (!isUnicodeAssignable(cp)) {
      fail('text.not_assignable', requirement, 'Text contains a character outside RFC 9839 Unicode Assignables', {
        path,
        observed: `U+${cp.toString(16).toUpperCase().padStart(4, '0')}`,
      });
    }
    if (cp !== 0x20) nonblank = true;
  }
  if (rule.nonblank) check(nonblank, 'text.blank', requirement, 'Text must be nonblank', { path });
  if (rule.maxCodePoints !== undefined) {
    check(count <= rule.maxCodePoints, 'text.too_long', requirement, `Text exceeds ${rule.maxCodePoints} code points`, {
      path,
      observed: count,
    });
  }
}

/** Validate a JSON value as a string field under §4.2. */
export function checkTextField(value: unknown, rule: TextRule): string {
  if (typeof value !== 'string') {
    fail('metadata.type', rule.requirement ?? '§4', 'Expected a string', { path: rule.path, observed: value });
  }
  checkText(value, rule);
  return value;
}

/** Decode and validate README.md (nonblank) or LICENSE (may be blank) bytes. */
export function checkTextFile(bytes: Uint8Array, kind: 'README.md' | 'LICENSE'): string {
  const text = utf8DecodeStrict(bytes, '§4.2', kind);
  checkText(text, {
    controls: true,
    nonblank: kind === 'README.md',
    path: kind,
    requirement: kind === 'README.md' ? '§2, §4.2' : '§4.2',
  });
  return text;
}
