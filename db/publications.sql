-- SCI publications: accepted and finalizing publications, full-text search,
-- and upload attempts (DESIGN.md "Push: what happens").

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

-- Row-level security: public roles see accepted records only.
alter table sci.publications enable row level security;
alter table sci.uploads enable row level security;

drop policy if exists publications_public_read on sci.publications;
create policy publications_public_read on sci.publications for select to anon, authenticated
  using (state = 'accepted');
drop policy if exists publications_publisher on sci.publications;
create policy publications_publisher on sci.publications for all to sci_publisher using (true) with check (true);
drop policy if exists uploads_publisher on sci.uploads;
create policy uploads_publisher on sci.uploads for all to sci_publisher using (true) with check (true);

-- Public roles get only the columns the catalog views need.
revoke all on sci.publications, sci.uploads from public, anon, authenticated;
revoke all on function sci.search_document(text, text, text[]) from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke all on sci.publications, sci.uploads from service_role';
  end if;
end
$$;

grant select (uuid, manifest_sha256, state, author_fingerprint, author_public_key, title, abstract,
              categories, keywords, headline_figure_caption, created_at, files, files_tree,
              accepted_at, search_document)
  on sci.publications to anon, authenticated;
grant select, insert, update on sci.publications, sci.uploads to sci_publisher;
grant execute on function sci.search_document(text, text, text[]) to anon, authenticated, sci_publisher;
