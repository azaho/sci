import { parseJson, verifyZip } from "./verify.ts";

const root = Deno.args[0];
if (!root) throw new Error("Usage: deno run --allow-read check.ts FIXTURE_DIR");
const cases = JSON.parse(await Deno.readTextFile(`${root}/cases.json`));
let failures = 0;
for (const fixture of cases) {
  const started = performance.now();
  let error: string | null = null;
  try { await verifyZip(await Deno.readFile(`${root}/${fixture.name}.zip`), fixture.id); }
  catch (e) { error = e instanceof Error ? e.message : String(e); }
  const passed = error === fixture.expected_error;
  if (!passed) failures++;
  console.log(JSON.stringify({ case: fixture.name, passed, error, expected: fixture.expected_error,
    local_wall_ms: performance.now() - started }));
}
for (const invalid of ['{"a":1,"\\u0061":2}', '{"x":"\\ud800"}', '{"a":1} trailing']) {
  try { parseJson(new TextEncoder().encode(invalid)); failures++; } catch { /* Expected. */ }
}
if (failures) throw new Error(`${failures} local checks failed`);
