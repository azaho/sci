import {
  Invalid,
  Incomplete,
  parseJSON,
  schema,
  keys,
  requireValue,
  type ObjectValue,
} from "./index.js";
export function absolute(value: unknown): string {
  requireValue(
    typeof value === "string" &&
      value.length > 0 &&
      !/[^\x21-\x7e]/.test(value) &&
      !/%(?![0-9a-fA-F]{2})/.test(value) &&
      !value.includes("\\"),
    "absolute URL",
  );
  try {
    const u = new URL(value);
    requireValue(u.hostname && !u.username && !u.password, "URL authority");
  } catch {
    throw new Invalid("absolute URL");
  }
  return value;
}
export function descriptor(
  raw: Uint8Array,
  kind: "index" | "archive",
): ObjectValue {
  const o = parseJSON(raw);
  schema(o, "sci-" + kind + "/0.1");
  const rules: Record<string, string[]> =
    kind === "index"
      ? {
          publication_lookup_url: ["id"],
          revocation_lookup_url: ["x", "file"],
          timestamp_lookup_url: ["id"],
          submit_url: [],
          policy_url: [],
          docs_url: [],
        }
      : {
          bundle_url: ["id", "id2"],
          entry_url: ["id", "id2", "+path"],
          blob_url: ["sha256"],
          list_url: [],
        };
  if (kind === "index")
    requireValue(
      [
        "publication_lookup_url",
        "revocation_lookup_url",
        "timestamp_lookup_url",
      ].every((k) => k in o),
      "index routes",
    );
  else requireValue("bundle_url" in o || "entry_url" in o, "archive route");
  for (const [name, allowed] of Object.entries(rules)) {
    if (!(name in o)) continue;
    const value = o[name];
    requireValue(typeof value === "string", "template type");
    const expressions = [...value.matchAll(/\{([^{}]*)\}/g)].map((m) => m[1]);
    const plain = value.replace(/\{[^{}]*\}/g, "x");
    requireValue(
      !/[{}]/.test(plain) && expressions.every((e) => allowed.includes(e)),
      "template expression",
    );
    if (kind === "index")
      requireValue(
        allowed.every((e) => expressions.includes(e)),
        "required expression",
      );
    absolute(plain);
  }
  return o;
}
export function expandTemplate(
  template: string,
  values: Record<string, string>,
): string {
  return template.replace(/\{([^{}]+)\}/g, (_match, e: string) => {
    const value = values[e.replace(/^\+/, "")];
    if (value === undefined) throw new Invalid("missing template variable");
    return e.startsWith("+")
      ? value.split("/").map(encodeURIComponent).join("/")
      : encodeURIComponent(value);
  });
}
export function submission(raw: Uint8Array): ObjectValue {
  const o = parseJSON(raw);
  keys(o, ["id", "descriptors"], ["account"]);
  requireValue(
    typeof o.id === "string" &&
      o.id.length === 64 &&
      /^[0-9a-f]{64}$/.test(o.id),
    "submission ID",
  );
  requireValue(
    Array.isArray(o.descriptors) && o.descriptors.length > 0,
    "descriptor array",
  );
  for (const d of o.descriptors) absolute(d);
  requireValue(
    new Set(o.descriptors).size === o.descriptors.length,
    "duplicate descriptor",
  );
  if ("account" in o) {
    keys(o.account, ["type", "id"]);
    requireValue(
      typeof o.account.type === "string" && typeof o.account.id === "string",
      "account strings",
    );
  }
  return o;
}
export function submissionResponse(
  raw: Uint8Array,
  status: number,
  contentType: string,
  id: string,
): ObjectValue {
  requireValue(
    contentType.split(";", 1)[0] === "application/json",
    "content type",
  );
  const o = parseJSON(raw);
  requireValue(
    o !== null && typeof o === "object" && !Array.isArray(o),
    "response object",
  );
  if (status === 200 || status === 201)
    requireValue(o.id === id, "response ID");
  else {
    requireValue(status >= 400 && status <= 599, "error status");
    requireValue(
      o.error &&
        typeof o.error.code === "string" &&
        typeof o.error.message === "string" &&
        typeof o.error.retryable === "boolean",
      "error contract",
    );
  }
  return o;
}
export function lookupSources(raw: Uint8Array, status: number): string[] {
  if (status === 404) return [];
  if (status !== 200) throw new Incomplete("lookup unavailable");
  const o = parseJSON(raw);
  requireValue(o && Array.isArray(o.descriptors), "lookup descriptors");
  return o.descriptors.map(absolute);
}
