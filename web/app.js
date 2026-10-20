/*
 * SCI static website: feed (index.html) and reader (publication/index.html).
 * Plain browser JavaScript, no framework or build step.
 *
 * Safety rules (TEST_PLAN.md "Safe rendering and delivery"):
 * - Publisher-controlled strings and API errors are inserted only as text
 *   (textContent / text nodes). No HTML strings are built from data.
 * - Links are built only from validated identifiers and http(s) URLs.
 * - The only innerHTML assignment is the README container, and it receives
 *   only DOMPurify output of markdown-it output (raw HTML and images disabled).
 * - The optional publishable key is sent only to catalog_url requests.
 */
(function () {
  'use strict';

  // Site base = directory containing this script. index.html loads "app.js" and
  // publication/index.html loads "../app.js", so both resolve the same base,
  // whether the site lives at a domain root or under a project subpath.
  var SCRIPT = document.currentScript;
  var BASE = new URL('./', (SCRIPT && SCRIPT.src) || document.baseURI);

  var UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  var SCI_TARGET_RE = /^sci:([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/;
  var DOI_TARGET_RE = /^doi:/i;
  var DOI_PATH_RE = /^[A-Za-z0-9\-._~!$&'()*+,;=:@\/%]+$/;
  var FP_RE = /^ed25519:[0-9a-f]{64}$/;
  var CATEGORY_RE = /^[A-Za-z0-9][A-Za-z0-9.\-]{0,63}$/;
  var SHA256_RE = /^[0-9a-f]{64}$/;
  var TIMESTAMP_RE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+\-][0-9]{2}(:?[0-9]{2})?)$/;
  var SCHEMA_RE = /^[A-Za-z_][A-Za-z0-9_]{0,62}$/;
  var KEY_RE = /^[\x21-\x7e]{1,512}$/;
  var RELATIONS = ['uses', 'supports', 'reproduces', 'challenges', 'supersedes'];
  var FEED_PAGE = 20;
  var CITATION_PAGE = 100;
  var MAX_CITATION_PAGES = 50;
  var FEED_SELECT = 'uuid,title,abstract,categories,keywords,author_fingerprint,accepted_at';
  var DETAIL_SELECT = 'uuid,manifest_sha256,title,abstract,categories,keywords,headline_figure_caption,' +
    'author_public_key,author_fingerprint,created_at,accepted_at,files_tree,files,objects';
  var ORIGINAL_FILES = ['sci.json', 'manifest.json', 'manifest.json.sig', 'LICENSE', 'README.md'];

  // ---------------------------------------------------------------- helpers

  function $(id) { return document.getElementById(id); }

  function hasOwn(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }

  function isObject(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }

  // Create an element. Text is always set through textContent.
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // Return a normalized http(s) URL string, or null.
  function safeUrl(value, base) {
    if (typeof value !== 'string' || value.length === 0 || value.length > 4096) return null;
    var url;
    try { url = new URL(value, base); } catch (e) { return null; }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
    if (url.username || url.password) return null;
    return url.href;
  }

  // A link whose href has already been validated by the caller.
  function link(href, text, className, external) {
    var a = el('a', className, text);
    a.href = href;
    if (external) a.rel = 'noopener noreferrer';
    return a;
  }

  function siteUrl(path) { return new URL(path, BASE).href; }
  function publicationHref(uuid) { return siteUrl('publication/?id=' + uuid); }
  function authorHref(fp) { return siteUrl('./?author=' + encodeURIComponent(fp)); }
  function categoryHref(code) { return siteUrl('./?category=' + encodeURIComponent(code)); }

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  // ISO-like UTC display, e.g. "2026-10-20 08:45:12 UTC".
  function formatTime(value, withSeconds) {
    if (typeof value !== 'string') return '';
    var d = new Date(value);
    if (isNaN(d.getTime())) return value;
    var s = d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) + ' ' +
      pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes());
    if (withSeconds) s += ':' + pad(d.getUTCSeconds());
    return s + ' UTC';
  }

  function timeEl(value, withSeconds) {
    var t = el('time', null, formatTime(value, withSeconds));
    if (typeof value === 'string') t.dateTime = value;
    return t;
  }

  function formatBytes(n) {
    if (typeof n !== 'number' || !isFinite(n) || n < 0) return '';
    var exact = n.toLocaleString('en-US') + ' bytes';
    if (n < 1000) return exact;
    var units = ['kB', 'MB', 'GB', 'TB'];
    var v = n, i = -1;
    do { v /= 1000; i++; } while (v >= 1000 && i < units.length - 1);
    return (v >= 100 ? v.toFixed(0) : v.toFixed(1)) + ' ' + units[i] + ' (' + exact + ')';
  }

  function shortFingerprint(fp) { return fp.slice(0, 8 + 12) + '…'; }

  function setStatus(node, text, isError) {
    if (!node) return;
    node.textContent = text || '';
    node.classList.toggle('error', !!isError);
  }

  function errorText(err) {
    if (err && typeof err.message === 'string' && err.message) return err.message;
    return 'Unexpected error.';
  }

  // ------------------------------------------------- instance configuration

  var configPromise = null;
  var categoriesPromise = null;

  function loadConfig() {
    if (!configPromise) configPromise = fetchConfig();
    return configPromise;
  }

  async function fetchConfig() {
    var where = new URL('sci-server.json', BASE).href;
    var res;
    try {
      res = await fetch(where, { cache: 'no-cache', credentials: 'same-origin' });
    } catch (e) {
      throw new Error('Could not load the instance descriptor (sci-server.json).');
    }
    if (!res.ok) throw new Error('Could not load the instance descriptor (sci-server.json): HTTP ' + res.status + '.');
    var d;
    try { d = await res.json(); } catch (e) { throw new Error('The instance descriptor is not valid JSON.'); }
    if (!isObject(d) || d.schema_version !== 1) throw new Error('Unsupported instance descriptor: schema_version must be 1.');
    var catalog = safeUrl(d.catalog_url);
    var archive = safeUrl(d.archive_url);
    if (!catalog) throw new Error('Instance descriptor: catalog_url must be an http(s) URL.');
    if (!archive) throw new Error('Instance descriptor: archive_url must be an http(s) URL.');
    if (!safeUrl(d.publishing_api_url)) throw new Error('Instance descriptor: publishing_api_url must be an http(s) URL.');
    if (typeof d.catalog_schema !== 'string' || !SCHEMA_RE.test(d.catalog_schema)) {
      throw new Error('Instance descriptor: catalog_schema is missing or invalid.');
    }
    var key = null;
    if (hasOwn(d, 'supabase_publishable_key') && d.supabase_publishable_key !== null) {
      if (typeof d.supabase_publishable_key !== 'string' || !KEY_RE.test(d.supabase_publishable_key)) {
        throw new Error('Instance descriptor: supabase_publishable_key is invalid.');
      }
      key = d.supabase_publishable_key;
    }
    return {
      catalog: catalog.replace(/\/+$/, ''),
      archive: archive.replace(/\/+$/, ''),
      schema: d.catalog_schema,
      key: key
    };
  }

  // Optional categories.json beside the site; raw codes are shown without it.
  function loadCategories() {
    if (!categoriesPromise) {
      categoriesPromise = (async function () {
        var out = { labels: new Map(), groups: new Map(), list: [] };
        try {
          var res = await fetch(new URL('categories.json', BASE).href, { credentials: 'same-origin' });
          if (!res.ok) return out;
          var d = await res.json();
          if (!isObject(d)) return out;
          if (Array.isArray(d.groups)) {
            d.groups.forEach(function (g) {
              if (isObject(g) && typeof g.id === 'string' && typeof g.label === 'string') out.groups.set(g.id, g.label);
            });
          }
          if (Array.isArray(d.categories)) {
            d.categories.forEach(function (c) {
              if (!isObject(c) || typeof c.code !== 'string' || !CATEGORY_RE.test(c.code)) return;
              var label = typeof c.label === 'string' ? c.label : c.code;
              out.labels.set(c.code, label);
              out.list.push({ code: c.code, label: label, group: typeof c.group === 'string' ? c.group : '', historical: c.historical === true });
            });
          }
        } catch (e) { /* optional file */ }
        return out;
      })();
    }
    return categoriesPromise;
  }

  // ----------------------------------------------------------- catalog API

  function CatalogError(message, status) {
    this.name = 'CatalogError';
    this.message = message;
    this.status = status;
  }
  CatalogError.prototype = Object.create(Error.prototype);

  // params: array of [name, value]; values are URL-encoded here.
  function query(params) {
    return params.map(function (p) { return p[0] + '=' + encodeURIComponent(p[1]); }).join('&');
  }

  async function catalogGet(cfg, path, params) {
    var url = cfg.catalog + '/' + path + (params && params.length ? '?' + query(params) : '');
    var headers = { 'Accept': 'application/json', 'Accept-Profile': cfg.schema };
    if (cfg.key) headers.apikey = cfg.key; // only ever sent to catalog_url
    var res;
    try {
      res = await fetch(url, { headers: headers, credentials: 'omit', cache: 'no-cache' });
    } catch (e) {
      throw new CatalogError('The catalog could not be reached.', 0);
    }
    var body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (!res.ok) {
      var msg = isObject(body) && typeof body.message === 'string' && body.message ? body.message
        : isObject(body) && typeof body.msg === 'string' && body.msg ? body.msg
          : 'HTTP ' + res.status;
      throw new CatalogError('Catalog error: ' + msg, res.status);
    }
    if (body === null) throw new CatalogError('Catalog error: response is not JSON.', res.status);
    return body;
  }

  async function catalogRows(cfg, path, params) {
    var body = await catalogGet(cfg, path, params);
    if (!Array.isArray(body)) throw new CatalogError('Catalog error: expected a list of rows.', 200);
    return body.filter(isObject);
  }

  // Resolve a publication object URL: the catalog's objects map, falling back to
  // archive_url/<uuid>/<name>. Only http(s) URLs are returned.
  function objectUrl(cfg, pub, name) {
    if (isObject(pub.objects) && hasOwn(pub.objects, name)) {
      var u = safeUrl(pub.objects[name]);
      if (u) return u;
    }
    return safeUrl(cfg.archive + '/' + pub.uuid + '/' + name);
  }

  function showInstance(cfg) {
    var pill = $('instance');
    if (!pill) return;
    pill.textContent = 'Catalog: ' + new URL(cfg.catalog).host;
    pill.classList.add('ok');
  }

  function showInstanceError() {
    var pill = $('instance');
    if (!pill) return;
    pill.textContent = 'Instance unavailable';
    pill.classList.add('bad');
  }

  function categoryChips(list, categories, linked) {
    var ul = el('ul', 'chips');
    if (!Array.isArray(list)) return ul;
    list.forEach(function (code, i) {
      if (typeof code !== 'string') return;
      var li = el('li');
      var cls = 'chip' + (i === 0 ? ' primary' : '');
      var label = categories.labels.get(code);
      var chip = linked && CATEGORY_RE.test(code) ? link(categoryHref(code), code, cls) : el('span', cls, code);
      chip.title = (label ? label + ' · ' : '') + (i === 0 ? 'primary category' : 'secondary category');
      li.appendChild(chip);
      ul.appendChild(li);
    });
    return ul;
  }

  // ================================================================== home

  var feed = { generation: 0, cursor: null, filters: null, count: 0 };

  function readFilters() {
    var p = new URLSearchParams(location.search);
    var q = (p.get('q') || '').trim().slice(0, 200);
    var category = p.get('category') || '';
    var author = p.get('author') || '';
    return {
      q: q,
      category: CATEGORY_RE.test(category) ? category : '',
      badCategory: category && !CATEGORY_RE.test(category) ? category : '',
      author: FP_RE.test(author) ? author : '',
      badAuthor: author && !FP_RE.test(author) ? author : ''
    };
  }

  function fillCategorySelect(select, categories, selected) {
    var byGroup = new Map();
    var historical = [];
    categories.list.forEach(function (c) {
      if (c.historical) { historical.push(c); return; }
      if (!byGroup.has(c.group)) byGroup.set(c.group, []);
      byGroup.get(c.group).push(c);
    });
    function option(c) {
      var o = el('option', null, c.code + ' — ' + c.label);
      o.value = c.code;
      return o;
    }
    byGroup.forEach(function (items, group) {
      var og = document.createElement('optgroup');
      og.label = categories.groups.get(group) || group || 'Other';
      items.forEach(function (c) { og.appendChild(option(c)); });
      select.appendChild(og);
    });
    if (historical.length) {
      var hg = document.createElement('optgroup');
      hg.label = 'Historical codes';
      historical.forEach(function (c) { hg.appendChild(option(c)); });
      select.appendChild(hg);
    }
    if (selected && !categories.labels.has(selected)) {
      var o = el('option', null, selected);
      o.value = selected;
      select.appendChild(o);
    }
    select.value = selected || '';
  }

  function applyFilters(next) {
    var p = new URLSearchParams();
    if (next.q) p.set('q', next.q);
    if (next.category) p.set('category', next.category);
    if (next.author) p.set('author', next.author);
    var s = p.toString();
    location.assign(siteUrl('./' + (s ? '?' + s : '')));
  }

  async function initHome() {
    var filters = readFilters();
    feed.filters = filters;
    var form = $('filters'), qInput = $('q'), select = $('category');
    qInput.value = filters.q;

    var instanceUrl = $('instance-url');
    if (instanceUrl) instanceUrl.textContent = BASE.href;

    if (filters.author) {
      $('active-filters').hidden = false;
      var code = $('author-filter');
      code.textContent = filters.author;
      code.title = filters.author;
    }

    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      applyFilters({ q: qInput.value.trim().slice(0, 200), category: select.value, author: filters.author });
    });
    $('load-more').addEventListener('click', function () { loadFeedPage(); });

    var categories = await loadCategories();
    fillCategorySelect(select, categories, filters.category);

    if (filters.badAuthor || filters.badCategory) {
      setStatus($('feed-status'), filters.badAuthor
        ? 'The publisher filter must be a full fingerprint: ed25519: followed by 64 lowercase hex characters.'
        : 'The category filter is not a valid category code.', true);
      return;
    }

    var cfg;
    try {
      cfg = await loadConfig();
    } catch (err) {
      showInstanceError();
      setStatus($('feed-status'), errorText(err), true);
      return;
    }
    showInstance(cfg);
    feed.cfg = cfg;
    feed.categories = categories;
    loadFeedPage();
  }

  async function loadFeedPage() {
    var gen = ++feed.generation;
    var f = feed.filters;
    var more = $('load-more');
    var status = $('feed-status');
    more.disabled = true;
    setStatus(status, feed.count ? 'Loading more…' : 'Loading publications…');

    var params = [['select', FEED_SELECT]];
    if (f.q) params.push(['search_document', 'wfts(english).' + f.q]);
    if (f.category) params.push(['categories', 'cs.{"' + f.category + '"}']);
    if (f.author) params.push(['author_fingerprint', 'eq.' + f.author]);
    if (feed.cursor) {
      var ts = feed.cursor.accepted_at, id = feed.cursor.uuid;
      params.push(['or', '(accepted_at.lt.' + ts + ',and(accepted_at.eq.' + ts + ',uuid.lt.' + id + '))']);
    }
    params.push(['order', 'accepted_at.desc,uuid.desc']);
    params.push(['limit', String(FEED_PAGE)]);

    var rows;
    try {
      rows = await catalogRows(feed.cfg, 'publications', params);
    } catch (err) {
      if (gen !== feed.generation) return;
      setStatus(status, errorText(err), true);
      more.disabled = false;
      more.hidden = !feed.count;
      return;
    }
    if (gen !== feed.generation) return;

    var list = $('feed');
    rows.forEach(function (row) {
      if (typeof row.uuid !== 'string' || !UUID_RE.test(row.uuid)) return;
      list.appendChild(renderFeedItem(row, feed.categories));
      feed.count++;
    });

    var last = rows[rows.length - 1];
    var canContinue = rows.length === FEED_PAGE && last && UUID_RE.test(String(last.uuid)) &&
      typeof last.accepted_at === 'string' && TIMESTAMP_RE.test(last.accepted_at);
    feed.cursor = canContinue ? { accepted_at: last.accepted_at, uuid: last.uuid } : null;
    more.hidden = !canContinue;
    more.disabled = false;

    if (feed.count === 0) {
      var filtered = f.q || f.category || f.author;
      setStatus(status, filtered ? 'No publications match these filters.' : 'No publications yet.');
    } else if (!canContinue) {
      setStatus(status, feed.count === 1 ? '1 publication.' : feed.count + ' publications.');
    } else {
      setStatus(status, '');
    }
  }

  function renderFeedItem(row, categories) {
    var li = el('li', 'pub');
    if (Array.isArray(row.categories) && row.categories.length) li.appendChild(categoryChips(row.categories, categories, true));
    li.appendChild(link(publicationHref(row.uuid), typeof row.title === 'string' ? row.title : row.uuid, 'title'));
    if (typeof row.abstract === 'string') li.appendChild(el('p', 'abstract', row.abstract));
    var signals = el('div', 'signals');
    if (typeof row.author_fingerprint === 'string' && FP_RE.test(row.author_fingerprint)) {
      var fp = link(authorHref(row.author_fingerprint), shortFingerprint(row.author_fingerprint), 'fp');
      fp.title = 'Show publications by ' + row.author_fingerprint;
      signals.appendChild(fp);
    }
    if (typeof row.accepted_at === 'string') {
      var acc = el('span', null, 'Accepted ');
      acc.appendChild(timeEl(row.accepted_at, false));
      signals.appendChild(acc);
    }
    if (Array.isArray(row.keywords) && row.keywords.length) {
      signals.appendChild(el('span', null, row.keywords.filter(function (k) { return typeof k === 'string'; }).join(' · ')));
    }
    li.appendChild(signals);
    return li;
  }

  // ================================================================ reader

  async function initReader() {
    var status = $('pub-status');
    var id = new URLSearchParams(location.search).get('id');
    if (id === null || id === '') {
      setStatus(status, 'No publication was requested. Open a publication from the feed, or add ?id=UUID to this page address.', true);
      return;
    }
    if (!UUID_RE.test(id)) {
      setStatus(status, 'This is not a valid publication ID. Publication IDs are canonical lowercase UUIDv4 values, for example c8af05c1-c45b-471c-9e84-c39bedb1a462.', true);
      return;
    }

    var cfg;
    try {
      cfg = await loadConfig();
    } catch (err) {
      showInstanceError();
      setStatus(status, errorText(err), true);
      return;
    }
    showInstance(cfg);
    var categoriesP = loadCategories();

    var rows;
    try {
      rows = await catalogRows(cfg, 'publications', [['uuid', 'eq.' + id], ['select', DETAIL_SELECT], ['limit', '1']]);
    } catch (err) {
      setStatus(status, errorText(err), true);
      return;
    }
    if (rows.length === 0 || rows[0].uuid !== id) {
      setStatus(status, 'Publication ' + id + ' is not available in this catalog. It may exist on another instance; try sci pull ' + id + ' --server URL.', true);
      return;
    }
    var pub = rows[0];
    var categories = await categoriesP;
    setStatus(status, '');
    renderPublication(cfg, pub, categories);
    loadReadme(cfg, pub);
    loadReferences(cfg, pub).catch(function (err) { setStatus($('refs-status'), errorText(err), true); });
    loadCitedBy(cfg, pub).catch(function (err) { setStatus($('citedby-status'), errorText(err), true); });
  }

  function renderPublication(cfg, pub, categories) {
    var title = typeof pub.title === 'string' ? pub.title : pub.uuid;
    document.title = title + ' · sci';
    $('pub-title').textContent = title;

    var catList = $('pub-categories');
    var chips = categoryChips(pub.categories, categories, true);
    while (chips.firstChild) catList.appendChild(chips.firstChild);
    if (!catList.firstChild) catList.hidden = true;

    $('pub-abstract').textContent = typeof pub.abstract === 'string' ? pub.abstract : '';

    var kw = $('pub-keywords');
    if (Array.isArray(pub.keywords)) {
      pub.keywords.forEach(function (k) { if (typeof k === 'string') kw.appendChild(el('li', null, k)); });
    }
    if (!kw.firstChild) kw.hidden = true;

    var publisher = $('pub-publisher');
    if (typeof pub.author_fingerprint === 'string' && FP_RE.test(pub.author_fingerprint)) {
      var a = link(authorHref(pub.author_fingerprint), pub.author_fingerprint, 'fp');
      a.title = 'Show all publications by this key';
      publisher.appendChild(a);
    } else {
      publisher.textContent = typeof pub.author_fingerprint === 'string' ? pub.author_fingerprint : 'Unknown';
    }
    $('pub-created').appendChild(timeEl(pub.created_at, true));
    $('pub-accepted').appendChild(timeEl(pub.accepted_at, true));
    $('pub-uuid').textContent = pub.uuid;

    var files = isObject(pub.files) ? pub.files : {};

    // Headline figure: only when declared in the signed file map.
    if (hasOwn(files, 'headline_figure.png')) {
      var figUrl = objectUrl(cfg, pub, 'headline_figure.png');
      if (figUrl) {
        var figure = el('figure', 'headline-figure');
        var img = document.createElement('img');
        var caption = typeof pub.headline_figure_caption === 'string' ? pub.headline_figure_caption : '';
        img.alt = caption || 'Headline figure';
        img.decoding = 'async';
        img.referrerPolicy = 'no-referrer';
        img.addEventListener('error', function () {
          figure.replaceChild(el('p', 'small error', 'The headline figure could not be loaded.'), img);
        });
        img.src = figUrl;
        figure.appendChild(img);
        if (caption) figure.appendChild(el('figcaption', null, caption));
        $('figure-slot').appendChild(figure);
      }
    }

    // Downloads and file listing.
    var downloads = $('downloads');
    if (hasOwn(files, 'files.zip')) {
      var zipUrl = objectUrl(cfg, pub, 'files.zip');
      var zip = isObject(files['files.zip']) ? files['files.zip'] : {};
      if (zipUrl) {
        var z = link(zipUrl, 'files.zip', 'download', true);
        z.setAttribute('download', '');
        downloads.appendChild(z);
      } else {
        downloads.appendChild(el('p', 'small', 'files.zip'));
      }
      if (typeof zip.bytes === 'number') downloads.appendChild(el('span', 'small', formatBytes(zip.bytes)));
      renderTree(pub.files_tree);
    } else {
      downloads.appendChild(el('p', 'small', 'No supporting files.'));
    }
    var licenseUrl = objectUrl(cfg, pub, 'LICENSE');
    if (licenseUrl) {
      var slot = $('license-slot');
      slot.appendChild(link(licenseUrl, "This publication's LICENSE", null, true));
      slot.appendChild(document.createTextNode(' · '));
    }

    // Signature information.
    $('pub-key').textContent = typeof pub.author_public_key === 'string' ? pub.author_public_key : '';
    $('pub-fp').textContent = typeof pub.author_fingerprint === 'string' ? pub.author_fingerprint : '';
    $('pub-manifest').textContent = typeof pub.manifest_sha256 === 'string' && SHA256_RE.test(pub.manifest_sha256) ? pub.manifest_sha256 : '';
    $('pull-cmd').textContent = 'sci server ' + BASE.href + '\nsci pull ' + pub.uuid;
    var originals = $('originals');
    ORIGINAL_FILES.forEach(function (name) {
      var u = objectUrl(cfg, pub, name);
      if (!u) return;
      var li = el('li');
      li.appendChild(link(u, name, null, true));
      originals.appendChild(li);
    });

    $('publication').hidden = false;
  }

  // files_tree from the signed manifest, rendered as plain text only.
  function renderTree(tree) {
    $('listing').hidden = false;
    if (!isObject(tree) || !Array.isArray(tree.paths)) {
      $('tree-unavailable').hidden = false;
      return;
    }
    var ul = $('tree');
    tree.paths.forEach(function (p) {
      if (typeof p !== 'string') return;
      var depth = p.replace(/\/+$/, '').split('/').length - 1;
      ul.appendChild(el('li', 'd' + Math.max(0, Math.min(2, depth)), p));
    });
    ul.hidden = !ul.firstChild;
    if (!ul.firstChild && tree.truncated !== true) $('tree-unavailable').hidden = false;
    if (tree.truncated === true) $('tree-omitted').hidden = false;
  }

  // README: fetched directly from the archive object (no catalog headers),
  // rendered with markdown-it (no raw HTML, no images) and sanitized by DOMPurify.
  async function loadReadme(cfg, pub) {
    var status = $('readme-status');
    var container = $('readme');
    var url = objectUrl(cfg, pub, 'README.md');
    if (!url) { setStatus(status, 'The main text is unavailable.', true); return; }
    var text;
    try {
      var res = await fetch(url, { credentials: 'omit' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      text = await res.text();
    } catch (err) {
      setStatus(status, 'The main text (README.md) could not be loaded: ' + errorText(err), true);
      return;
    }
    setStatus(status, '');
    renderReadme(container, text);
  }

  function renderReadme(container, text) {
    var clean = null;
    try {
      var markdownit = window.markdownit;
      var purify = window.DOMPurify;
      if (typeof markdownit === 'function' && purify && typeof purify.sanitize === 'function' && purify.isSupported !== false) {
        var md = markdownit({ html: false, linkify: false });
        md.disable('image');
        var rendered = md.render(text);
        var sanitized = purify.sanitize(rendered, { USE_PROFILES: { html: true }, FORBID_TAGS: ['img'], FORBID_ATTR: ['style'] });
        if (typeof sanitized === 'string') clean = sanitized;
      }
    } catch (e) {
      clean = null;
    }
    clear(container);
    if (clean === null) {
      container.appendChild(el('pre', 'readme-plain', text));
      return;
    }
    container.innerHTML = clean; // sanitized output only; not transformed afterwards
  }

  // ------------------------------------------------------------- citations

  function relationLabel(relation) {
    var known = typeof relation === 'string' && RELATIONS.indexOf(relation) !== -1;
    var label = typeof relation === 'string' && relation ? relation : 'cites';
    return el('span', 'rel' + (known ? ' ' + relation : ''), label);
  }

  function doiHref(target) {
    if (!DOI_TARGET_RE.test(target)) return null;
    var rest = target.slice(4);
    if (!rest || !DOI_PATH_RE.test(rest)) return null;
    return 'https://doi.org/' + rest;
  }

  async function loadReferences(cfg, pub) {
    var status = $('refs-status');
    var rows = [];
    var last = null;
    for (var page = 0; page < MAX_CITATION_PAGES; page++) {
      var params = [['source_uuid', 'eq.' + pub.uuid], ['select', 'position,target,target_key,relation,reason']];
      if (last !== null) params.push(['position', 'gt.' + last]);
      params.push(['order', 'position.asc'], ['limit', String(CITATION_PAGE)]);
      var batch = await catalogRows(cfg, 'citations', params);
      rows = rows.concat(batch);
      var tail = batch[batch.length - 1];
      if (batch.length < CITATION_PAGE || !tail || typeof tail.position !== 'number') break;
      last = tail.position;
    }
    if (rows.length === 0) { setStatus(status, 'This publication cites no other work.'); return; }

    // Which cited SCI publications exist in this catalog?
    var ids = [];
    rows.forEach(function (r) {
      var m = typeof r.target === 'string' ? SCI_TARGET_RE.exec(r.target) : null;
      if (m && ids.indexOf(m[1]) === -1) ids.push(m[1]);
    });
    var local = new Map();
    var lookupFailed = false;
    for (var i = 0; i < ids.length; i += CITATION_PAGE) {
      var chunk = ids.slice(i, i + CITATION_PAGE);
      try {
        var found = await catalogRows(cfg, 'publications', [['uuid', 'in.(' + chunk.join(',') + ')'], ['select', 'uuid,title']]);
        found.forEach(function (p) { if (typeof p.uuid === 'string') local.set(p.uuid, typeof p.title === 'string' ? p.title : p.uuid); });
      } catch (e) {
        lookupFailed = true;
      }
    }

    var list = $('refs');
    rows.forEach(function (r) {
      var li = el('li');
      li.appendChild(relationLabel(r.relation));
      var target = typeof r.target === 'string' ? r.target : '';
      var m = SCI_TARGET_RE.exec(target);
      if (m && local.has(m[1])) {
        li.appendChild(link(publicationHref(m[1]), local.get(m[1])));
        li.appendChild(document.createTextNode(' '));
        li.appendChild(el('code', 'target-raw', target));
      } else if (m) {
        li.appendChild(el('code', null, target));
        li.appendChild(el('span', 'unavailable', lookupFailed ? ' · availability unknown' : ' · not available on this instance'));
      } else {
        var href = doiHref(target);
        if (href) li.appendChild(link(href, target, null, true));
        else li.appendChild(el('code', null, target));
      }
      if (typeof r.reason === 'string' && r.reason) li.appendChild(el('p', 'reason', r.reason));
      list.appendChild(li);
    });
    setStatus(status, lookupFailed ? 'Could not check which cited SCI publications are on this instance.' : '', lookupFailed);
  }

  async function loadCitedBy(cfg, pub) {
    var status = $('citedby-status');
    var key = 'sci:' + pub.uuid;
    var countRows = await catalogRows(cfg, 'citation_counts', [['target_key', 'eq.' + key], ['select', 'citing_publications']]);
    var count = countRows.length ? Number(countRows[0].citing_publications) : 0;
    if (!isFinite(count) || count < 0) count = 0;
    $('citedby-count').textContent = '(' + count + ')';
    if (count === 0) { setStatus(status, 'No publications on this instance cite this one yet.'); return; }

    var rows = [];
    var cursor = null;
    var complete = false;
    for (var page = 0; page < MAX_CITATION_PAGES; page++) {
      var params = [['target_key', 'eq.' + key],
        ['select', 'source_uuid,position,relation,reason,publications(uuid,title)']];
      if (cursor) {
        params.push(['or', '(source_uuid.gt.' + cursor.source + ',and(source_uuid.eq.' + cursor.source + ',position.gt.' + cursor.position + '))']);
      }
      params.push(['order', 'source_uuid.asc,position.asc'], ['limit', String(CITATION_PAGE)]);
      var batch = await catalogRows(cfg, 'citations', params);
      rows = rows.concat(batch);
      var tail = batch[batch.length - 1];
      if (batch.length < CITATION_PAGE) { complete = true; break; }
      if (!tail || !UUID_RE.test(String(tail.source_uuid)) || typeof tail.position !== 'number') break;
      cursor = { source: tail.source_uuid, position: tail.position };
    }

    // Group citing rows by source publication, keeping catalog order.
    var groups = new Map();
    rows.forEach(function (r) {
      if (typeof r.source_uuid !== 'string' || !UUID_RE.test(r.source_uuid)) return;
      if (!groups.has(r.source_uuid)) groups.set(r.source_uuid, { title: null, refs: [] });
      var g = groups.get(r.source_uuid);
      if (isObject(r.publications) && typeof r.publications.title === 'string') g.title = r.publications.title;
      g.refs.push(r);
    });

    var list = $('citedby');
    groups.forEach(function (g, source) {
      var li = el('li');
      var labels = [];
      g.refs.forEach(function (r) {
        var label = typeof r.relation === 'string' && r.relation ? r.relation : 'cites';
        if (labels.indexOf(label) === -1) labels.push(label);
      });
      var rel = el('span', 'rel' + (labels.length === 1 && RELATIONS.indexOf(labels[0]) !== -1 ? ' ' + labels[0] : ''), labels.join(' · '));
      li.appendChild(rel);
      li.appendChild(link(publicationHref(source), g.title || source));
      g.refs.forEach(function (r) {
        if (typeof r.reason === 'string' && r.reason) li.appendChild(el('p', 'reason', r.reason));
      });
      list.appendChild(li);
    });
    setStatus(status, complete ? '' : 'Showing the first ' + rows.length + ' incoming citations.');
  }

  // ------------------------------------------------------------------ boot

  function boot() {
    var run = document.body.classList.contains('publication-page') ? initReader
      : document.body.classList.contains('home-page') ? initHome : null;
    if (!run) return;
    run().catch(function (err) {
      var status = $('pub-status') || $('feed-status');
      setStatus(status, errorText(err), true);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
