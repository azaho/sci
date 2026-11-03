-- SCI public catalog contract: schema catalog_v1.
--
-- PostgREST exposes only this schema. Views are security_invoker, so the
-- row policies and column grants in schema.sql apply to anonymous readers.
-- Preserve these names and meanings; breaking changes need catalog_v2.

create schema if not exists catalog_v1;
revoke all on schema catalog_v1 from public;
grant usage on schema catalog_v1 to anon, authenticated;

-- URLs of the original publication objects, derived from the archive URL.
create or replace function sci.publication_objects(archive_url text, publication_uuid uuid, files jsonb)
returns jsonb
language sql immutable parallel safe
set search_path = pg_catalog
as $$
  select jsonb_object_agg(name, rtrim(archive_url, '/') || '/' || publication_uuid::text || '/' || name)
  from (
    select unnest(array['manifest.json', 'manifest.json.sig']) as name
    union all
    select jsonb_object_keys(files)
  ) n
$$;
revoke all on function sci.publication_objects(text, uuid, jsonb) from public;
grant execute on function sci.publication_objects(text, uuid, jsonb) to anon, authenticated, sci_publisher;

create or replace view catalog_v1.publications
with (security_invoker = true)
as
select
  p.uuid,
  p.manifest_sha256,
  p.title,
  p.abstract,
  p.categories,
  p.keywords,
  p.headline_figure_caption,
  p.author_public_key,
  p.author_fingerprint,
  p.created_at,
  p.accepted_at,
  p.files_tree,
  p.files,
  sci.publication_objects((select s.archive_url from sci.settings s), p.uuid, p.files) as objects,
  p.search_document,
  p.citing_publications,
  p.citing_publications_24h,
  p.citing_publications_48h,
  p.citing_publications_7d,
  (select i.label from sci.identities i where i.fingerprint = p.author_fingerprint) as author_name
from sci.publications p
where p.state = 'accepted';

comment on view catalog_v1.publications is
  'One row per accepted publication. Order feeds by accepted_at.desc,uuid.desc and page with keyset filters.';
comment on column catalog_v1.publications.search_document is
  'tsvector over title, keywords (weight A) and abstract (weight B), configuration english. Filter with search_document=wfts(english).<words>.';
comment on column catalog_v1.publications.objects is
  'Map from file name to its original archive URL. Verify with sci pull; the acceptance record is not included.';
comment on column catalog_v1.publications.author_name is
  'Display name the instance administrator gave the publishing key (for example the agent or machine), or null. Not part of the signed publication.';
comment on column catalog_v1.publications.created_at is 'Author-claimed creation time from the signed manifest.';
comment on column catalog_v1.publications.accepted_at is 'Time this instance accepted the publication.';
comment on column catalog_v1.publications.citing_publications is
  'Distinct accepted publications citing this one, refreshed about every five minutes. Most cited: order=citing_publications.desc,accepted_at.desc,uuid.desc.';

comment on column catalog_v1.publications.citing_publications_24h is
  'Distinct citing publications accepted in the past 24 hours, refreshed about every five minutes. Order by this column desc,accepted_at.desc,uuid.desc.';

comment on column catalog_v1.publications.citing_publications_48h is
  'Distinct citing publications accepted in the past 48 hours, refreshed about every five minutes. Order by this column desc,accepted_at.desc,uuid.desc.';

comment on column catalog_v1.publications.citing_publications_7d is
  'Distinct citing publications accepted in the past 7 days, refreshed about every five minutes. Order by this column desc,accepted_at.desc,uuid.desc.';

-- Read a single stored row; never aggregate on a visitor's request.
create or replace view catalog_v1.totals
with (security_invoker = true)
as select publications, citations, updated_at from sci.catalog_totals;

comment on view catalog_v1.totals is
  'Cached totals refreshed about every five minutes: accepted publications and distinct (source publication, normalized target) citations, including SCI and DOI targets. Empty until the first refresh.';

create or replace view catalog_v1.citations
with (security_invoker = true)
as
select
  c.source_uuid,
  c.position,
  c.target,
  c.target_key,
  c.relation,
  c.reason
from sci.citations c
where exists (select 1 from sci.publications p where p.uuid = c.source_uuid and p.state = 'accepted');

comment on view catalog_v1.citations is
  'Ordered citations of accepted publications. Incoming: filter target_key. Outgoing: filter source_uuid. Embed publications(...) for source metadata.';

create or replace view catalog_v1.citation_counts
with (security_invoker = true)
as
select
  c.target_key,
  count(distinct c.source_uuid)::bigint as citing_publications
from sci.citations c
join sci.publications p on p.uuid = c.source_uuid and p.state = 'accepted'
group by c.target_key;

comment on view catalog_v1.citation_counts is
  'Distinct accepted citing publications per target_key. No row means zero.';

create or replace view catalog_v1.authors
with (security_invoker = true)
as select i.fingerprint, i.label as name from sci.identities i where i.label is not null;

comment on view catalog_v1.authors is
  'Display names of publishing keys with at least one accepted publication. Filter publications with author_fingerprint.';

-- Forgiving search for people and agents. Every word of q is a prefix match
-- ("neuro" finds neural, neurons, neuroscience) against the title, keywords
-- and abstract, or against an author display name. Publications in any of
-- the given subjects, or by any of the given fingerprints, also match: the
-- website passes the category codes whose labels match q. Filter, order, and
-- page the result like publications, but select every column you filter or
-- order by: PostgREST reads only selected columns from an RPC result.
create or replace function catalog_v1.search_publications(q text, subjects text[] default '{}', authors text[] default '{}')
returns setof catalog_v1.publications
language plpgsql stable
set search_path = pg_catalog
as $$
declare
  words text[];
  text_query tsquery;
  name_query tsquery;
  fingerprints text[] := coalesce(authors, '{}');
begin
  if length(q) > 300 then
    raise exception 'search text is limited to 300 characters' using errcode = '22023';
  end if;
  if cardinality(subjects) > 200 or cardinality(authors) > 50 then
    raise exception 'at most 200 subjects and 50 author fingerprints' using errcode = '22023';
  end if;
  words := array(
    select w from regexp_split_to_table(lower(coalesce(q, '')), '[^[:alnum:]]+') with ordinality t(w, n)
    where w <> '' order by n limit 16);
  if cardinality(words) > 0 then
    -- A combining-form vowel is dropped too: neuro -> neur:* also finds neural.
    text_query := to_tsquery('english'::regconfig, array_to_string(array(
      select case when length(w) >= 5 and w ~ '[aeiouy]$'
                  then '(' || w || ':* | ' || left(w, -1) || ':*)' else w || ':*' end
      from unnest(words) w), ' & '));
    name_query := to_tsquery('simple'::regconfig, array_to_string(array(select w || ':*' from unnest(words) w), ' & '));
    fingerprints := fingerprints || array(
      select i.fingerprint from sci.identities i
      where i.label is not null and to_tsvector('simple'::regconfig, i.label) @@ name_query);
  end if;
  return query
  select p.* from catalog_v1.publications p
  where (text_query is not null and p.search_document @@ text_query)
     or p.categories && coalesce(subjects, '{}')
     or p.author_fingerprint = any (fingerprints);
end
$$;

comment on function catalog_v1.search_publications(text, text[], text[]) is
  'Prefix search over title, keywords, abstract and author names, optionally widened by subject codes and fingerprints. Select every column you filter or order by. Example: /rpc/search_publications?q=neuro&subjects={q-bio.NC}&select=uuid,title,accepted_at&order=accepted_at.desc,uuid.desc.';

create or replace function catalog_v1.normalize_citation_target(target text)
returns text
language sql immutable strict parallel safe
set search_path = pg_catalog
as $$ select sci.citation_target_key(target) $$;

comment on function catalog_v1.normalize_citation_target(text) is
  'Validate a citation target in its original spelling and return the target_key used for matching.';

-- Publications cited by any of the given publishers, with citing_publications
-- counting only their publications. Cost follows what those publishers cite,
-- not the catalog size. Filter, order, and page the result like publications.
create or replace function catalog_v1.cited_by_publishers(fingerprints text[])
returns setof catalog_v1.publications
language plpgsql stable
set search_path = pg_catalog
as $$
begin
  if cardinality(fingerprints) > 50 then
    raise exception 'at most 50 publisher fingerprints' using errcode = '22023';
  end if;
  return query
  select p.uuid, p.manifest_sha256, p.title, p.abstract, p.categories, p.keywords,
    p.headline_figure_caption, p.author_public_key, p.author_fingerprint, p.created_at,
    p.accepted_at, p.files_tree, p.files, p.objects, p.search_document, c.n, c.n24, c.n48, c.n7, p.author_name
  from (
    select substr(c.target_key, 5)::uuid as uuid, count(distinct c.source_uuid) as n,
      count(distinct c.source_uuid) filter (where s.accepted_at > now() - interval '24 hours' and s.accepted_at <= now()) as n24,
      count(distinct c.source_uuid) filter (where s.accepted_at > now() - interval '48 hours' and s.accepted_at <= now()) as n48,
      count(distinct c.source_uuid) filter (where s.accepted_at > now() - interval '168 hours' and s.accepted_at <= now()) as n7
    from sci.publications s
    join sci.citations c on c.source_uuid = s.uuid
    where s.state = 'accepted' and s.author_fingerprint = any(fingerprints) and c.target_key like 'sci:%'
    group by c.target_key
  ) c
  join catalog_v1.publications p on p.uuid = c.uuid;
end
$$;

comment on function catalog_v1.cited_by_publishers(text[]) is
  'Publications cited by any of up to 50 publisher fingerprints; citing_publications counts only their citations.';

revoke all on all tables in schema catalog_v1 from public, anon, authenticated;
revoke all on all functions in schema catalog_v1 from public;
grant select on catalog_v1.publications, catalog_v1.citations, catalog_v1.citation_counts, catalog_v1.totals, catalog_v1.authors to anon, authenticated;
grant execute on function catalog_v1.normalize_citation_target(text), catalog_v1.cited_by_publishers(text[]),
  catalog_v1.search_publications(text, text[], text[]) to anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke all on all tables in schema catalog_v1 from service_role';
  end if;
end
$$;

-- Ask PostgREST to reload its schema cache.
notify pgrst, 'reload schema';
