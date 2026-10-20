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
  p.search_document
from sci.publications p
where p.state = 'accepted';

comment on view catalog_v1.publications is
  'One row per accepted publication. Order feeds by accepted_at.desc,uuid.desc and page with keyset filters.';
comment on column catalog_v1.publications.search_document is
  'tsvector over title, keywords (weight A) and abstract (weight B), configuration english. Filter with search_document=wfts(english).<words>.';
comment on column catalog_v1.publications.objects is
  'Map from file name to its original archive URL. Verify with sci pull; the acceptance record is not included.';
comment on column catalog_v1.publications.created_at is 'Author-claimed creation time from the signed manifest.';
comment on column catalog_v1.publications.accepted_at is 'Time this instance accepted the publication.';

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

create or replace function catalog_v1.normalize_citation_target(target text)
returns text
language sql immutable strict parallel safe
set search_path = pg_catalog
as $$ select sci.citation_target_key(target) $$;

comment on function catalog_v1.normalize_citation_target(text) is
  'Validate a citation target in its original spelling and return the target_key used for matching.';

revoke all on all tables in schema catalog_v1 from public, anon, authenticated;
revoke all on all functions in schema catalog_v1 from public;
grant select on catalog_v1.publications, catalog_v1.citations, catalog_v1.citation_counts to anon, authenticated;
grant execute on function catalog_v1.normalize_citation_target(text) to anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke all on all tables in schema catalog_v1 from service_role';
  end if;
end
$$;

-- Ask PostgREST to reload its schema cache.
notify pgrst, 'reload schema';
