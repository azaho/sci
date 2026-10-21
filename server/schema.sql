-- SCI internal schema: tables, indexes, row policies, and grants.
--
-- This file is idempotent. The migration runner (python -m server.database
-- migrate) applies it, then catalog.sql, then admin.sql, in one transaction.
-- Never drop or reset data here; add new structures with IF NOT EXISTS.

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

create table if not exists sci.publications (
  uuid uuid primary key,
  manifest_sha256 text not null check (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  state text not null check (state in ('finalizing', 'accepted', 'failed')),
  failure_reason text,
  author_fingerprint text not null references sci.identities (fingerprint),
  author_public_key text not null,
  title text not null,
  abstract text not null,
  categories text[] not null default '{}',
  keywords text[] not null default '{}',
  headline_figure_caption text,
  created_at timestamptz not null,          -- author's claimed creation time
  files jsonb not null,                     -- signed manifest "files" map
  files_tree jsonb,                         -- signed publisher preview
  object_prefix text not null,              -- publications/<uuid>/ in the archive
  accepted_at timestamptz not null,         -- committed by the finalization claim
  upload_id uuid,
  state_changed_at timestamptz not null default now(),
  check ((state = 'failed') = (failure_reason is not null))
);

-- Full-text search over title, abstract, and keywords (configuration: english).
create or replace function sci.search_document(title text, abstract text, keywords text[])
returns tsvector
language sql immutable parallel safe
set search_path = pg_catalog
as $$
  select setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A')
      || setweight(to_tsvector('english'::regconfig, array_to_string(coalesce(keywords, '{}'), ' ')), 'A')
      || setweight(to_tsvector('english'::regconfig, coalesce(abstract, '')), 'B')
$$;

alter table sci.publications
  add column if not exists search_document tsvector
  generated always as (sci.search_document(title, abstract, keywords)) stored;

create index if not exists publications_feed_idx
  on sci.publications (accepted_at desc, uuid desc) where state = 'accepted';
create index if not exists publications_author_idx on sci.publications (author_fingerprint, accepted_at desc);
create index if not exists publications_categories_idx on sci.publications using gin (categories);
create index if not exists publications_search_idx on sci.publications using gin (search_document);
create index if not exists publications_finalizing_idx on sci.publications (uuid) where state = 'finalizing';

-- Distinct accepted publications citing this one. Derived from sci.citations
-- by sci.refresh_citation_counts(); stored so feeds can sort by it.
alter table sci.publications add column if not exists citing_publications bigint not null default 0;
create index if not exists publications_cited_idx
  on sci.publications (citing_publications desc, accepted_at desc, uuid desc) where state = 'accepted';

-- Citation target keys (DESIGN.md "Citations"). The Python implementation
-- is sci.publication.normalize_citation_target; both are tested against
-- tests/fixtures/citation_targets.json.
create or replace function sci.citation_target_key(target text)
returns text
language plpgsql immutable strict parallel safe
set search_path = pg_catalog
as $$
declare
  safe constant text := 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&''()*+,;=:@/';
  path text;
  raw bytea := ''::bytea;
  folded bytea;
  name text;
  ch text;
  hx text;
  cp integer;
  b integer;
  i integer;
  n integer;
  result text := '';
begin
  if length(target) > 512 then
    raise exception 'citation target has % characters; the limit is 512 ASCII characters', length(target)
      using errcode = '22023';
  end if;
  for i in 1 .. length(target) loop
    if ascii(substr(target, i, 1)) > 127 then
      raise exception 'citation target must contain only ASCII characters; percent-encode other characters'
        using errcode = '22023';
    end if;
  end loop;

  if left(target, 4) = 'sci:' then
    if substr(target, 5) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' then
      return target;
    end if;
    raise exception 'a sci: target must be sci: followed by a canonical lowercase UUIDv4'
      using errcode = '22023';
  end if;

  if translate(left(target, 4), 'DOI', 'doi') <> 'doi:' then
    raise exception 'citation target must start with sci: or doi:' using errcode = '22023';
  end if;

  path := substr(target, 5);
  n := length(path);
  i := 1;
  while i <= n loop
    ch := substr(path, i, 1);
    if ch = '%' then
      hx := substr(path, i + 1, 2);
      if hx !~ '^[0-9A-Fa-f]{2}$' then
        raise exception 'citation target contains an invalid percent-encoding' using errcode = '22023';
      end if;
      raw := raw || decode(hx, 'hex');
      i := i + 3;
    elsif ch = '?' or ch = '#' then
      raise exception 'a doi: target must not contain a query or fragment' using errcode = '22023';
    elsif strpos(safe, ch) = 0 then
      raise exception 'citation target contains a character that must be percent-encoded' using errcode = '22023';
    else
      raw := raw || convert_to(ch, 'UTF8');
      i := i + 1;
    end if;
  end loop;

  begin
    name := convert_from(raw, 'UTF8');
  exception when others then
    raise exception 'percent-encoded bytes in the DOI name are not valid UTF-8' using errcode = '22023';
  end;

  for i in 1 .. length(name) loop
    cp := ascii(substr(name, i, 1));
    if cp < 32 or (cp between 127 and 159) or cp in (8232, 8233)
       or (cp between 64976 and 65007) or (cp & 65534) = 65534 then
      raise exception 'DOI name contains a forbidden character U+%', upper(to_hex(cp)) using errcode = '22023';
    end if;
  end loop;

  if name !~ '^10\.[0-9]+(\.[0-9]+)*/.+$' then
    raise exception 'not a DOI name: expected 10.<registrant>/<suffix>' using errcode = '22023';
  end if;

  folded := convert_to(translate(name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'UTF8');
  for i in 0 .. length(folded) - 1 loop
    b := get_byte(folded, i);
    if b < 128 and strpos(safe, chr(b)) > 0 then
      result := result || chr(b);
    else
      result := result || '%' || upper(lpad(to_hex(b), 2, '0'));
    end if;
  end loop;
  result := 'doi:' || result;
  if octet_length(result) > 512 then
    raise exception 'normalized citation key exceeds 512 bytes' using errcode = '22023';
  end if;
  return result;
end
$$;

create table if not exists sci.citations (
  source_uuid uuid not null references sci.publications (uuid) on delete cascade,
  position integer not null check (position >= 0),
  target text collate "C" not null,
  target_key text collate "C" not null,
  relation text check (relation in ('uses', 'supports', 'reproduces', 'challenges', 'supersedes')),
  reason text,
  primary key (source_uuid, position),
  -- Python and SQL normalizers must agree for every stored citation.
  constraint citations_target_key_matches check (target_key = sci.citation_target_key(target))
);
create index if not exists citations_target_key_idx on sci.citations (target_key, source_uuid, position);

-- Recompute stored citation counts from sci.citations; returns the number of
-- rows changed. The maintenance worker runs it, so counts lag by at most one
-- worker interval. Matches catalog_v1.citation_counts for sci: targets.
create or replace function sci.refresh_citation_counts()
returns bigint
language sql
set search_path = pg_catalog
as $$
  with counts as (
    select substr(c.target_key, 5)::uuid as uuid, count(distinct c.source_uuid) as n
    from sci.citations c
    join sci.publications s on s.uuid = c.source_uuid and s.state = 'accepted'
    where c.target_key like 'sci:%'
    group by c.target_key
  ), changed as (
    update sci.publications p set citing_publications = coalesce(counts.n, 0)
    from sci.publications q left join counts on counts.uuid = q.uuid
    where p.uuid = q.uuid and p.state = 'accepted'
      and p.citing_publications <> coalesce(counts.n, 0)
    returning 1
  )
  select count(*) from changed
$$;

create table if not exists sci.uploads (
  upload_id uuid primary key,
  publication_uuid uuid not null,
  manifest_sha256 text not null check (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  author_fingerprint text not null,
  temp_prefix text not null,
  files jsonb not null,
  state text not null check (state in ('pending', 'committed', 'failed', 'expired')),
  failure_reason text,
  deadline timestamptz not null,
  source_etags jsonb,
  outcome jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  temp_cleaned_at timestamptz
);
-- One active upload attempt per publication UUID.
create unique index if not exists uploads_one_active_idx on sci.uploads (publication_uuid) where state = 'pending';
create index if not exists uploads_cleanup_idx on sci.uploads (state, deadline) where temp_cleaned_at is null;

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

-- Row-level security: public roles see accepted records only.
alter table sci.settings enable row level security;
alter table sci.identities enable row level security;
alter table sci.publications enable row level security;
alter table sci.citations enable row level security;
alter table sci.uploads enable row level security;
alter table sci.schema_migrations enable row level security;

drop policy if exists settings_public_read on sci.settings;
create policy settings_public_read on sci.settings for select to anon, authenticated using (true);
drop policy if exists publications_public_read on sci.publications;
create policy publications_public_read on sci.publications for select to anon, authenticated
  using (state = 'accepted');
drop policy if exists citations_public_read on sci.citations;
create policy citations_public_read on sci.citations for select to anon, authenticated
  using (exists (select 1 from sci.publications p where p.uuid = source_uuid and p.state = 'accepted'));

drop policy if exists settings_publisher on sci.settings;
create policy settings_publisher on sci.settings for all to sci_publisher using (true) with check (true);
drop policy if exists identities_publisher_read on sci.identities;
create policy identities_publisher_read on sci.identities for select to sci_publisher using (true);
drop policy if exists publications_publisher on sci.publications;
create policy publications_publisher on sci.publications for all to sci_publisher using (true) with check (true);
drop policy if exists citations_publisher on sci.citations;
create policy citations_publisher on sci.citations for all to sci_publisher using (true) with check (true);
drop policy if exists uploads_publisher on sci.uploads;
create policy uploads_publisher on sci.uploads for all to sci_publisher using (true) with check (true);

-- Grants. Public roles get only the columns the catalog views need.
revoke all on all tables in schema sci from public, anon, authenticated;
revoke all on all functions in schema sci from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke all on all tables in schema sci from service_role';
  end if;
end
$$;

grant select (archive_url) on sci.settings to anon, authenticated;
grant select (uuid, manifest_sha256, state, author_fingerprint, author_public_key, title, abstract,
              categories, keywords, headline_figure_caption, created_at, files, files_tree,
              accepted_at, search_document, citing_publications)
  on sci.publications to anon, authenticated;
grant select (source_uuid, position, target, target_key, relation, reason) on sci.citations to anon, authenticated;

grant select, insert, update on sci.settings to sci_publisher;
grant select on sci.identities to sci_publisher;
grant select, insert, update on sci.publications, sci.citations, sci.uploads to sci_publisher;

grant execute on function sci.search_document(text, text, text[]) to anon, authenticated, sci_publisher;
grant execute on function sci.citation_target_key(text) to anon, authenticated, sci_publisher;
grant execute on function sci.lock_key(text, text) to sci_publisher;
grant execute on function sci.refresh_citation_counts() to sci_publisher;
