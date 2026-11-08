/**
 * Pure index policy state for the protocol rules an indexer must apply across events:
 * - §5.3: a verified revocation is permanent, blocks new catalog admission, keeps existing
 *   entries (marked revoked), and survives the source disappearing. Timestamps do not override it.
 * - §6.5: timestamp acquisition failure never blocks or reverses admission.
 * - §8.1.2/§8.1.3: a 404 lookup proves nothing (status stays `unknown`).
 * - §8.1.4: repeat submissions do not duplicate registrations; account hints are unsigned.
 *
 * The state is an immutable value; {@link applyIndexEvent} returns a new state. Services can
 * mirror it in their database transactions.
 */

export interface CatalogEntry {
  readonly id: string;
  readonly keyX: string;
}

export interface IndexPolicyState {
  readonly revokedKeys: ReadonlySet<string>;
  readonly catalog: ReadonlyMap<string, CatalogEntry>;
  /** Publication digest → registered descriptor URLs. */
  readonly registrations: ReadonlyMap<string, ReadonlySet<string>>;
  /** Publication digests with at least one retained verified timestamp. */
  readonly timestamps: ReadonlySet<string>;
  /** Verified associations as `x|type|id`. Only verified §6.6 checks add entries. */
  readonly associations: ReadonlySet<string>;
}

export const EMPTY_INDEX_STATE: IndexPolicyState = Object.freeze({
  revokedKeys: new Set<string>(),
  catalog: new Map<string, CatalogEntry>(),
  registrations: new Map<string, ReadonlySet<string>>(),
  timestamps: new Set<string>(),
  associations: new Set<string>(),
});

export type IndexEvent =
  | { readonly type: 'revocation_verified'; readonly keyX: string }
  | { readonly type: 'revocation_lookup_not_found'; readonly keyX: string }
  | { readonly type: 'revocation_source_removed'; readonly keyX: string }
  | { readonly type: 'admission_requested'; readonly id: string; readonly keyX: string }
  | { readonly type: 'timestamp_verified'; readonly id: string }
  | { readonly type: 'timestamp_unavailable'; readonly id: string }
  | { readonly type: 'timestamp_lookup_not_found'; readonly id: string }
  | { readonly type: 'location_registered'; readonly id: string; readonly descriptor: string }
  | { readonly type: 'association_hint'; readonly keyX: string; readonly account: { readonly type: string; readonly id: string } }
  | { readonly type: 'association_verified'; readonly keyX: string; readonly account: { readonly type: string; readonly id: string } };

export type AdmissionDecision =
  | { readonly decision: 'accept'; readonly existing: boolean }
  | { readonly decision: 'reject'; readonly reason: 'revoked_key' };

/** §5.3/§6.5: decide whether a publication may be newly admitted. Existing entries stay admitted. */
export function decideAdmission(state: IndexPolicyState, id: string, keyX: string): AdmissionDecision {
  if (state.catalog.has(id)) return { decision: 'accept', existing: true };
  if (state.revokedKeys.has(keyX)) return { decision: 'reject', reason: 'revoked_key' };
  return { decision: 'accept', existing: false };
}

export function keyStatus(state: IndexPolicyState, keyX: string): 'revoked' | 'unknown' {
  return state.revokedKeys.has(keyX) ? 'revoked' : 'unknown';
}

export function timestampStatus(state: IndexPolicyState, id: string): 'verified' | 'unknown' {
  return state.timestamps.has(id) ? 'verified' : 'unknown';
}

export function isAssociationVerified(
  state: IndexPolicyState,
  keyX: string,
  account: { readonly type: string; readonly id: string },
): boolean {
  return !state.revokedKeys.has(keyX) && state.associations.has(`${keyX}|${account.type}|${account.id}`);
}

export function locationCount(state: IndexPolicyState): number {
  let n = 0;
  for (const s of state.registrations.values()) n += s.size;
  return n;
}

export interface EventResult {
  readonly state: IndexPolicyState;
  readonly admission?: AdmissionDecision;
}

/** Apply one event and return the new state (and an admission decision for admission requests). */
export function applyIndexEvent(state: IndexPolicyState, event: IndexEvent): EventResult {
  switch (event.type) {
    case 'revocation_verified':
      return { state: { ...state, revokedKeys: new Set([...state.revokedKeys, event.keyX]) } };
    case 'revocation_lookup_not_found':
    case 'revocation_source_removed':
    case 'timestamp_unavailable':
    case 'timestamp_lookup_not_found':
    case 'association_hint':
      return { state };
    case 'admission_requested': {
      const admission = decideAdmission(state, event.id, event.keyX);
      if (admission.decision === 'accept' && !admission.existing) {
        const catalog = new Map(state.catalog);
        catalog.set(event.id, { id: event.id, keyX: event.keyX });
        return { state: { ...state, catalog }, admission };
      }
      return { state, admission };
    }
    case 'timestamp_verified':
      return { state: { ...state, timestamps: new Set([...state.timestamps, event.id]) } };
    case 'location_registered': {
      const registrations = new Map(state.registrations);
      registrations.set(event.id, new Set([...(state.registrations.get(event.id) ?? []), event.descriptor]));
      return { state: { ...state, registrations } };
    }
    case 'association_verified':
      return {
        state: { ...state, associations: new Set([...state.associations, `${event.keyX}|${event.account.type}|${event.account.id}`]) },
      };
  }
}
