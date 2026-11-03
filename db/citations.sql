-- SCI citations: target normalization, ordered citations, stored citation
-- counts on publications, and the cached catalog totals.

-- Distinct accepted publications citing this one. Derived from sci.citations
-- by sci.refresh_citation_counts(); stored so feeds can sort by it.
alter table sci.publications add column if not exists citing_publications bigint not null default 0;
create index if not exists publications_cited_idx
  on sci.publications (citing_publications desc, accepted_at desc, uuid desc) where state = 'accepted';

alter table sci.publications add column if not exists citing_publications_24h bigint not null default 0;
create index if not exists publications_cited_24h_idx
  on sci.publications (citing_publications_24h desc, accepted_at desc, uuid desc) where state = 'accepted';
alter table sci.publications add column if not exists citing_publications_48h bigint not null default 0;
create index if not exists publications_cited_48h_idx
  on sci.publications (citing_publications_48h desc, accepted_at desc, uuid desc) where state = 'accepted';
alter table sci.publications add column if not exists citing_publications_7d bigint not null default 0;
create index if not exists publications_cited_7d_idx
  on sci.publications (citing_publications_7d desc, accepted_at desc, uuid desc) where state = 'accepted';

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

-- One shared snapshot for the site header; empty until the first worker refresh.
create table if not exists sci.catalog_totals (
  id boolean primary key default true check (id),
  publications bigint not null check (publications >= 0),
  citations bigint not null check (citations >= 0),
  updated_at timestamptz not null
);

-- Recompute stored citation counts and catalog totals; returns the number of
-- rows changed. The maintenance worker refreshes these about every five minutes.
-- All-time counts match catalog_v1.citation_counts for sci: targets. Windows
-- use the citing publication's acceptance time, not the target's age.
create or replace function sci.refresh_citation_counts()
returns bigint
language sql
set search_path = pg_catalog
as $$
  insert into sci.catalog_totals (id, publications, citations, updated_at)
  select true,
    (select count(*) from sci.publications where state = 'accepted'),
    (select count(distinct (c.source_uuid, c.target_key))
     from sci.citations c
     join sci.publications p on p.uuid = c.source_uuid and p.state = 'accepted'),
    now()
  on conflict (id) do update set publications = excluded.publications,
    citations = excluded.citations, updated_at = excluded.updated_at;

  with counts as (
    select substr(c.target_key, 5)::uuid as uuid, count(distinct c.source_uuid) as n,
      count(distinct c.source_uuid) filter (where s.accepted_at > now() - interval '24 hours' and s.accepted_at <= now()) as n24,
      count(distinct c.source_uuid) filter (where s.accepted_at > now() - interval '48 hours' and s.accepted_at <= now()) as n48,
      count(distinct c.source_uuid) filter (where s.accepted_at > now() - interval '168 hours' and s.accepted_at <= now()) as n7
    from sci.citations c
    join sci.publications s on s.uuid = c.source_uuid and s.state = 'accepted'
    where c.target_key like 'sci:%'
    group by c.target_key
  ), changed as (
    update sci.publications p set citing_publications = coalesce(counts.n, 0),
      citing_publications_24h = coalesce(counts.n24, 0),
      citing_publications_48h = coalesce(counts.n48, 0),
      citing_publications_7d = coalesce(counts.n7, 0)
    from sci.publications q left join counts on counts.uuid = q.uuid
    where p.uuid = q.uuid and p.state = 'accepted'
      and (p.citing_publications, p.citing_publications_24h, p.citing_publications_48h, p.citing_publications_7d)
          is distinct from (coalesce(counts.n, 0), coalesce(counts.n24, 0), coalesce(counts.n48, 0), coalesce(counts.n7, 0))
    returning 1
  )
  select count(*) from changed
$$;

-- Row-level security and grants.
alter table sci.citations enable row level security;
alter table sci.catalog_totals enable row level security;

drop policy if exists citations_public_read on sci.citations;
create policy citations_public_read on sci.citations for select to anon, authenticated
  using (exists (select 1 from sci.publications p where p.uuid = source_uuid and p.state = 'accepted'));
drop policy if exists citations_publisher on sci.citations;
create policy citations_publisher on sci.citations for all to sci_publisher using (true) with check (true);
drop policy if exists catalog_totals_public_read on sci.catalog_totals;
create policy catalog_totals_public_read on sci.catalog_totals for select to anon, authenticated using (true);
drop policy if exists catalog_totals_publisher on sci.catalog_totals;
create policy catalog_totals_publisher on sci.catalog_totals for all to sci_publisher using (true) with check (true);

revoke all on sci.citations, sci.catalog_totals from public, anon, authenticated;
revoke all on function sci.citation_target_key(text), sci.refresh_citation_counts() from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke all on sci.citations, sci.catalog_totals from service_role';
  end if;
end
$$;

-- The citation count columns belong to this file; publications.sql grants the rest.
grant select (citing_publications, citing_publications_24h, citing_publications_48h, citing_publications_7d)
  on sci.publications to anon, authenticated;
grant select (source_uuid, position, target, target_key, relation, reason) on sci.citations to anon, authenticated;
grant select on sci.catalog_totals to anon, authenticated;
grant select, insert, update on sci.citations, sci.catalog_totals to sci_publisher;
grant execute on function sci.citation_target_key(text) to anon, authenticated, sci_publisher;
grant execute on function sci.refresh_citation_counts() to sci_publisher;
