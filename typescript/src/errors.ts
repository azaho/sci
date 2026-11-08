/**
 * Outcome vocabulary and structured error records.
 *
 * SCI §6 requires implementations to distinguish failed checks (`invalid`), unknown
 * schemas (`unsupported`), and checks that could not be completed (`incomplete`).
 */

export type Outcome = 'valid' | 'invalid' | 'unsupported' | 'incomplete';

/** A structured description of one failed or uncompleted check. */
export interface SciIssue {
  /** Stable machine-readable code, e.g. `json.duplicate_key`. */
  readonly code: string;
  /** Outcome class this issue implies. */
  readonly outcome: Exclude<Outcome, 'valid'>;
  /** SCI protocol section (or external rule) that the check enforces, e.g. `§3.1`. */
  readonly requirement: string;
  /** Location inside the input: a JSON pointer, a bundle path, or a byte offset. */
  readonly path?: string;
  /** A short rendering of the observed offending value, when safe to report. */
  readonly observed?: string;
  /** Human-readable explanation. */
  readonly message: string;
}

/** Result of a check that yields a value on success. */
export type CheckResult<T> =
  | { readonly outcome: 'valid'; readonly value: T; readonly issues: readonly SciIssue[] }
  | { readonly outcome: Exclude<Outcome, 'valid'>; readonly issues: readonly SciIssue[] };

/** Thrown internally by validators; converted to a {@link CheckResult} at API boundaries. */
export class SciError extends Error {
  readonly issue: SciIssue;
  constructor(issue: SciIssue) {
    super(issue.message);
    this.name = 'SciError';
    this.issue = issue;
  }
  get outcome(): Exclude<Outcome, 'valid'> {
    return this.issue.outcome;
  }
}

interface IssueInput {
  readonly path?: string | undefined;
  readonly observed?: unknown;
}

function render(observed: unknown): string | undefined {
  if (observed === undefined) return undefined;
  let text: string;
  if (typeof observed === 'string') text = JSON.stringify(observed);
  else if (typeof observed === 'number' || typeof observed === 'boolean' || typeof observed === 'bigint') text = String(observed);
  else if (observed === null) text = 'null';
  else if (observed instanceof Uint8Array) text = `${observed.length} bytes`;
  else if (Array.isArray(observed)) text = `array(${observed.length})`;
  else text = typeof observed;
  return text.length > 120 ? `${text.slice(0, 117)}...` : text;
}

function makeIssue(
  outcome: Exclude<Outcome, 'valid'>,
  code: string,
  requirement: string,
  message: string,
  extra: IssueInput = {},
): SciIssue {
  const observed = render(extra.observed);
  return {
    code,
    outcome,
    requirement,
    message,
    ...(extra.path !== undefined ? { path: extra.path } : {}),
    ...(observed !== undefined ? { observed } : {}),
  };
}

/** Throw an `invalid` issue. */
export function fail(code: string, requirement: string, message: string, extra?: IssueInput): never {
  throw new SciError(makeIssue('invalid', code, requirement, message, extra));
}

/** Throw an `unsupported` issue. */
export function unsupported(code: string, requirement: string, message: string, extra?: IssueInput): never {
  throw new SciError(makeIssue('unsupported', code, requirement, message, extra));
}

/** Throw an `incomplete` issue (unavailable data or a local limit). */
export function incomplete(code: string, requirement: string, message: string, extra?: IssueInput): never {
  throw new SciError(makeIssue('incomplete', code, requirement, message, extra));
}

/** Assert a condition, throwing an `invalid` issue otherwise. */
export function check(condition: unknown, code: string, requirement: string, message: string, extra?: IssueInput): asserts condition {
  if (!condition) fail(code, requirement, message, extra);
}

export function issueFrom(
  outcome: Exclude<Outcome, 'valid'>,
  code: string,
  requirement: string,
  message: string,
  extra?: IssueInput,
): SciIssue {
  return makeIssue(outcome, code, requirement, message, extra);
}

/** Convert any thrown value into an issue. Unknown exceptions are reported as `invalid`. */
export function toIssue(error: unknown): SciIssue {
  if (error instanceof SciError) return error.issue;
  const message = error instanceof Error ? error.message : String(error);
  return makeIssue('invalid', 'internal.unexpected', '§6', `Unexpected verification error: ${message}`);
}

/** Run a synchronous validator and capture its outcome. */
export function capture<T>(fn: () => T): CheckResult<T> {
  try {
    return { outcome: 'valid', value: fn(), issues: [] };
  } catch (error) {
    const issue = toIssue(error);
    return { outcome: issue.outcome, issues: [issue] };
  }
}

/** Run an asynchronous validator and capture its outcome. */
export async function captureAsync<T>(fn: () => Promise<T>): Promise<CheckResult<T>> {
  try {
    return { outcome: 'valid', value: await fn(), issues: [] };
  } catch (error) {
    const issue = toIssue(error);
    return { outcome: issue.outcome, issues: [issue] };
  }
}

/** Unwrap a valid result or rethrow its first issue. */
export function unwrap<T>(result: CheckResult<T>): T {
  if (result.outcome === 'valid') return result.value;
  const issue = result.issues[0] ?? makeIssue(result.outcome, 'internal.no_issue', '§6', 'Check did not succeed');
  throw new SciError(issue);
}
