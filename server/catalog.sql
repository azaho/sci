-- Public contract. Caller privileges and RLS apply to every view.
CREATE VIEW catalog_v1.publications WITH (security_invoker = true) AS
SELECT uuid, manifest_sha256, title, abstract, categories, keywords,
       headline_figure_caption, author_fingerprint,
       jsonb_build_object('kty', 'OKP', 'crv', 'Ed25519', 'x', author_public_key) AS public_key,
       created_at, accepted_at, files_tree,
       (SELECT jsonb_object_agg(name, object_base || '/' || name)
        FROM unnest(filenames) AS name) AS objects,
       search_document
FROM sci_private.publications WHERE state = 'accepted';

CREATE VIEW catalog_v1.citations WITH (security_invoker = true) AS
SELECT c.source_uuid, c.position, c.target, c.target_key, c.relation, c.reason
FROM sci_private.citations c
JOIN sci_private.publications p ON p.uuid = c.source_uuid
WHERE p.state = 'accepted';

CREATE VIEW catalog_v1.citation_counts WITH (security_invoker = true) AS
SELECT c.target_key, count(DISTINCT c.source_uuid) AS citing_publications
FROM sci_private.citations c
JOIN sci_private.publications p ON p.uuid = c.source_uuid
WHERE p.state = 'accepted'
GROUP BY c.target_key;

-- Explicit computed relationships avoid relying on PostgREST's view inference.
CREATE FUNCTION catalog_v1.publications(catalog_v1.citations)
RETURNS SETOF catalog_v1.publications ROWS 1
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog AS $$
  SELECT p.uuid, p.manifest_sha256, p.title, p.abstract, p.categories, p.keywords,
         p.headline_figure_caption, p.author_fingerprint, p.public_key,
         p.created_at, p.accepted_at, p.files_tree, p.objects, p.search_document
  FROM catalog_v1.publications p WHERE p.uuid = ($1).source_uuid
$$;
CREATE FUNCTION catalog_v1.citations(catalog_v1.publications)
RETURNS SETOF catalog_v1.citations
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog AS $$
  SELECT c.source_uuid, c.position, c.target, c.target_key, c.relation, c.reason
  FROM catalog_v1.citations c WHERE c.source_uuid = ($1).uuid
$$;

GRANT SELECT (uuid, manifest_sha256, state, title, abstract, categories, keywords,
  headline_figure_caption, author_fingerprint, author_public_key, created_at,
  accepted_at, files_tree, object_base, filenames, search_document)
  ON sci_private.publications TO sci_reader;
GRANT SELECT (source_uuid, position, target, target_key, relation, reason)
  ON sci_private.citations TO sci_reader;
GRANT USAGE ON SCHEMA catalog_v1 TO sci_reader;
GRANT SELECT ON catalog_v1.publications, catalog_v1.citations, catalog_v1.citation_counts TO sci_reader;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA catalog_v1 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION catalog_v1.publications(catalog_v1.citations),
  catalog_v1.citations(catalog_v1.publications),
  catalog_v1.normalize_citation_target(text) TO sci_reader;
NOTIFY pgrst, 'reload schema';
