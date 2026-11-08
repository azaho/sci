/** RFC 3339 date-time parsing with exact comparison (no floating-point rounding). */

export interface Instant {
  /** Whole seconds since the Unix epoch (UTC). */
  readonly seconds: number;
  /** Fractional second as nanoseconds, 0..999_999_999. */
  readonly nanos: number;
}

const RFC3339 = /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:([Zz])|([+-])(\d{2}):(\d{2}))$/;

function daysInMonth(y: number, m: number): number {
  if (m === 2) return (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0 ? 29 : 28;
  return [4, 6, 9, 11].includes(m) ? 30 : 31;
}

/**
 * Parse an RFC 3339 §5.6 `date-time`. Returns null for invalid syntax or field values.
 * A leap second (`:60`) is accepted and treated as the last instant before the next minute.
 */
export function parseRfc3339(text: string): Instant | null {
  const m = RFC3339.exec(text);
  if (!m) return null;
  const [y, mo, d, h, mi, s] = [m[1], m[2], m[3], m[4], m[5], m[6]].map((x) => Number(x)) as [
    number,
    number,
    number,
    number,
    number,
    number,
  ];
  if (mo < 1 || mo > 12 || d < 1 || d > daysInMonth(y, mo) || h > 23 || mi > 59 || s > 60) return null;
  let offset = 0;
  if (m[9] !== undefined) {
    const oh = Number(m[10]);
    const om = Number(m[11]);
    if (oh > 23 || om > 59) return null;
    offset = (oh * 60 + om) * 60 * (m[9] === '-' ? -1 : 1);
  }
  const frac = m[7] ?? '';
  let nanos = Number(`${frac}000000000`.slice(0, 9));
  // Digits beyond nanosecond precision only matter if nonzero; round up conservatively.
  if (frac.length > 9 && /[1-9]/.test(frac.slice(9))) nanos += 1;
  let sec = s;
  if (s === 60) {
    sec = 59;
    nanos = 999_999_999;
  }
  // Date.UTC maps years 0..99 to 1900..1999, so set the full year explicitly.
  const date = new Date(0);
  date.setUTCFullYear(y, mo - 1, d);
  date.setUTCHours(h, mi, sec, 0);
  const base = date.getTime() / 1000;
  if (!Number.isFinite(base)) return null;
  let seconds = base - offset;
  if (nanos > 999_999_999) {
    seconds += 1;
    nanos -= 1_000_000_000;
  }
  return { seconds, nanos };
}

export function instantFromDate(date: Date): Instant {
  const ms = date.getTime();
  const seconds = Math.floor(ms / 1000);
  return { seconds, nanos: (ms - seconds * 1000) * 1_000_000 };
}

export function compareInstants(a: Instant, b: Instant): number {
  if (a.seconds !== b.seconds) return a.seconds < b.seconds ? -1 : 1;
  return a.nanos === b.nanos ? 0 : a.nanos < b.nanos ? -1 : 1;
}

/** Format a Date as RFC 3339 UTC with a trailing `Z`, omitting zero milliseconds. */
export function formatUtc(date: Date): string {
  const iso = date.toISOString();
  return iso.endsWith('.000Z') ? `${iso.slice(0, -5)}Z` : iso;
}
