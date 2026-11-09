import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import { gzipSync } from 'node:zlib';
import {
  checkKeyAssociation,
  type FetchLike,
  fetchRevocation,
  lookupPublicationSources,
  openPublication,
  parseIndexDescriptor,
  publicJwkFromSeed,
  signEd25519,
  tryOpenPublication,
} from '../src/index.ts';
import { baselineLicense, enc, issueOfAsync, makePublication, SEED } from './helpers.ts';

interface MockOptions {
  readonly ranges?: boolean;
  readonly exposeContentRange?: boolean;
}

/** A mock fetch serving an in-memory set of URLs, with optional single-range support. */
function mockFetch(routes: Record<string, Uint8Array>, opts: MockOptions = {}): FetchLike & { log: string[] } {
  const log: string[] = [];
  const f = async (url: string, init?: RequestInit): Promise<Response> => {
    const range = new Headers(init?.headers).get('range');
    log.push(range ? `${url} ${range}` : url);
    const body = routes[url];
    if (!body) return new Response('not found', { status: 404 });
    if (range && opts.ranges !== false) {
      const total = body.length;
      let start: number;
      let end: number;
      const suffix = /^bytes=-(\d+)$/.exec(range);
      const span = /^bytes=(\d+)-(\d+)$/.exec(range);
      if (suffix) {
        start = Math.max(0, total - Number(suffix[1]));
        end = total - 1;
      } else if (span) {
        start = Number(span[1]);
        end = Math.min(Number(span[2]), total - 1);
      } else return new Response(null, { status: 416 });
      const headers = new Headers();
      if (opts.exposeContentRange !== false) headers.set('content-range', `bytes ${start}-${end}/${total}`);
      return new Response(body.slice(start, end + 1), { status: 206, headers });
    }
    return new Response(body.slice(), { status: 200 });
  };
  return Object.assign(f, { log });
}

const license = await baselineLicense();
const pub = makePublication({ LICENSE: license, 'files/data.csv': enc('a,b\n1,2\n') });

describe('retrieval with injected fetch', () => {
  const entryDesc = enc(JSON.stringify({ schema: 'sci-archive/0.1', entry_url: 'https://loose.example/{id}/{+path}' }));
  const zipDesc = enc(JSON.stringify({ schema: 'sci-archive/0.1', bundle_url: 'https://zip.example/{id2}/{id}.zip' }));
  const looseRoutes: Record<string, Uint8Array> = { 'https://loose.example/archive.json': entryDesc };
  for (const [p, d] of pub.loose) looseRoutes[`https://loose.example/${pub.digest}/${p}`] = d;
  const zipRoutes: Record<string, Uint8Array> = {
    'https://zip.example/archive.json': zipDesc,
    [`https://zip.example/${pub.digest.slice(0, 2)}/${pub.digest}.zip`]: pub.zip,
  };

  test('entry_url route: manifest, single files, metadata, complete bundle', async () => {
    const fetch = mockFetch(looseRoutes);
    const remote = await openPublication(['https://loose.example/archive.json'], pub.digest, { fetch });
    assert.equal(remote.route, 'entry');
    assert.deepEqual(await remote.readFile('files/data.csv'), enc('a,b\n1,2\n'));
    const meta = await remote.readMetadata();
    assert.equal(meta.metadata.title, 'Test');
    assert.equal(meta.license, new TextDecoder().decode(license));
    const full = await remote.verifyComplete();
    assert.equal(full.baseline.baseline, true);
    assert.equal(await remote.fetchTimestampSidecar(), null);
    await assert.rejects(remote.readFile('files/unlisted'));
  });

  test('bundle_url route uses range requests for single files', async () => {
    const fetch = mockFetch(zipRoutes);
    const remote = await openPublication(['https://zip.example/archive.json'], pub.id, { fetch });
    assert.equal(remote.route, 'bundle');
    const before = fetch.log.length;
    assert.deepEqual(await remote.readFile('files/data.csv'), enc('a,b\n1,2\n'));
    assert.equal(fetch.log.length - before, 1, 'one range request per file');
    assert.match(fetch.log[fetch.log.length - 1] ?? '', /bytes=\d+-\d+$/);
    const full = await remote.verifyComplete();
    assert.equal(full.zipSha256 !== undefined, true);
  });

  test('complete ZIP verification works when the host rejects suffix ranges', async () => {
    const fallback = mockFetch(zipRoutes);
    const fetch: FetchLike = (url, init) => {
      const range = new Headers(init?.headers).get('range');
      if (range?.startsWith('bytes=-')) return Promise.resolve(new Response('Unsupported client range', { status: 501 }));
      return fallback(url, init);
    };
    const remote = await openPublication(['https://zip.example/archive.json'], pub.id, { fetch });
    assert.deepEqual(await remote.readFile('files/data.csv'), enc('a,b\n1,2\n'));
    const full = await remote.verifyComplete();
    assert.equal(full.baseline.baseline, true);
    assert.equal(full.zipSha256 !== undefined, true);
  });

  for (const exposeEncoding of [true, false]) {
    test(`decoded gzip responses respect file bytes (Content-Encoding exposed: ${exposeEncoding})`, async () => {
      const fetch: FetchLike = async (url) => {
        const body = looseRoutes[url];
        if (!body) return new Response(null, { status: 404 });
        // Fetch decodes the body but retains the wire Content-Length. CORS can hide Content-Encoding.
        const headers = new Headers({ 'content-length': String(gzipSync(body).length) });
        if (exposeEncoding) headers.set('content-encoding', 'gzip');
        return new Response(body.slice(), { headers });
      };
      const readme = pub.files.get('README.md');
      assert.ok(readme);
      assert.ok(gzipSync(readme).length > readme.length);
      const remote = await openPublication(['https://loose.example/archive.json'], pub.digest, { fetch });
      assert.deepEqual(await remote.readFile('README.md'), readme);
      assert.equal((await remote.readMetadata()).metadata.title, 'Test');
    });
  }

  test('decoded streams exceeding the file budget are cancelled even with a small Content-Length', async () => {
    let cancelled = false;
    let pulls = 0;
    const fallback = mockFetch(looseRoutes);
    const fetch: FetchLike = async (url, init) => {
      if (!url.endsWith('/README.md')) return fallback(url, init);
      return new Response(
        new ReadableStream<Uint8Array>({
          pull(controller) {
            pulls++;
            controller.enqueue(pulls === 1 ? enc('# Hello\n') : enc('!'));
          },
          cancel() {
            cancelled = true;
          },
        }),
        { headers: { 'content-length': '1', 'content-encoding': 'gzip' } },
      );
    };
    const remote = await openPublication(['https://loose.example/archive.json'], pub.digest, { fetch });
    await assert.rejects(remote.readFile('README.md'), /budget\.response_bytes/);
    assert.ok(cancelled);
  });

  test('decoded files with the correct size still require the signed hash', async () => {
    const fallback = mockFetch(looseRoutes);
    const fetch: FetchLike = async (url, init) => {
      if (!url.endsWith('/README.md')) return fallback(url, init);
      return new Response(enc('# Evil!\n').slice(), { headers: { 'content-length': '1', 'content-encoding': 'gzip' } });
    };
    const remote = await openPublication(['https://loose.example/archive.json'], pub.digest, { fetch });
    await assert.rejects(remote.readFile('README.md'), /hash/);
  });

  test('servers without range support or with hidden Content-Range fall back to a bounded download', async () => {
    for (const opts of [{ ranges: false }, { exposeContentRange: false }]) {
      const remote = await openPublication(['https://zip.example/archive.json'], pub.id, { fetch: mockFetch(zipRoutes, opts) });
      assert.deepEqual(await remote.readFile('README.md'), enc('# Hello\n'));
    }
  });

  test('bad sources are skipped; a source with tampered files is not trusted', async () => {
    const tampered = { ...looseRoutes, [`https://loose.example/${pub.digest}/files/data.csv`]: enc('evil') };
    const wrongManifest: Record<string, Uint8Array> = {
      'https://bad.example/archive.json': entryDesc.slice(),
      [`https://loose.example/${pub.digest}/manifest.json`]: enc('{}'),
    };
    const remote = await openPublication(['https://missing.example/archive.json', 'https://loose.example/archive.json'], pub.digest, {
      fetch: mockFetch(tampered),
    });
    assert.equal(remote.attempts.length, 1);
    assert.equal((await issueOfAsync(() => remote.readFile('files/data.csv'))).code, 'retrieval.file_unavailable');
    const r = await tryOpenPublication(['https://bad.example/archive.json'], pub.digest, {
      fetch: mockFetch({ ...wrongManifest, 'https://bad.example/archive.json': entryDesc }),
    });
    assert.equal(r.outcome, 'incomplete');
  });

  test('network failures are incomplete', async () => {
    const failing: FetchLike = () => Promise.reject(new TypeError('network down'));
    const r = await tryOpenPublication(['https://loose.example/archive.json'], pub.digest, { fetch: failing });
    assert.equal(r.outcome, 'incomplete');
  });

  test('index lookups, revocations, and key associations', async () => {
    const jwk = publicJwkFromSeed(SEED);
    const index = parseIndexDescriptor(
      enc(
        JSON.stringify({
          schema: 'sci-index/0.1',
          publication_lookup_url: 'https://i.example/p/{id}',
          revocation_lookup_url: 'https://i.example/k/{x}/{file}',
          timestamp_lookup_url: 'https://i.example/t/{id}',
        }),
      ),
    );
    const rev = enc(JSON.stringify({ schema: 'sci-key-revocation/0.1', public_key: jwk }));
    const claim = enc(
      JSON.stringify({
        schema: 'sci-key-claim/0.1',
        public_key: jwk,
        account: { type: 'github', id: 'octo' },
        expires: '2099-01-01T00:00:00Z',
      }),
    );
    const fetch = mockFetch({
      [`https://i.example/p/${pub.digest}`]: enc('{"descriptors":["https://loose.example/archive.json"]}'),
      [`https://i.example/k/${jwk.x}/revocation.json`]: rev,
      [`https://i.example/k/${jwk.x}/revocation.json.sig`]: signEd25519(SEED, rev),
      [`https://raw.githubusercontent.com/octo/.github/main/sci/keys/${jwk.x}.json`]: claim,
      [`https://raw.githubusercontent.com/octo/.github/main/sci/keys/${jwk.x}.json.sig`]: signEd25519(SEED, claim),
    });
    assert.deepEqual(await lookupPublicationSources(index, pub.id, { fetch }), ['https://loose.example/archive.json']);
    assert.deepEqual(await lookupPublicationSources(index, 'f'.repeat(64), { fetch }), []);
    assert.equal((await fetchRevocation(index, jwk.x, { fetch }))?.revocation.publicKey.x, jwk.x);
    assert.equal(await fetchRevocation(index, 'A'.repeat(43), { fetch }), null);
    const verified = await checkKeyAssociation({ type: 'github', id: 'octo' }, jwk.x, { fetch, now: new Date('2026-11-07T00:00:00Z') });
    assert.equal(verified.account.id, 'octo');
    assert.equal(
      (await issueOfAsync(() => checkKeyAssociation({ type: 'domain', id: 'example.org' }, jwk.x, { fetch }))).outcome,
      'incomplete',
    );
    assert.equal(
      (await issueOfAsync(() => checkKeyAssociation({ type: 'github', id: 'octo' }, jwk.x, { fetch, knownRevocation: true }))).code,
      'claim.key_revoked',
    );
  });
});
