const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const FINGERPRINT = /^ed25519:[0-9a-f]{64}$/;
const page = document.body.dataset.page;
const base = new URL(page === "publication" ? "../" : "./", location.href);
const $ = (id) => document.getElementById(id);
let descriptor;
let categories = new Map();

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function safeBase(value) {
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password ||
      url.search || url.hash) throw new Error("Invalid service URL in instance descriptor.");
  if (location.protocol === "https:" && url.protocol !== "https:") {
    throw new Error("This instance requires HTTPS service URLs.");
  }
  return url.href.replace(/\/+$/, "") + "/";
}

function publicationURL(uuid) {
  if (!UUID.test(uuid)) throw new Error("Invalid publication UUID.");
  const url = new URL("publication/", base);
  url.searchParams.set("id", uuid);
  return url.href;
}

function link(label, url) {
  const node = element("a", label);
  node.href = url;
  node.rel = "noopener noreferrer";
  return node;
}

function showError(error) {
  $("problem").textContent = error instanceof Error ? error.message : String(error);
  $("problem").hidden = false;
}

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, {redirect: "error", credentials: "omit", ...options});
  if (!response.ok) throw new Error(`Request failed (HTTP ${response.status}). Please retry.`);
  return response.json();
}

async function catalog(resource, params = {}, signal) {
  const url = new URL(resource, safeBase(descriptor.catalog_url));
  for (const [name, value] of Object.entries(params)) url.searchParams.set(name, value);
  const headers = {"Accept-Profile": descriptor.catalog_schema, Accept: "application/json"};
  if (descriptor.supabase_publishable_key) headers.apikey = descriptor.supabase_publishable_key;
  return fetchJSON(url, {headers, signal});
}

function categoryNodes(codes) {
  const node = element("div", undefined, "categories");
  for (const [i, code] of (codes || []).entries()) {
    const item = categories.get(code);
    const url = new URL(base);
    url.searchParams.set("category", code);
    const anchor = link(item ? `${item.label} · ${code}` : code, url.href);
    if (i === 0) anchor.className = "primary";
    node.append(anchor);
  }
  return node;
}

function dateLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(undefined, {
    year: "numeric", month: "short", day: "numeric", timeZone: "UTC",
  }).format(date);
}

async function feed() {
  const params = new URLSearchParams(location.search);
  $("search").value = params.get("q") || "";
  $("publisher").value = params.get("publisher") || "";
  const groups = new Map();
  for (const item of categories.values()) {
    if (!groups.has(item.group)) {
      const group = element("optgroup");
      group.label = item.group;
      groups.set(item.group, group);
      $("category").append(group);
    }
    const option = element("option", `${item.code} · ${item.label}`);
    option.value = item.code;
    groups.get(item.group).append(option);
  }
  $("category").value = categories.has(params.get("category")) ? params.get("category") : "";
  let last = null;
  let abort = null;
  let count = 0;
  const seen = new Set();

  async function load(reset = true) {
    if (reset) {
      abort?.abort();
      abort = new AbortController();
      last = null;
      count = 0;
      seen.clear();
      $("publications").replaceChildren(element("p", "Loading publications…", "empty"));
    }
    const signal = abort.signal;
    $("more").disabled = true;
    $("problem").hidden = true;
    try {
      const search = $("search").value.trim();
      const category = $("category").value;
      const publisher = $("publisher").value.trim();
      if (publisher && !FINGERPRINT.test(publisher)) {
        throw new Error("Use the full publisher fingerprint: ed25519: followed by 64 lowercase hex characters.");
      }
      const query = {
        select: "uuid,title,abstract,categories,author_fingerprint,accepted_at",
        order: "accepted_at.desc,uuid.desc", limit: 20,
      };
      if (search) query.search_document = `wfts(english).${search}`;
      if (category) query.categories = `cs.{${category}}`;
      if (publisher) query.author_fingerprint = `eq.${publisher}`;
      if (last) query.or = `(accepted_at.lt.${last.accepted_at},and(accepted_at.eq.${last.accepted_at},uuid.lt.${last.uuid}))`;
      const rows = await catalog("publications", query, signal);
      if (signal.aborted) return;
      if (reset) $("publications").replaceChildren();
      for (const row of rows) {
        if (!UUID.test(row.uuid) || seen.has(row.uuid)) continue;
        seen.add(row.uuid);
        count++;
        const article = element("article", undefined, "pub");
        article.append(categoryNodes(row.categories));
        const title = link(row.title, publicationURL(row.uuid));
        title.className = "title";
        article.append(title, element("p", row.abstract, "summary"));
        const details = element("div", undefined, "pub-meta");
        details.append(element("span", `Accepted ${dateLabel(row.accepted_at)}`));
        const publisherURL = new URL(base);
        publisherURL.searchParams.set("publisher", row.author_fingerprint);
        const identity = link(`${row.author_fingerprint.slice(0, 24)}…`, publisherURL.href);
        identity.className = "identity";
        identity.title = row.author_fingerprint;
        details.append(identity);
        article.append(details);
        $("publications").append(article);
      }
      if (!count) $("publications").append(element("p", "No publications match these filters.", "empty"));
      if (rows.length) last = rows.at(-1);
      $("more").hidden = rows.length < 20;
      $("result-note").textContent = count ? `· ${count} shown` : "";
      const url = new URL(base);
      if (search) url.searchParams.set("q", search);
      if (category) url.searchParams.set("category", category);
      if (publisher) url.searchParams.set("publisher", publisher);
      history.replaceState(null, "", url);
    } catch (error) {
      if (error.name !== "AbortError") {
        if (reset) $("publications").replaceChildren();
        showError(error);
      }
    } finally {
      if (!signal.aborted) $("more").disabled = false;
    }
  }
  $("filters").addEventListener("submit", (event) => {event.preventDefault(); load();});
  $("category").addEventListener("change", () => load());
  $("refresh").addEventListener("click", () => load());
  $("more").addEventListener("click", () => load(false));
  await load();
}

function objectURL(publication, name) {
  const url = publication.objects?.[name];
  if (typeof url !== "string") return null;
  const expected = new URL(`${publication.uuid}/${name}`, safeBase(descriptor.archive_url)).href;
  if (new URL(url).href !== expected) throw new Error("Catalog object URL differs from the configured archive.");
  return expected;
}

async function readText(url) {
  const response = await fetch(url, {redirect: "error", credentials: "omit"});
  if (!response.ok) throw new Error(`Publication text is unavailable (HTTP ${response.status}).`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", {fatal: true});
  let count = 0;
  let text = "";
  try {
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      count += value.length;
      if (count > 1000000) throw new Error("Publication text exceeds the README size limit.");
      text += decoder.decode(value, {stream: true});
    }
    return text + decoder.decode();
  } finally {
    await reader.cancel();
  }
}

export function renderREADME(node, source) {
  try {
    if (!window.markdownit || !window.DOMPurify?.isSupported) throw new Error("Renderer unavailable");
    const markdown = window.markdownit({html: false, linkify: false});
    markdown.disable("image");
    const clean = window.DOMPurify.sanitize(markdown.render(source), {
      USE_PROFILES: {html: true},
      FORBID_TAGS: ["img"],
      FORBID_ATTR: ["style"],
      ALLOWED_URI_REGEXP: /^(?:https?:|mailto:)/i,
    });
    // Only the sanitizer's result reaches this HTML sink. No post-processing.
    node.innerHTML = clean;
    node.classList.remove("plain");
  } catch {
    node.textContent = source;
    node.classList.add("plain");
  }
}

function citationLink(target) {
  if (target.startsWith("sci:") && UUID.test(target.slice(4))) {
    return link(target, publicationURL(target.slice(4)));
  }
  if (/^doi:/i.test(target) && target.length <= 512 && /^[\x21-\x7e]+$/.test(target) &&
      !/[?#\\]/.test(target)) {
    return link(target, `https://doi.org/${target.slice(4)}`);
  }
  return element("span", target);
}

function citationNode(citation, source = false, available = null) {
  const node = element("div", undefined, "citation");
  if (citation.relation) node.append(element("span", citation.relation, "relation"));
  if (source && UUID.test(citation.source_uuid)) {
    node.append(link(citation.publications?.title || citation.source_uuid, publicationURL(citation.source_uuid)));
  } else {
    const anchor = citationLink(citation.target);
    anchor.classList.add("target");
    node.append(anchor);
    if (citation.target.startsWith("sci:") && available) {
      const target = available.get(citation.target.slice(4));
      node.append(element("p", target?.title || "Reference retained · not available in this catalog"));
    }
  }
  if (citation.reason) node.append(element("p", citation.reason));
  return node;
}

async function citations(uuid) {
  let after = -1;
  const outgoing = [];
  while (true) {
    const rows = await catalog("citations", {
      source_uuid: `eq.${uuid}`, position: `gt.${after}`, order: "position.asc", limit: 100,
      select: "source_uuid,position,target,relation,reason",
    });
    outgoing.push(...rows);
    if (rows.length < 100) break;
    after = rows.at(-1).position;
  }
  const targetIDs = [...new Set(outgoing.map(c => c.target.startsWith("sci:") ? c.target.slice(4) : "")
    .filter(id => UUID.test(id)))];
  const available = new Map();
  for (let i = 0; i < targetIDs.length; i += 100) {
    const rows = await catalog("publications", {uuid: `in.(${targetIDs.slice(i,i+100).join(",")})`, select: "uuid,title", limit: 100});
    for (const row of rows) available.set(row.uuid, row);
  }
  $("outgoing-count").textContent = String(outgoing.length);
  $("outgoing").replaceChildren(...outgoing.map(c => citationNode(c, false, available)));
  if (!outgoing.length) $("outgoing").append(element("p", "No references were declared.", "small"));
  const counts = await catalog("citation_counts", {target_key: `eq.sci:${uuid}`, select: "citing_publications"});
  $("incoming-count").textContent = `${counts[0]?.citing_publications || 0} publications`;
  let last = null;
  async function incoming() {
    $("incoming-more").disabled = true;
    try {
      const query = {
        target_key: `eq.sci:${uuid}`, order: "source_uuid.asc,position.asc", limit: 100,
        select: "source_uuid,position,target,relation,reason,publications(uuid,title)",
      };
      if (last) query.or = `(source_uuid.gt.${last.source_uuid},and(source_uuid.eq.${last.source_uuid},position.gt.${last.position}))`;
      const rows = await catalog("citations", query);
      $("incoming").append(...rows.map(c => citationNode(c, true)));
      if (!last && !rows.length) $("incoming").append(element("p", "No incoming citations on this instance.", "small"));
      if (rows.length) last = rows.at(-1);
      $("incoming-more").hidden = rows.length < 100;
    } catch (error) { showError(error); }
    finally { $("incoming-more").disabled = false; }
  }
  $("incoming-more").addEventListener("click", incoming);
  await incoming();
}

async function publication() {
  const uuid = new URLSearchParams(location.search).get("id") || "";
  if (!UUID.test(uuid)) throw new Error("Use a publication link with a canonical lowercase UUIDv4 in the id parameter.");
  const rows = await catalog("publications", {uuid: `eq.${uuid}`, limit: 1, select: "uuid,title,abstract,categories,keywords,headline_figure_caption,author_fingerprint,created_at,accepted_at,manifest_sha256,files_tree,objects"});
  if (!rows.length) throw new Error("This publication is not available in this catalog.");
  const record = rows[0];
  $("title").textContent = record.title;
  document.title = `${record.title} · sci`;
  $("abstract").textContent = record.abstract;
  $("categories").replaceWith(Object.assign(categoryNodes(record.categories), {id: "categories"}));
  $("keywords").textContent = (record.keywords || []).join(" · ");
  $("created").textContent = `Author's date: ${dateLabel(record.created_at)}`;
  $("created").title = record.created_at;
  $("accepted").textContent = `Accepted here: ${dateLabel(record.accepted_at)}`;
  $("accepted").title = record.accepted_at;
  $("uuid").textContent = uuid;
  $("fingerprint").textContent = record.author_fingerprint;
  $("manifest-hash").textContent = record.manifest_sha256;
  $("pull-command").textContent = `sci pull ${uuid}`;
  for (const [name, label] of [
    ["README.md", "README · original text"], ["files.zip", "Supporting files · ZIP"],
    ["headline_figure.png", "Headline figure · PNG"], ["sci.json", "Author metadata · JSON"],
    ["manifest.json", "Signed manifest · JSON"], ["manifest.json.sig", "Detached signature"],
    ["LICENSE", "Publication license"],
  ]) {
    const url = objectURL(record, name);
    if (url) $("downloads").append(link(label, url));
  }
  const figure = objectURL(record, "headline_figure.png");
  if (figure) {
    $("headline-image").src = figure;
    $("headline").hidden = false;
    if (record.headline_figure_caption) {
      $("caption").textContent = record.headline_figure_caption;
      $("caption").hidden = false;
    }
  }
  if (objectURL(record, "files.zip")) {
    $("file-list-section").hidden = false;
    $("file-list").textContent = record.files_tree?.paths?.join("\n") || "";
    $("file-list-note").textContent = record.files_tree
      ? (record.files_tree.truncated ? "More entries omitted" : (record.files_tree.paths.length ? "" : "Empty file listing"))
      : "File listing unavailable";
  }
  $("content").hidden = false;
  const results = await Promise.allSettled([
    readText(objectURL(record, "README.md")).then(source => renderREADME($("readme"), source)),
    citations(uuid),
  ]);
  for (const result of results) {
    if (result.status === "rejected") showError(result.reason);
  }
}

async function start() {
  descriptor = await fetchJSON(new URL("sci-server.json", base));
  if (descriptor.schema_version !== 1 || descriptor.catalog_schema !== "catalog_v1") {
    throw new Error("Unsupported instance descriptor.");
  }
  safeBase(descriptor.catalog_url);
  safeBase(descriptor.archive_url);
  const key = descriptor.supabase_publishable_key;
  if (key && !(/^sb_publishable_[A-Za-z0-9_-]+$/.test(key) || /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(key))) {
    throw new Error("The descriptor must use a public Supabase publishable key.");
  }
  try {
    const taxonomy = await fetchJSON(new URL("categories.json", base));
    const groupLabels = new Map((taxonomy.groups || []).map(group => [group.id, group.label]));
    categories = new Map(taxonomy.categories.map(item => [item.code, {...item, group: groupLabels.get(item.group) || item.group}]));
  } catch { /* Historical category codes remain readable if labels are unavailable. */ }
  await (page === "publication" ? publication() : feed());
  $("connection").textContent = "Public catalog";
  $("connection").classList.add("ready");
}

start().catch(error => {
  $("connection").textContent = "Catalog unavailable";
  if (page === "feed") $("publications").replaceChildren();
  showError(error);
});
