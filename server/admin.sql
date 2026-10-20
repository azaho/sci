-- SCI whitelist administration helpers.
--
-- Run these as the database administrator (Supabase: the postgres role in
-- the SQL editor; locally: the database superuser). They are not exposed
-- through PostgREST, and no other role may execute them.
--
--   select * from sci.enable_key('<base64url public key>');
--   select * from sci.disable_key('<base64url public key>');
--   select * from sci.list_keys();
--
-- The key is the unpadded base64url encoding of the raw 32-byte Ed25519
-- public key: the manifest JWK "x" value, printed by `sci keygen`.

create or replace function sci.key_fingerprint(public_key text)
returns text
language plpgsql immutable strict
set search_path = pg_catalog
as $$
begin
  -- 43 characters encode 32 bytes; the last character must leave the two
  -- unused bits zero (canonical encoding).
  if public_key !~ '^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$' then
    raise exception 'expected the unpadded base64url encoding of a 32-byte Ed25519 public key (43 characters, as printed by sci keygen); never supply private key material'
      using errcode = '22023';
  end if;
  return 'ed25519:' || encode(sha256(decode(translate(public_key, '-_', '+/') || '=', 'base64')), 'hex');
end
$$;

create or replace function sci.enable_key(public_key text)
returns table (fingerprint text, public_key_base64url text, enabled boolean)
language plpgsql
set search_path = pg_catalog
as $$
declare
  fp text := sci.key_fingerprint(public_key);
begin
  insert into sci.identities as i (fingerprint, public_key, enabled)
  values (fp, enable_key.public_key, true)
  on conflict on constraint identities_pkey do update set enabled = true
  where i.enabled is distinct from true;
  return query select i.fingerprint, i.public_key, i.enabled from sci.identities i where i.fingerprint = fp;
end
$$;

create or replace function sci.disable_key(public_key text)
returns table (fingerprint text, public_key_base64url text, enabled boolean)
language plpgsql
set search_path = pg_catalog
as $$
declare
  fp text := sci.key_fingerprint(public_key);
begin
  if not exists (select 1 from sci.identities i where i.fingerprint = fp) then
    raise exception 'unknown key %: it is not on this server''s whitelist', fp using errcode = 'P0002';
  end if;
  update sci.identities i set enabled = false where i.fingerprint = fp and i.enabled;
  return query select i.fingerprint, i.public_key, i.enabled from sci.identities i where i.fingerprint = fp;
end
$$;

create or replace function sci.list_keys()
returns table (fingerprint text, public_key_base64url text, enabled boolean, created_at timestamptz, updated_at timestamptz)
language sql stable
set search_path = pg_catalog
as $$
  select i.fingerprint, i.public_key, i.enabled, i.created_at, i.updated_at
  from sci.identities i
  order by i.created_at, i.fingerprint
$$;

revoke all on function sci.key_fingerprint(text) from public;
revoke all on function sci.enable_key(text) from public;
revoke all on function sci.disable_key(text) from public;
revoke all on function sci.list_keys() from public;
