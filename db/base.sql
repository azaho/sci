-- SCI base schema: the sci schema, roles, instance settings, the public-key
-- whitelist, and shared lock helpers.
--
-- Every file in db/ is idempotent. The migration runner (python -m db migrate)
-- applies them in the order of db.migrate.MIGRATION_FILES, in one transaction.
-- Never drop or reset data here; add new structures with IF NOT EXISTS. Each
-- file revokes and grants privileges on its own objects only.

create schema if not exists sci;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
  -- The publishing service role. The migration runner sets LOGIN and its
  -- password; it can never administer the whitelist.
  if not exists (select 1 from pg_roles where rolname = 'sci_publisher') then
    create role sci_publisher nologin;
  end if;
end
$$;

revoke all on schema sci from public;
grant usage on schema sci to sci_publisher, anon, authenticated;

create table if not exists sci.schema_migrations (
  name text primary key,
  sha256 text not null,
  applied_at timestamptz not null default now()
);

-- Instance configuration used by the public views (one row).
create table if not exists sci.settings (
  id boolean primary key default true check (id),
  instance_url text not null,
  archive_url text not null,
  updated_at timestamptz not null default now()
);

-- The public-key whitelist.
create table if not exists sci.identities (
  fingerprint text primary key check (fingerprint ~ '^ed25519:[0-9a-f]{64}$'),
  public_key text not null unique check (public_key ~ '^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$'),
  enabled boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Advisory lock keys shared by the service, recovery workers, and admin helpers.
create or replace function sci.lock_key(kind text, id text)
returns bigint
language sql immutable parallel safe
set search_path = pg_catalog
as $$ select ('x' || substr(md5('sci:' || kind || ':' || id), 1, 16))::bit(64)::bigint $$;

-- Every change to an identity takes an exclusive per-key lock; finalization
-- takes the shared lock before its whitelist check and holds it to commit.
create or replace function sci.identities_serialize()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  perform pg_advisory_xact_lock(sci.lock_key('identity', coalesce(new.fingerprint, old.fingerprint)));
  if tg_op = 'UPDATE' then
    new.updated_at := now();
  end if;
  return coalesce(new, old);
end
$$;

drop trigger if exists identities_serialize on sci.identities;
create trigger identities_serialize
  before update or delete on sci.identities
  for each row execute function sci.identities_serialize();

-- Row-level security and grants.
alter table sci.settings enable row level security;
alter table sci.identities enable row level security;
alter table sci.schema_migrations enable row level security;

drop policy if exists settings_public_read on sci.settings;
create policy settings_public_read on sci.settings for select to anon, authenticated using (true);
drop policy if exists settings_publisher on sci.settings;
create policy settings_publisher on sci.settings for all to sci_publisher using (true) with check (true);
drop policy if exists identities_publisher_read on sci.identities;
create policy identities_publisher_read on sci.identities for select to sci_publisher using (true);

revoke all on sci.schema_migrations, sci.settings, sci.identities from public, anon, authenticated;
revoke all on function sci.lock_key(text, text), sci.identities_serialize() from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke all on sci.schema_migrations, sci.settings, sci.identities from service_role';
  end if;
end
$$;

grant select (archive_url) on sci.settings to anon, authenticated;
grant select, insert, update on sci.settings to sci_publisher;
grant select on sci.identities to sci_publisher;
grant execute on function sci.lock_key(text, text) to sci_publisher;
