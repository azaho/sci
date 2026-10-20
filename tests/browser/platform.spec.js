import {test, expect} from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const demo = JSON.parse(fs.readFileSync(process.env.SCI_DEMO_INDEX || ".local/demo/index.json", "utf8"));
const shotDirectory = process.env.SCI_SCREENSHOT_DIR || "test-results/screenshots";
fs.mkdirSync(shotDirectory, {recursive: true});

test("feed, search, subject filters, and direct reader refresh", async ({page}) => {
  const requests = [];
  page.on("request", request => requests.push(request.url()));
  await page.goto("./");
  await expect(page.locator(".pub")).toHaveCount(3);
  await expect(page.locator(".status")).toHaveText("Public catalog");
  await expect(page.locator("img")).toHaveCount(0);
  await page.screenshot({path: path.join(shotDirectory, "feed-desktop.png"), fullPage: true});
  await page.getByRole("searchbox").fill("median");
  await page.getByRole("button", {name: "Search", exact: true}).click();
  await expect(page.locator(".pub")).toHaveCount(1);
  await page.getByRole("searchbox").fill("");
  await page.selectOption("#category", "cs.LG");
  await expect(page.locator(".pub")).toHaveCount(2);
  await page.goto(`./publication/?id=${demo[0].uuid}`);
  await expect(page.locator("#title")).toHaveText(demo[0].title);
  await page.reload();
  await expect(page.locator("#readme h2").first()).toBeVisible();
  await expect(page.locator("#headline img")).toHaveCount(1);
  await expect(page.locator("#headline")).toBeVisible();
  await expect(page.locator("#readme img")).toHaveCount(0);
  await expect(page.locator("#incoming .citation")).toHaveCount(2);
  await expect(page.locator("#file-list")).toContainText("data/");
  await expect(page.locator("#downloads")).toContainText("Supporting files");
  expect(requests.some(url => /\/manifest\.json(?:\.sig)?(?:\?|$)|\/sci\.json(?:\?|$)|\/files\.zip(?:\?|$)/.test(url))).toBe(false);
  await page.screenshot({path: path.join(shotDirectory, "publication-desktop.png"), fullPage: true});
});

test("safe rendering, missing dependencies, and absent figures", async ({page}) => {
  const unexpected = [];
  page.on("request", request => {
    if (request.url().includes("image.invalid")) unexpected.push(request.url());
  });
  await page.goto(`./publication/?id=${demo[1].uuid}`);
  await expect(page.locator("#title")).toHaveText(demo[1].title);
  await expect(page.locator("#headline")).toBeHidden();
  await expect(page.locator("#headline img")).not.toHaveAttribute("src");
  const findings = await page.evaluate(async () => {
    const {renderREADME} = await import("../app.js");
    const node = document.getElementById("readme");
    const source = [
      "# Heading", "", "- one", "- two", "",
      "| A | B |", "| - | - |", "| 1 | 2 |", "",
      "[external](https://example.org) [relative](files/secret.txt)",
      "![bad](https://image.invalid/inline.png)", "![ref][image]",
      "[image]: https://image.invalid/ref.png", "",
      '<img src="https://image.invalid/html.png" onerror="window.PWNED=1">',
      "[unsafe](javascript:window.PWNED=1)", "![data](data:image/svg+xml,<svg></svg>)",
    ].join("\n");
    renderREADME(node, source);
    const result = {
      images: node.querySelectorAll("img").length,
      dangerous: node.querySelectorAll('[href^="javascript:"],[href^="files/"]').length,
      headings: node.querySelectorAll("h1").length, tables: node.querySelectorAll("table").length,
      external: node.querySelectorAll('a[href="https://example.org"]').length,
      pwned: window.PWNED,
    };
    const saved = window.DOMPurify;
    window.DOMPurify = undefined;
    renderREADME(node, source);
    result.fallback = node.textContent === source && node.children.length === 0;
    window.DOMPurify = saved;
    return result;
  });
  expect(findings).toMatchObject({images: 0, dangerous: 0, headings: 1, tables: 1, external: 1, fallback: true});
  expect(findings.pwned).toBeUndefined();
  expect(unexpected).toEqual([]);
});

test("literal metadata, valid errors, and a narrow screen", async ({page}) => {
  await page.goto("./publication/?id=invalid");
  await expect(page.getByRole("alert")).toContainText("UUIDv4");
  await page.goto("./publication/?id=00000000-0000-4000-8000-000000000000");
  await expect(page.getByRole("alert")).toContainText("not available");
  await page.setViewportSize({width: 390, height: 844});
  await page.goto("./");
  await expect(page.locator(".pub")).toHaveCount(3);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: path.join(shotDirectory, "feed-mobile.png"), fullPage: true});
  await page.goto(`./publication/?id=${demo[0].uuid}`);
  await expect(page.locator("#readme h2").first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: path.join(shotDirectory, "publication-mobile.png"), fullPage: true});
});

test("content security policy blocks inline execution", async ({page}) => {
  await page.goto("./");
  await expect(page.locator(".pub")).toHaveCount(3);
  const result = await page.evaluate(() => {
    const script = document.createElement("script");
    script.textContent = "window.INLINE_EXECUTED = true";
    document.body.append(script);
    return window.INLINE_EXECUTED === true;
  });
  expect(result).toBe(false);
});

test("publisher metadata and file names remain literal text", async ({page}) => {
  const hostile = '<img src="https://image.invalid/metadata.png" onerror="window.PWNED=1">';
  const imageRequests = [];
  page.on("request", request => {
    if (request.url().includes("image.invalid")) imageRequests.push(request.url());
  });
  await page.route("**/catalog/publications?**", async route => {
    const response = await route.fetch();
    const rows = await response.json();
    for (const record of rows) {
      record.title = hostile;
      if (record.objects) {
        record.abstract = hostile;
        record.headline_figure_caption = hostile;
        record.keywords = [hostile];
        record.files_tree = {paths: [hostile], truncated: true};
      }
    }
    await route.fulfill({response, json: rows});
  });
  await page.route("**/catalog/citations?**", async route => {
    const response = await route.fetch();
    const rows = await response.json();
    for (const record of rows) record.reason = hostile;
    await route.fulfill({response, json: rows});
  });
  await page.goto(`./publication/?id=${demo[0].uuid}`);
  for (const id of ["title", "abstract", "caption", "keywords", "file-list"]) {
    await expect(page.locator(`#${id}`)).toHaveText(hostile);
    await expect(page.locator(`#${id} img`)).toHaveCount(0);
  }
  await expect(page.locator("#file-list-note")).toHaveText("More entries omitted");
  await expect(page.locator("#incoming .citation p").first()).toHaveText(hostile);
  expect(imageRequests).toEqual([]);
  expect(await page.evaluate(() => window.PWNED)).toBeUndefined();
});
