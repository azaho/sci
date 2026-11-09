import assert from 'node:assert/strict';
import { test } from 'node:test';
import { verificationSource } from '../src/source.ts';

test('verification cache reads across boundaries, evicts old blocks, and rejects invalid/short reads', async () => {
  const block = 4 * 1024 * 1024;
  const calls: number[] = [];
  const source = verificationSource({
    size: block * 4 + 3,
    async read(offset, length) {
      assert.ok(length <= block);
      calls.push(offset);
      return new Uint8Array(length).fill(offset / block);
    },
  });
  assert.deepEqual(await source.read(block - 2, 5), new Uint8Array([0, 0, 1, 1, 1]));
  await source.read(0, 1);
  await source.read(block * 2, 1);
  await source.read(block, 1); // Block 1 was least recently used and must be fetched again.
  assert.deepEqual(calls, [0, block, block * 2, block]);
  assert.deepEqual(await source.read(block * 4, 3), new Uint8Array([4, 4, 4]));
  assert.equal((await source.read(source.size, 0)).length, 0);
  for (const [offset, length] of [
    [-1, 1],
    [0, -1],
    [0.5, 1],
    [source.size, 1],
  ]) {
    await assert.rejects(source.read(offset ?? 0, length ?? 0), RangeError);
  }
  await assert.rejects(verificationSource({ size: 10, read: async () => new Uint8Array(9) }).read(0, 1), /short block/);
});
