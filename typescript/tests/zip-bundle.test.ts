import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import {
  blobSource,
  bytesSource,
  checkZipBundle,
  fromHex,
  locateZipEntry,
  parseMetadata,
  readZipDirectory,
  readZipEntriesSpan,
  sha256Hex,
  verifyLooseBundle,
  verifyManifest,
  verifyManifestScope,
  verifyZipBundle,
  zipSizeOf,
} from '../src/index.ts';
import { baselineLicense, enc, issueOf, issueOfAsync, makePublication } from './helpers.ts';

describe('canonical ZIP (§2.2)', () => {
  const pub = makePublication({ 'files/b.txt': enc('bbb'), 'files/a/x.csv': enc('1,2\n'), 'files/empty': new Uint8Array() });

  test('writer output matches the §2.1 size formula and reader accepts it', async () => {
    const m = verifyManifest(pub.manifest, pub.signature);
    assert.equal(pub.zip.length, zipSizeOf(pub.manifest.length, 64, m.files));
    const dir = await readZipDirectory(bytesSource(pub.zip));
    assert.deepEqual(
      dir.entries.map((e) => e.name),
      ['manifest.json', 'manifest.json.sig', 'README.md', 'files/a/x.csv', 'files/b.txt', 'files/empty', 'sci.json'],
    );
  });

  test('range locator and single-read entry spans', async () => {
    const src = bytesSource(pub.zip);
    const dir = await readZipDirectory(src);
    const r = locateZipEntry(dir, 'files/b.txt');
    assert.ok(r);
    assert.deepEqual(pub.zip.subarray(r.dataOffset, r.end), enc('bbb'));
    const [m, s] = await readZipEntriesSpan(src, dir.entries.slice(0, 2));
    assert.deepEqual(m, pub.manifest);
    assert.deepEqual(s, pub.signature);
    assert.equal(locateZipEntry(dir, 'nope'), undefined);
  });

  test('every single-byte corruption of structure is rejected or detected', async () => {
    // Flip each header byte (not file data) and require a non-valid outcome.
    const dir = await readZipDirectory(bytesSource(pub.zip));
    const dataRanges = dir.entries.map((e) => [e.dataOffset, e.dataOffset + e.size] as const);
    let checked = 0;
    for (let i = 0; i < pub.zip.length; i++) {
      if (dataRanges.some(([a, b]) => i >= a && i < b)) continue;
      const z = pub.zip.slice();
      z[i] = (z[i] ?? 0) ^ 0x01;
      const r = await checkZipBundle(z, { expectedId: pub.id });
      assert.notEqual(r.outcome, 'valid', `byte ${i} flip accepted`);
      checked++;
    }
    assert.ok(checked > 200);
  });

  test('trailing, prefix, and truncation are invalid', async () => {
    const plus = new Uint8Array(pub.zip.length + 1);
    plus.set(pub.zip);
    assert.equal((await checkZipBundle(plus)).outcome, 'invalid');
    const pre = new Uint8Array(pub.zip.length + 1);
    pre.set(pub.zip, 1);
    assert.equal((await checkZipBundle(pre)).outcome, 'invalid');
    assert.equal((await checkZipBundle(pub.zip.subarray(0, pub.zip.length - 1))).outcome, 'invalid');
    assert.equal((await checkZipBundle(new Uint8Array(10))).outcome, 'invalid');
  });
});

const TINY_PNG = fromHex(
  '89504e470d0a1a0a0000000d49484452000000010000000108000000003a7e9b550000000a4944415478da6360000000020001e527defc0000000049454e44ae426082',
) as Uint8Array;

describe('complete bundle checks (§6.4) and baseline (§2.1)', () => {
  test('baseline publication via ZIP, Blob, and loose files', async () => {
    const pub = makePublication({ LICENSE: await baselineLicense(), 'files/fig.png': TINY_PNG }, undefined, {
      title: 'T',
      abstract: 'A\nB',
      display: [{ path: 'files/fig.png', caption: 'Figure' }],
      citations: [{ target: 'doi:10.1000/XYZ', relations: ['uses'] }],
    });
    const z = await verifyZipBundle(pub.zip, { expectedId: pub.id, retain: (p) => p === 'files/fig.png' });
    assert.equal(z.baseline.baseline, true);
    assert.equal(z.zipSha256, sha256Hex(pub.zip));
    assert.deepEqual(z.files.get('files/fig.png'), TINY_PNG);
    assert.equal(z.metadata.citations?.[0]?.targetKey, 'doi:10.1000/xyz');
    const b = await verifyZipBundle(blobSource(new Blob([pub.zip.slice()])), { expectedId: pub.digest });
    assert.equal(b.manifest.id, pub.id);
    const loose = await verifyLooseBundle(pub.loose, { expectedId: pub.id });
    assert.equal(loose.zipSha256, sha256Hex(pub.zip));
    const withSidecar = new Map(pub.loose);
    withSidecar.set('manifest.json.sig.tsr', new Uint8Array([1]));
    assert.equal((await verifyLooseBundle(withSidecar)).baseline.baseline, true);
    // Streaming loose sources produce the same canonical ZIP hash.
    const streamed = new Map([...pub.loose].map(([k, v]) => [k, bytesSource(v)] as const));
    assert.equal((await verifyLooseBundle(streamed, { budget: { chunkBytes: 3 } })).zipSha256, sha256Hex(pub.zip));
  });

  test('non-baseline reasons are reported without invalidity', async () => {
    const pub = makePublication({ LICENSE: enc('custom') });
    const r = await verifyZipBundle(pub.zip);
    assert.equal(r.baseline.baseline, false);
    assert.deepEqual(r.baseline.unmet, ['license']);
  });

  test('display files must pass the PNG structure checks, split across any chunk size', async () => {
    const license = await baselineLicense();
    const verdict = async (name: string, bytes: Uint8Array, chunkBytes = 3) => {
      const pub = makePublication({ LICENSE: license, [name]: bytes }, undefined, { title: 'T', abstract: 'A', display: [{ path: name }] });
      return (await verifyZipBundle(pub.zip, { budget: { chunkBytes } })).baseline.unmet;
    };
    assert.deepEqual(await verdict('files/f.png', TINY_PNG), []);
    assert.deepEqual(await verdict('files/f.PNG', TINY_PNG, 65_536), []);
    assert.deepEqual(await verdict('files/f.jpg', TINY_PNG), ['display_files']);
    assert.deepEqual(await verdict('files/f.png', TINY_PNG.subarray(0, TINY_PNG.length - 1)), ['display_files']);
  });

  test('inventory, integrity, and identity failures', async () => {
    const pub = makePublication({ 'files/a': enc('a') });
    const extra = new Map(pub.loose);
    extra.set('files/unlisted', enc('x'));
    assert.equal((await issueOfAsync(() => verifyLooseBundle(extra))).code, 'bundle.unlisted_file');
    const missing = new Map(pub.loose);
    missing.delete('files/a');
    assert.equal((await issueOfAsync(() => verifyLooseBundle(missing))).code, 'bundle.missing_file');
    const tampered = new Map(pub.loose);
    tampered.set('files/a', enc('b'));
    assert.equal((await issueOfAsync(() => verifyLooseBundle(tampered))).code, 'file.hash_mismatch');
    assert.equal((await issueOfAsync(() => verifyZipBundle(pub.zip, { expectedId: 'f'.repeat(64) }))).code, 'manifest.id_mismatch');
    assert.equal((await issueOfAsync(() => verifyZipBundle(pub.zip, { budget: { maxTotalBytes: 10 } }))).outcome, 'incomplete');
  });

  test('manifest scope and metadata rules', () => {
    const pub = makePublication();
    const r = verifyManifestScope(pub.manifest, pub.signature);
    assert.equal(r.withinBaselineZipLimit, true);
    const files = verifyManifest(pub.manifest, pub.signature).files;
    assert.equal(
      issueOf(() => parseMetadata(enc('{"title":"t","abstract":"a","display":[{"path":"files/x"}]}'), files)).code,
      'metadata.display_path',
    );
    assert.equal(
      issueOf(() =>
        parseMetadata(enc('{"title":"t","abstract":"a","citations":[{"target":"https://x.example","relations":["uses","uses"]}]}'), files),
      ).code,
      'metadata.citation_relation_duplicate',
    );
    assert.equal(
      issueOf(() => parseMetadata(enc('{"title":"t","abstract":"a","keywords":["k\\n"]}'), files)).code,
      'text.forbidden_control',
    );
    assert.equal(
      issueOf(() =>
        parseMetadata(
          enc('{"title":"t","abstract":"a","categories":[{"scheme":"arxiv","code":"cs.CR"},{"scheme":"arxiv","code":"cs.CR"}]}'),
          files,
        ),
      ).code,
      'metadata.category_duplicate',
    );
  });
});
