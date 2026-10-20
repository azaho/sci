-- Migration 001. Operational tables are never exposed by PostgREST.
CREATE SCHEMA IF NOT EXISTS sci_private;
CREATE SCHEMA IF NOT EXISTS catalog_v1;
REVOKE ALL ON SCHEMA sci_private FROM PUBLIC;
REVOKE ALL ON SCHEMA catalog_v1 FROM PUBLIC;

DO $roles$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sci_reader') THEN
    CREATE ROLE sci_reader NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sci_anon') THEN
    CREATE ROLE sci_anon NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sci_publisher') THEN
    CREATE ROLE sci_publisher LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sci_authenticator') THEN
    CREATE ROLE sci_authenticator LOGIN NOINHERIT;
  END IF;
END
$roles$;
GRANT sci_reader TO sci_anon;
GRANT sci_anon TO sci_authenticator;
DO $supabase$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN
    GRANT sci_reader TO anon;
    ALTER ROLE anon SET statement_timeout = '5s';
  END IF;
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
    GRANT sci_reader TO authenticated;
    ALTER ROLE authenticated SET statement_timeout = '5s';
  END IF;
END
$supabase$;
ALTER ROLE sci_anon SET statement_timeout = '5s';
ALTER ROLE sci_publisher SET statement_timeout = '30s';

ALTER DEFAULT PRIVILEGES IN SCHEMA sci_private REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA sci_private REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA catalog_v1 REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA catalog_v1 REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE sci_private.identities (
  fingerprint text PRIMARY KEY CHECK (fingerprint ~ '^ed25519:[0-9a-f]{64}$'),
  public_key text NOT NULL UNIQUE CHECK (public_key ~ '^[A-Za-z0-9_-]{43}$'),
  enabled boolean NOT NULL DEFAULT false
);
CREATE TABLE sci_private.publications (
  uuid uuid PRIMARY KEY CHECK (uuid::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
  manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  state text NOT NULL CHECK (state IN ('finalizing', 'accepted', 'failed')),
  failure_reason text,
  title text NOT NULL,
  abstract text NOT NULL,
  categories text[] NOT NULL DEFAULT '{}',
  keywords text[] NOT NULL DEFAULT '{}',
  headline_figure_caption text,
  author_fingerprint text NOT NULL REFERENCES sci_private.identities(fingerprint),
  author_public_key text NOT NULL,
  created_at text NOT NULL,
  accepted_at timestamptz NOT NULL,
  instance_url text NOT NULL,
  object_base text NOT NULL,
  filenames text[] NOT NULL,
  acceptance_ref text NOT NULL,
  files_tree jsonb,
  upload_id uuid,
  search_document tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english'::regconfig, title), 'A') ||
    setweight(to_tsvector('english'::regconfig, abstract), 'B')
  ) STORED
);
-- Keyword search is maintained in a separate immutable expression below.
CREATE FUNCTION sci_private.search_text(title text, abstract text, keywords text[])
RETURNS tsvector LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path = pg_catalog AS $$
  SELECT setweight(to_tsvector('english', title), 'A') ||
         setweight(to_tsvector('english', abstract), 'B') ||
         setweight(to_tsvector('english', array_to_string(keywords, ' ')), 'C')
$$;
ALTER TABLE sci_private.publications DROP COLUMN search_document;
ALTER TABLE sci_private.publications ADD COLUMN search_document tsvector GENERATED ALWAYS AS
  (sci_private.search_text(title, abstract, keywords)) STORED;

CREATE TABLE sci_private.citations (
  source_uuid uuid NOT NULL REFERENCES sci_private.publications(uuid),
  position integer NOT NULL CHECK (position >= 0),
  target text COLLATE "C" NOT NULL,
  target_key text COLLATE "C" NOT NULL CHECK (octet_length(target_key) <= 512),
  relation text CHECK (relation IN ('uses', 'supports', 'reproduces', 'challenges', 'supersedes')),
  reason text,
  PRIMARY KEY (source_uuid, position)
);
CREATE TABLE sci_private.uploads (
  upload_id uuid PRIMARY KEY,
  uuid uuid NOT NULL,
  manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  author_fingerprint text NOT NULL REFERENCES sci_private.identities(fingerprint),
  temporary_prefix text NOT NULL UNIQUE,
  deadline timestamptz NOT NULL,
  state text NOT NULL CHECK (state IN (
    'preparing', 'pending', 'verifying', 'failed', 'expired', 'committed', 'accepted'
  )),
  -- Only references, descriptors, and verification ETags; never publication blobs.
  sources jsonb,
  failure_reason text,
  cleaned_at timestamptz
);
ALTER TABLE sci_private.publications ADD CONSTRAINT publication_upload
  FOREIGN KEY (upload_id) REFERENCES sci_private.uploads(upload_id);
CREATE UNIQUE INDEX one_active_upload_per_uuid ON sci_private.uploads(uuid)
  WHERE state IN ('preparing', 'pending', 'verifying', 'committed');
CREATE INDEX uploads_cleanup ON sci_private.uploads(deadline) WHERE cleaned_at IS NULL;
CREATE INDEX publications_feed ON sci_private.publications(accepted_at DESC, uuid DESC)
  WHERE state = 'accepted';
CREATE INDEX publications_author ON sci_private.publications(author_fingerprint, accepted_at DESC, uuid DESC)
  WHERE state = 'accepted';
CREATE INDEX publications_categories ON sci_private.publications USING gin(categories)
  WHERE state = 'accepted';
CREATE INDEX publications_search ON sci_private.publications USING gin(search_document)
  WHERE state = 'accepted';
CREATE INDEX incoming_citations ON sci_private.citations(target_key, source_uuid, position);
CREATE INDEX incoming_relations ON sci_private.citations(target_key, relation, source_uuid);

CREATE TABLE sci_private.rate_limits (
  bucket text PRIMARY KEY,
  window_start timestamptz NOT NULL,
  requests integer NOT NULL
);
CREATE TABLE sci_private.instance_state (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  writes_enabled boolean NOT NULL DEFAULT true
);
INSERT INTO sci_private.instance_state(singleton, writes_enabled) VALUES (true, true);

ALTER TABLE sci_private.identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE sci_private.publications ENABLE ROW LEVEL SECURITY;
ALTER TABLE sci_private.citations ENABLE ROW LEVEL SECURITY;
ALTER TABLE sci_private.uploads ENABLE ROW LEVEL SECURITY;
ALTER TABLE sci_private.rate_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE sci_private.instance_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY publisher_identity_read ON sci_private.identities FOR SELECT TO sci_publisher USING (true);
CREATE POLICY publisher_publications ON sci_private.publications TO sci_publisher USING (true) WITH CHECK (true);
CREATE POLICY publisher_citations ON sci_private.citations TO sci_publisher USING (true) WITH CHECK (true);
CREATE POLICY publisher_uploads ON sci_private.uploads TO sci_publisher USING (true) WITH CHECK (true);
CREATE POLICY publisher_limits ON sci_private.rate_limits TO sci_publisher USING (true) WITH CHECK (true);
CREATE POLICY publisher_state_read ON sci_private.instance_state FOR SELECT TO sci_publisher USING (true);
CREATE POLICY public_accepted ON sci_private.publications FOR SELECT TO sci_reader USING (state = 'accepted');
CREATE POLICY public_citations ON sci_private.citations FOR SELECT TO sci_reader USING (
  EXISTS (SELECT 1 FROM sci_private.publications p WHERE p.uuid = source_uuid AND p.state = 'accepted')
);

GRANT USAGE ON SCHEMA sci_private TO sci_publisher, sci_reader;
GRANT SELECT ON sci_private.identities, sci_private.instance_state TO sci_publisher;
-- SELECT FOR SHARE on the whitelist requires an UPDATE privilege. Use the narrow
-- lock helper in admin.sql instead; the publisher gets no whitelist write grant.
GRANT SELECT, INSERT, UPDATE ON sci_private.publications, sci_private.citations,
  sci_private.uploads TO sci_publisher;
GRANT SELECT, INSERT, UPDATE, DELETE ON sci_private.rate_limits TO sci_publisher;
GRANT EXECUTE ON FUNCTION sci_private.search_text(text, text, text[]) TO sci_publisher;
