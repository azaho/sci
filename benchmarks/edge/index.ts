import { LIMITS, verifyZip } from "./verify.ts";

// Replaced in the deployed copy, not in the repository.
const fixtureConfig = /* FIXTURE_CONFIG */ { bucket: "", cases: [] as any[] };
let active = 0;
Deno.serve(async (request) => {
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!serviceKey || request.headers.get("authorization") !== `Bearer ${serviceKey}`) {
    return new Response("Unauthorized", { status: 401 });
  }
  if (request.method !== "POST") return new Response("POST required", { status: 405 });
  const requestBody = await request.text();
  if (requestBody.length > 1024) return new Response("Request too large", { status: 413 });
  let input;
  try { input = JSON.parse(requestBody); } catch { return new Response("Invalid JSON", { status: 400 }); }
  const fixture = fixtureConfig.cases.find(c => c.name === input.case);
  if (!fixture) return new Response("Unknown fixture", { status: 400 });
  // Explicitly serialize within one isolate. Separate isolates may still run concurrently.
  if (active) return new Response("Busy", { status: 429 });
  active++;
  const started = performance.now();
  let verifyStarted: number | undefined;
  const peak: Record<string, number> = {};
  const sample = () => {
    try { for (const [k, v] of Object.entries(Deno.memoryUsage())) peak[k] = Math.max(peak[k] || 0, v); }
    catch { /* Memory samples may not be exposed by the hosted runtime. */ }
  };
  sample();
  const initialMemory = { ...peak };
  const result: Record<string, unknown> = {
    case: fixture.name, run: input.run, region: Deno.env.get("SB_REGION"),
    execution_id: Deno.env.get("SB_EXECUTION_ID"), runtime: Deno.version,
    zip_bytes: fixture.zip_bytes,
  };
  try {
    if (fixture.zip_bytes > LIMITS.zip) throw new Error("zip_limit");
    // One bounded buffer; never concatenate a list of whole-response copies.
    const bytes = new Uint8Array(fixture.zip_bytes);
    let written = 0;
    for (const part of fixture.parts) {
      const url = `${Deno.env.get("SUPABASE_URL")}/storage/v1/object/authenticated/${fixtureConfig.bucket}/${part}`;
      const response = await fetch(url, {
        headers: { authorization: `Bearer ${serviceKey}`, apikey: serviceKey },
        redirect: "error", signal: AbortSignal.timeout(30_000),
      });
      if (!response.ok || !response.body) throw new Error(`source_${response.status}`);
      const reader = response.body.getReader();
      try {
        for (;;) {
          const { value, done } = await reader.read(); if (done) break;
          if (written + value.length > bytes.length) throw new Error("download_limit");
          bytes.set(value, written); written += value.length; sample();
        }
      } finally { await reader.cancel(); reader.releaseLock(); }
    }
    if (written !== bytes.length) throw new Error("download_size");
    result.download_ms = performance.now() - started;
    verifyStarted = performance.now();
    result.verification = await verifyZip(bytes, fixture.id, sample);
    result.accepted = true;
  } catch (error) {
    result.accepted = false;
    result.error = error instanceof Error ? error.message : "error";
  } finally {
    sample(); active--;
  }
  result.verify_wall_ms = verifyStarted === undefined ? null : performance.now() - verifyStarted;
  result.total_wall_ms = performance.now() - started;
  result.memory_before = initialMemory;
  result.memory_sampled_max = peak;
  // Wall time is NOT CPU time. Runtime shutdown telemetry is collected separately.
  console.log(JSON.stringify({ sci_benchmark: result }));
  return Response.json(result);
});
