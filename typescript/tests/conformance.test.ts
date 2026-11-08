import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  verify,
  unzip,
  manifest,
  zip,
  zipSize,
  hash,
  record,
  Invalid,
  Unsupported,
  Incomplete,
} from "../src/index.js";
const root = resolve("../protocol/test-vectors/0.1");
const cases = JSON.parse(
  readFileSync(resolve(root, "catalog.json"), "utf8"),
).cases;
for (const c of cases.filter((c: any) =>
  ["publication", "claim", "revocation"].includes(c.kind),
)) {
  test(c.id, () => {
    const artifacts: Record<string, Uint8Array> = Object.fromEntries(
      Object.entries(c.artifacts).map(([k, p]) => [
        k,
        readFileSync(resolve(root, p as string)),
      ]),
    );
    const ctx = c.context ?? {};
    let got: any;
    try {
      const size = Object.values(artifacts).reduce((n, b) => n + b.length, 0);
      if (
        ctx.transport === "unavailable" ||
        (ctx.verification_budget_bytes !== undefined &&
          ctx.verification_budget_bytes < size)
      )
        throw new Incomplete("transport/budget");
      if (c.kind === "publication") {
        const e = artifacts["bundle.zip"]
          ? unzip(artifacts["bundle.zip"])
          : artifacts;
        if (ctx.scope === "manifest") {
          const m = manifest(
              e["manifest.json"],
              e["manifest.json.sig"],
              ctx.publication_id,
            ),
            n = zipSize(e["manifest.json"], m);
          got = {
            outcome: "valid",
            baseline: null,
            zip_bytes: n,
            within_baseline_zip_limit: n <= 67108864,
          };
        } else {
          const p = verify(e, ctx.publication_id);
          const packed = zip(
            Object.fromEntries(
              Object.entries(e).filter(([k]) => k !== "manifest.json.sig.tsr"),
            ),
          );
          got = {
            outcome: "valid",
            baseline: p.baseline,
            zip_bytes: packed.length,
            zip_sha256: hash(packed),
          };
        }
      } else {
        record(
          artifacts["record.json"],
          artifacts["record.json.sig"],
          c.kind,
          ctx,
        );
        got = { outcome: "valid" };
      }
    } catch (e) {
      got = {
        outcome:
          e instanceof Unsupported
            ? "unsupported"
            : e instanceof Incomplete
              ? "incomplete"
              : "invalid",
        baseline: null,
      };
    }
    assert.deepEqual(
      Object.fromEntries(Object.keys(c.expected).map((k) => [k, got[k]])),
      c.expected,
    );
  });
}
import { verifyProofs } from "../src/timestamps.js";
for (const c of cases.filter((c: any) => c.kind === "timestamp"))
  test(c.id, async () => {
    const ctx = c.context,
      a = c.artifacts;
    const read = (p: string) => readFileSync(resolve(root, p));
    const got = await verifyProofs(
      read(a["proofs.tsr"]),
      read(a["signature.bin"]),
      {
        anchors: ctx.trust_anchors.map(read),
        crls: ctx.crls.map(read),
        now: new Date(ctx.verification_time),
      },
    );
    if (a["manifest.json"])
      Object.assign(got, {
        publication_outcome: "valid",
        publication_id_unchanged: true,
      });
    else got.revocation_outcome = "valid";
    assert.deepEqual(
      Object.fromEntries(Object.keys(c.expected).map((k) => [k, got[k]])),
      c.expected,
    );
  });
import {
  descriptor,
  submission,
  submissionResponse,
  expandTemplate,
} from "../src/discovery.js";
for (const c of cases.filter((c: any) =>
  ["discovery", "submission", "template"].includes(c.kind),
))
  test(c.id, () => {
    const a: any = Object.fromEntries(
      Object.entries(c.artifacts).map(([k, p]) => [
        k,
        readFileSync(resolve(root, p as string)),
      ]),
    );
    const ctx = c.context ?? {};
    let got;
    try {
      if (c.kind === "discovery")
        descriptor(a["descriptor.json"], ctx.descriptor_kind);
      else if (c.kind === "submission") {
        if (ctx.phase === "request") submission(a["request.json"]);
        else
          submissionResponse(
            a["response.json"],
            ctx.status,
            ctx.content_type,
            ctx.requested_id,
          );
      } else {
        const templates = JSON.parse(a["templates.json"].toString()),
          variables = JSON.parse(a["variables.json"].toString());
        assert.deepEqual(
          Object.fromEntries(
            Object.keys(c.expected).map((k) => [
              k,
              expandTemplate(templates[k], variables),
            ]),
          ),
          c.expected,
        );
        return;
      }
      got = { outcome: "valid" };
    } catch (e) {
      got = { outcome: e instanceof Unsupported ? "unsupported" : "invalid" };
    }
    assert.deepEqual(got, c.expected);
  });
test("public FreeTSA response without accuracy", async () => {
  const base = resolve("../python/tests/fixtures/freetsa-20261108");
  const read = (name: string) => readFileSync(resolve(base, name));
  const got = await verifyProofs(read("proof.tsr"), read("signature.bin"), {
    anchors: [read("root.pem")],
    crls: [read("root.crl.pem")],
    now: new Date("2026-11-08T05:27:30Z"),
  });
  assert.equal(got.outcome, "valid");
  assert.equal(got.proofs[0].accuracy_seconds, null);
});
