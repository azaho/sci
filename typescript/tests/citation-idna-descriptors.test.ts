import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import {
  citationTargetKey,
  claimLocations,
  decodeALabel,
  descriptorLocations,
  expandTemplate,
  interpretPublicationLookup,
  interpretTimestampLookup,
  isHttpUri,
  isValidClaimDomain,
  parseArchiveDescriptor,
  parseCitationTarget,
  parseIndexDescriptor,
  parseSubmissionRequest,
  punycodeDecode,
  punycodeEncode,
  routes,
  validateSubmissionResponse,
} from '../src/index.ts';
import { enc, issueOf } from './helpers.ts';

const D = 'c016f424ed267b2edc1784101e4bd15b9950957b61f83af22efdaafc450f41f9';

describe('citation targets and target_key (§4.4)', () => {
  test('SCI targets by ID', () => {
    assert.equal(citationTargetKey(`sci:sha256:${D}`), `sci:sha256:${D}`);
    assert.equal(citationTargetKey(`sci:sha256:${D.toUpperCase()}`), null);
    assert.equal(citationTargetKey(`sci:${D}`), null);
  });
  test('DOIs by equivalence: case-insensitive ASCII, percent-decoding', () => {
    const k = citationTargetKey('doi:10.1000/ABC-def');
    assert.equal(k, 'doi:10.1000/abc-def');
    assert.equal(citationTargetKey('doi:10.1000/abc%2Ddef'), k);
    assert.equal(citationTargetKey('doi:10.1000/a%2Fb'), citationTargetKey('doi:10.1000/A/B'));
    assert.equal(citationTargetKey('doi:10.1000/x%3Fy'), 'doi:10.1000/x%3Fy');
    assert.equal(citationTargetKey('doi:10.1000/%C3%A9'), 'doi:10.1000/%C3%A9');
    assert.equal(citationTargetKey('doi:10.1000/x?y'), null);
    assert.equal(citationTargetKey('doi:10.1000/x#y'), null);
    assert.equal(citationTargetKey('doi:11.1000/x'), null);
    assert.equal(citationTargetKey('doi:10.1000/'), null);
    assert.equal(citationTargetKey('doi:10.1000/%ZZ'), null);
    assert.equal(citationTargetKey('doi:10.1000/%FF'), null);
    assert.equal(citationTargetKey('doi:10.1000/a b'), null);
    assert.equal(parseCitationTarget('doi:10.1000/182')?.kind, 'doi');
  });
  test('URLs by exact string', () => {
    assert.equal(citationTargetKey('https://Example.org/a#x'), 'https://Example.org/a#x');
    assert.notEqual(citationTargetKey('https://example.org/a'), citationTargetKey('https://example.org/a/'));
    for (const ok of ['http://example.org', 'https://example.org:8443/p?q=1&r=%20#frag', 'https://[2001:db8::1]/x', 'HTTPS://example.org/'])
      assert.ok(isHttpUri(ok), ok);
    for (const bad of [
      'ftp://example.org/a',
      '/relative',
      'https://',
      'https:///path',
      'https://user@example.org/',
      'https://exa mple.org/',
      'https://example.org/%zz',
      'https://example.org:8a/',
      'https://[::g]/',
    ])
      assert.ok(!isHttpUri(bad), bad);
  });
});

describe('IDNA2008 A-labels (§5.2.2)', () => {
  test('punycode round trip', () => {
    assert.equal(punycodeDecode('bcher-kva'), 'bücher');
    assert.equal(punycodeEncode('bücher'), 'bcher-kva');
    assert.equal(punycodeEncode('例え'), 'r8jz45g');
    assert.equal(punycodeDecode('r8jz45g'), '例え');
  });
  test('domain validity', () => {
    for (const ok of ['example.org', 'xn--bcher-kva.example', 'a-b.example', 'xn--r8jz45g.jp', 'localhost'])
      assert.ok(isValidClaimDomain(ok), ok);
    for (const bad of [
      'Example.org',
      'example.org.',
      '127.0.0.1',
      'xn--a.example',
      'xn--.example',
      'ab--cd.example',
      '-a.example',
      'a_b.example',
      'xn--Bcher-kva.example',
      'xn--abc.example',
      `${'a'.repeat(64)}.example`,
      '',
      'a..b',
    ]) {
      assert.ok(!isValidClaimDomain(bad), bad);
    }
    // Uppercase U-label content (DISALLOWED: unstable under case folding): "Bücher".
    assert.equal(decodeALabel(`xn--${punycodeEncode('bÜcher') ?? ''}`), null);
  });
  test('claim locations', () => {
    const x = 'B0FOUbkEkRCSvkhN3_l43qNcIhewT-QlZq-BnxuvDPg';
    assert.deepEqual(claimLocations({ type: 'domain', id: 'example.org' }, x), [
      { json: `https://example.org/.well-known/sci/keys/${x}.json`, sig: `https://example.org/.well-known/sci/keys/${x}.json.sig` },
    ]);
    assert.equal(claimLocations({ type: 'github', id: 'octo' }, x).length, 2);
    assert.equal(
      claimLocations({ type: 'github', id: 'octo' }, x)[1]?.json,
      `https://raw.githubusercontent.com/octo/.github/main/sci/keys/${x}.json`,
    );
    assert.equal(
      claimLocations({ type: 'huggingface', id: 'Org' }, x)[0]?.sig,
      `https://huggingface.co/spaces/Org/README/resolve/main/sci/keys/${x}.json.sig`,
    );
    assert.equal(issueOf(() => claimLocations({ type: 'github', id: 'Octo' }, x)).code, 'claim.github');
  });
});

describe('descriptors, templates, submission (§8)', () => {
  const index = parseIndexDescriptor(
    enc(
      JSON.stringify({
        schema: 'sci-index/0.1',
        publication_lookup_url: 'https://index.example/p/{id}',
        revocation_lookup_url: 'https://index.example/k/{x}/{file}',
        timestamp_lookup_url: 'https://index.example/t/{id}',
        extension: { any: 1 },
      }),
    ),
  );
  const archive = parseArchiveDescriptor(
    enc(
      JSON.stringify({
        schema: 'sci-archive/0.1',
        bundle_url: 'https://a.example/{id2}/{id}.zip',
        entry_url: 'https://a.example/{id}/{+path}',
        blob_url: 'https://a.example/b/{sha256}',
      }),
    ),
  );

  test('routes and RFC 6570 expansion', () => {
    assert.equal(routes.publicationLookup(index, D), `https://index.example/p/${D}`);
    assert.equal(routes.revocationLookup(index, 'abc', 'revocation.json.sig'), 'https://index.example/k/abc/revocation.json.sig');
    assert.equal(routes.bundle(archive, D), `https://a.example/c0/${D}.zip`);
    assert.equal(routes.entry(archive, D, 'files/Data/a.txt'), `https://a.example/${D}/files/Data/a.txt`);
    assert.equal(expandTemplate('https://x.example/{v}', { v: 'a/b c' }), 'https://x.example/a%2Fb%20c');
    assert.equal(expandTemplate('https://x.example/{+v}', { v: 'a/b c' }), 'https://x.example/a/b%20c');
    assert.equal(
      descriptorLocations.huggingface('https://huggingface.co/datasets/o/r/'),
      'https://huggingface.co/datasets/o/r/resolve/main/sci/archive.json',
    );
  });

  test('descriptor rejections', () => {
    const base = {
      schema: 'sci-index/0.1',
      publication_lookup_url: 'https://i.example/{id}',
      revocation_lookup_url: 'https://i.example/{x}/{file}',
      timestamp_lookup_url: 'https://i.example/t/{id}',
    };
    const bad = (o: object) => issueOf(() => parseIndexDescriptor(enc(JSON.stringify(o))));
    assert.equal(bad({ ...base, publication_lookup_url: 'https://i.example/{path}' }).code, 'template.expression');
    assert.equal(bad({ ...base, publication_lookup_url: 'https://i.example/x' }).code, 'template.missing_expression');
    assert.equal(bad({ ...base, publication_lookup_url: '/p/{id}' }).code, 'descriptor.url');
    assert.equal(bad({ ...base, schema: 'sci-index/9' }).outcome, 'unsupported');
    assert.equal(bad({ ...base, schema: 'sci-archive/0.1' }).outcome, 'invalid');
    assert.equal(
      issueOf(() => parseArchiveDescriptor(enc('{"schema":"sci-archive/0.1","blob_url":"https://a.example/{sha256}"}'))).code,
      'descriptor.no_manifest_route',
    );
    assert.equal(
      issueOf(() => parseArchiveDescriptor(enc('{"schema":"sci-archive/0.1","entry_url":"https://a.example/{path}"}'))).code,
      'template.expression',
    );
  });

  test('submission request/response contract (§8.1.4)', () => {
    const req = parseSubmissionRequest(
      enc(
        JSON.stringify({
          id: D,
          descriptors: ['https://a.example/.well-known/sci/archive.json'],
          account: { type: 'domain', id: 'example.org' },
        }),
      ),
    );
    assert.equal(req.account?.id, 'example.org');
    assert.equal(
      issueOf(() => parseSubmissionRequest(enc(JSON.stringify({ id: D, descriptors: [] })))).code,
      'submission.descriptors_empty',
    );
    const ok = validateSubmissionResponse(201, 'application/json; charset=utf-8', enc(JSON.stringify({ id: D, status: 'accepted' })), D);
    assert.equal(ok.kind, 'success');
    const err = validateSubmissionResponse(429, 'application/json', enc('{"error":{"code":"busy","message":"m","retryable":true}}'), D);
    assert.ok(err.kind === 'error' && err.retryable);
    assert.equal(issueOf(() => validateSubmissionResponse(200, 'text/plain', enc(`{"id":"${D}"}`), D)).code, 'submission.content_type');
  });

  test('lookup responses', () => {
    assert.deepEqual(interpretPublicationLookup(404, new Uint8Array()).sources, []);
    assert.deepEqual(interpretPublicationLookup(200, enc('{"descriptors":["https://a.example/d.json"]}')).sources, [
      'https://a.example/d.json',
    ]);
    assert.equal(issueOf(() => interpretPublicationLookup(503, new Uint8Array())).outcome, 'incomplete');
    assert.equal(interpretTimestampLookup(404, new Uint8Array()).timestampExistence, 'unknown');
    assert.equal(interpretTimestampLookup(200, new Uint8Array([0x30, 0x00, 0x02, 0x01, 0x01])).proofs, 2);
  });
});
