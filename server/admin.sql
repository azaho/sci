-- Private administration only. Run these as the database migration administrator.
CREATE FUNCTION sci_private.key_fingerprint(public_key text)
RETURNS text LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
DECLARE raw bytea;
BEGIN
  IF public_key IS NULL OR public_key !~ '^[A-Za-z0-9_-]{43}$' THEN
    RAISE EXCEPTION 'Expected canonical unpadded base64url of a raw 32-byte Ed25519 key';
  END IF;
  raw := decode(translate(public_key, '-_', '+/') || '=', 'base64');
  IF octet_length(raw) <> 32 OR rtrim(translate(encode(raw, 'base64'), '+/', '-_'), '=') <> public_key THEN
    RAISE EXCEPTION 'Noncanonical Ed25519 public key';
  END IF;
  RETURN 'ed25519:' || encode(sha256(raw), 'hex');
END
$$;

CREATE FUNCTION sci_private.enable_key(public_key text)
RETURNS text LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
DECLARE fp text := sci_private.key_fingerprint(public_key);
BEGIN
  -- Same transaction lock as the publisher's whitelist check.
  PERFORM pg_advisory_xact_lock(hashtextextended('key:' || fp, 0));
  INSERT INTO sci_private.identities AS i(fingerprint, public_key, enabled)
    VALUES (fp, public_key, true)
    ON CONFLICT (fingerprint) DO UPDATE SET enabled = true;
  RETURN fp;
END
$$;
CREATE FUNCTION sci_private.disable_key(public_key text)
RETURNS text LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
DECLARE fp text := sci_private.key_fingerprint(public_key);
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('key:' || fp, 0));
  UPDATE sci_private.identities SET enabled = false WHERE fingerprint = fp;
  IF NOT FOUND THEN RAISE EXCEPTION 'Unknown public key'; END IF;
  RETURN fp;
END
$$;
CREATE FUNCTION sci_private.list_keys()
RETURNS TABLE(public_key text, fingerprint text, enabled boolean)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog AS $$
  SELECT i.public_key, i.fingerprint, i.enabled FROM sci_private.identities i ORDER BY i.fingerprint
$$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA sci_private FROM PUBLIC;
REVOKE ALL ON FUNCTION sci_private.key_fingerprint(text), sci_private.enable_key(text),
  sci_private.disable_key(text), sci_private.list_keys() FROM sci_publisher, sci_reader;
-- The owner/administrator retains execution. Grant to a designated administrator
-- explicitly if it is different from the migration owner.
