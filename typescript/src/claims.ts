/** SCI §5.2 external identity claims, §5.3 key revocation, §6.6 and §6.7 checks. */
import { type Budget, resolveBudget } from './budget.ts';
import { type PublicJwk, parsePublicJwk, verifyEd25519 } from './ed25519.ts';
import { check, fail, incomplete } from './errors.ts';
import { isValidClaimDomain } from './idna.ts';
import { checkFields, checkRecordSchema, SCHEMAS } from './ids.ts';
import { isObject, type JsonValue, parseJsonBytes } from './json.ts';
import { checkText } from './text.ts';
import { compareInstants, type Instant, instantFromDate, parseRfc3339 } from './time.ts';

export interface Account {
  readonly type: string;
  readonly id: string;
}

export type AccountMethod = 'domain' | 'github' | 'huggingface';

export interface Claim {
  readonly publicKey: PublicJwk;
  readonly publicKeyBytes: Uint8Array;
  readonly account: Account;
  readonly note?: string;
  readonly name?: string;
  readonly expires: string;
  readonly expiresAt: Instant;
  readonly bytes: Uint8Array;
}

export interface Revocation {
  readonly publicKey: PublicJwk;
  readonly publicKeyBytes: Uint8Array;
  readonly bytes: Uint8Array;
}

/** Parse an account object `{type, id}` with exactly two string fields (§5.2.1). */
export function parseAccount(value: JsonValue | undefined, path = '/account', requirement = '§5.2.1'): Account {
  if (!isObject(value)) fail('claim.account_type', requirement, 'account must be an object', { path });
  checkFields(value, ['type', 'id'], [], requirement, path);
  const type = value.get('type');
  const id = value.get('id');
  check(
    typeof type === 'string' && typeof id === 'string',
    'claim.account_strings',
    requirement,
    'account.type and account.id must be strings',
    { path },
  );
  return { type, id };
}

/** Validate an account against its method's rules (§5.2.2–§5.2.4). */
export function checkAccountMethod(account: Account): AccountMethod {
  switch (account.type) {
    case 'domain':
      check(
        isValidClaimDomain(account.id),
        'claim.domain',
        '§5.2.2',
        'account.id must be a lowercase ASCII DNS host name with valid IDNA2008 A-labels, no final dot, and not an IP address',
        {
          observed: account.id,
        },
      );
      return 'domain';
    case 'github':
      check(/^[a-z0-9](?:[a-z0-9-]{0,38})$/.test(account.id), 'claim.github', '§5.2.3', 'account.id must be a lowercase GitHub login', {
        observed: account.id,
      });
      return 'github';
    case 'huggingface':
      check(
        /^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$/.test(account.id) && !account.id.includes('..'),
        'claim.huggingface',
        '§5.2.4',
        'account.id must be a Hugging Face user or organization name',
        {
          observed: account.id,
        },
      );
      return 'huggingface';
    default:
      return fail('claim.unknown_method', '§5.2', 'Unknown account method', { observed: account.type });
  }
}

export interface ClaimLocation {
  readonly json: string;
  readonly sig: string;
}

/**
 * Hosting locations for a key claim (§5.2.2–§5.2.4), in the order they should be tried.
 * The account must already be valid for its method.
 */
export function claimLocations(account: Account, x: string): ClaimLocation[] {
  const method = checkAccountMethod(account);
  const make = (base: string): ClaimLocation => ({ json: `${base}${x}.json`, sig: `${base}${x}.json.sig` });
  switch (method) {
    case 'domain':
      return [make(`https://${account.id}/.well-known/sci/keys/`)];
    case 'github':
      return [
        make(`https://raw.githubusercontent.com/${account.id}/${account.id}/main/sci/keys/`),
        make(`https://raw.githubusercontent.com/${account.id}/.github/main/sci/keys/`),
      ];
    case 'huggingface':
      return [make(`https://huggingface.co/spaces/${account.id}/README/resolve/main/sci/keys/`)];
  }
}

/** Parse and validate claim bytes (§5.2.1, §7) and verify its signature (§5.1). */
export function parseClaim(bytes: Uint8Array, signature: Uint8Array, budget?: Partial<Budget>): Claim {
  const b = resolveBudget(budget);
  if (bytes.length > b.maxJsonBytes) incomplete('budget.json_bytes', '§6', 'Claim exceeds the local size budget');
  const value = parseJsonBytes(bytes, { maxDepth: b.maxJsonDepth, path: 'claim' });
  const obj = checkRecordSchema(value, SCHEMAS.claim, '§5.2.1', 'claim');
  checkFields(obj, ['schema', 'public_key', 'account', 'expires'], ['note', 'name'], '§5.2.1');
  const { jwk, bytes: keyBytes } = parsePublicJwk(obj.get('public_key'), '/public_key');
  const account = parseAccount(obj.get('account'));
  let note: string | undefined;
  if (obj.has('note')) {
    const n = obj.get('note');
    if (typeof n !== 'string') fail('claim.note', '§5.2.1', 'note must be a string', { path: '/note' });
    checkText(n, { controls: true, nonblank: true, maxCodePoints: 2500, path: '/note', requirement: '§5.2.1, §4.2' });
    note = n;
  }
  let name: string | undefined;
  if (obj.has('name')) {
    const n = obj.get('name');
    if (typeof n !== 'string') fail('claim.name', '§5.2.1', 'name must be a string', { path: '/name' });
    checkText(n, { nonblank: true, maxCodePoints: 80, path: '/name', requirement: '§5.2.1' });
    name = n;
  }
  const expires = obj.get('expires');
  if (typeof expires !== 'string') fail('claim.expires', '§5.2.1', 'expires must be a string', { path: '/expires' });
  const expiresAt = parseRfc3339(expires);
  if (!expiresAt) fail('claim.expires', '§5.2.1', 'expires must be an RFC 3339 date-time', { path: '/expires', observed: expires });
  verifyEd25519(keyBytes, bytes, signature, 'claim.json.sig');
  return {
    publicKey: jwk,
    publicKeyBytes: keyBytes,
    account,
    ...(note !== undefined ? { note } : {}),
    ...(name !== undefined ? { name } : {}),
    expires,
    expiresAt,
    bytes,
  };
}

export interface ClaimCheckOptions {
  /** Expected key, as the canonical JWK `x` value. */
  readonly expectedPublicKeyX: string;
  /** The account the association is being checked for. */
  readonly requestedAccount: Account;
  /** Current time. */
  readonly now: Date;
  /** True if a verified revocation for this key is known (§6.6). */
  readonly knownRevocation: boolean;
  /**
   * True if the bytes were retrieved from the account method's hosting location (§5.2, §6.6).
   * Bytes from any other source cannot establish an association.
   */
  readonly retrievedFromAccountLocation: boolean;
  readonly budget?: Partial<Budget>;
}

/** §6.6 key association checks over already retrieved claim bytes. */
export function verifyClaim(bytes: Uint8Array, signature: Uint8Array, options: ClaimCheckOptions): Claim {
  const claim = parseClaim(bytes, signature, options.budget);
  check(claim.publicKey.x === options.expectedPublicKeyX, 'claim.key_mismatch', '§6.6', 'Claim key differs from the expected key', {
    observed: claim.publicKey.x,
  });
  check(
    claim.account.type === options.requestedAccount.type && claim.account.id === options.requestedAccount.id,
    'claim.account_mismatch',
    '§6.6',
    'Claim account differs from the requested account',
    { observed: `${claim.account.type}:${claim.account.id}` },
  );
  checkAccountMethod(claim.account);
  check(compareInstants(instantFromDate(options.now), claim.expiresAt) < 0, 'claim.expired', '§6.6', 'Current time is not before expires', {
    observed: claim.expires,
  });
  check(!options.knownRevocation, 'claim.key_revoked', '§5.3, §6.6', 'The key has a known verified revocation');
  check(options.retrievedFromAccountLocation, 'claim.not_hosted', '§5.2, §6.6', 'Claim was not retrieved from the account location');
  return claim;
}

/** §6.7 revocation checks. If `expectedPublicKeyX` is given, the record must name that key (§8.1.2). */
export function verifyRevocation(
  bytes: Uint8Array,
  signature: Uint8Array,
  options: { readonly expectedPublicKeyX?: string; readonly budget?: Partial<Budget> } = {},
): Revocation {
  const b = resolveBudget(options.budget);
  if (bytes.length > b.maxJsonBytes) incomplete('budget.json_bytes', '§6', 'Revocation exceeds the local size budget');
  const value = parseJsonBytes(bytes, { maxDepth: b.maxJsonDepth, path: 'revocation.json' });
  const obj = checkRecordSchema(value, SCHEMAS.revocation, '§5.3', 'revocation.json');
  checkFields(obj, ['schema', 'public_key'], [], '§5.3');
  const { jwk, bytes: keyBytes } = parsePublicJwk(obj.get('public_key'), '/public_key');
  verifyEd25519(keyBytes, bytes, signature, 'revocation.json.sig');
  if (options.expectedPublicKeyX !== undefined) {
    check(jwk.x === options.expectedPublicKeyX, 'revocation.key_mismatch', '§8.1.2', 'Revocation names a different key', {
      observed: jwk.x,
    });
  }
  return { publicKey: jwk, publicKeyBytes: keyBytes, bytes };
}
