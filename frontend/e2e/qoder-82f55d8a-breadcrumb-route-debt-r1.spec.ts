import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/* ORD-0567 second batch, resident ruler (source-only, starts nothing, opens no port).
 *
 * The shell paints the top-bar name as  t("nav." + (route in {…whitelist…} ? route : "hub")).  Any screen
 * outside that object literal therefore shows the hub's own name -- "Today" / 「今天」 -- while its card header
 * says something else. Four screens were read doing exactly that, in both arms, on the shipped generation:
 * import, footprints, trash, access. This branch pays that debt with four whitelist rows, and the names it
 * paints already exist in both dictionary arms -- nothing here invents copy.
 *
 * What stays guarded after the fix: any *new* mounted screen that lands outside the whitelist without being
 * judged, any ghost row inside it, and any revert of the four rows below -- arm 3 names them one by one.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(HERE, "..");
const read = (rel: string) => readFileSync(resolve(FRONTEND, rel), "utf8");

const ROUTES_FILE = "src/routes/v8Routes.ts";
const SHELL_FILE = "src/app/V8Experience.tsx";
const LOCALE_FILE = "src/i18n/LocaleContext.tsx";

/** Screens whose row must be judged: the two drawer tables the shell mounts. `tokenRoutes` is a URL that
 *  must not be drawn in the chrome at all, so it is excluded by name and checked in its own arm. */
const MOUNTED_TABLES = ["desktopRoutes", "mobileRoutes"] as const;

/** Read `export const <table> = [ … ] ` and return its `key:` values, in file order, deduped. The anchor is
 *  matched as a declaration, not a substring: a prefix match would let `desktopRoutesBroken` pass as
 *  `desktopRoutes`, which is how the first version of this ruler sailed through a renamed table. */
function tableKeys(src: string, table: string): string[] {
  const decl = new RegExp(`^export const ${table}\\s*=`, "m");
  const found = decl.exec(src);
  expect(found, `${table} is missing from ${ROUTES_FILE} -- the ruler would pass on an empty denominator`).not.toBeNull();
  const start = found!.index;
  const open = src.indexOf("[", start);
  expect(open, `${table} has no array literal`).toBeGreaterThan(start);
  let depth = 0;
  let end = -1;
  for (let i = open; i < src.length; i += 1) {
    if (src[i] === "[") depth += 1;
    else if (src[i] === "]") {
      depth -= 1;
      if (depth === 0) { end = i; break; }
    }
  }
  expect(end, `${table}'s array literal never closes`).toBeGreaterThan(open);
  const body = src.slice(open, end);
  const keys: string[] = [];
  for (const m of body.matchAll(/\bkey:\s*"([A-Za-z0-9_]+)"/g)) {
    if (!keys.includes(m[1])) keys.push(m[1]);
  }
  return keys;
}

/** Pull the object literal out of `route in { … }` in the shell, by shape rather than by line number. */
function whitelistRoutes(src: string): string[] {
  const at = src.search(/\broute\s+in\s*\{/);
  expect(at, "the shell no longer picks its top-bar name out of a `route in { … }` literal").toBeGreaterThanOrEqual(0);
  const open = src.indexOf("{", at);
  let depth = 0;
  let end = -1;
  for (let i = open; i < src.length; i += 1) {
    if (src[i] === "{") depth += 1;
    else if (src[i] === "}") {
      depth -= 1;
      if (depth === 0) { end = i; break; }
    }
  }
  expect(end, "the whitelist literal never closes").toBeGreaterThan(open);
  const keys: string[] = [];
  for (const m of src.slice(open, end).matchAll(/^\s*([A-Za-z0-9_]+):\s*1,?\s*$/gm)) keys.push(m[1]);
  expect(keys.length, "the whitelist literal parsed to zero rows").toBeGreaterThan(0);
  return keys;
}

/** The fallback the shell uses for anything outside the whitelist. */
function fallbackKey(src: string): string {
  const m = /\?\s*route\s*:\s*"([A-Za-z0-9_]+)"/.exec(src);
  expect(m, "the shell's breadcrumb fallback literal is unreadable").not.toBeNull();
  return m![1];
}

function dictionaryBlock(src: string, name: string): string {
  const start = src.search(new RegExp(`^const ${name}\\b`, "m"));
  expect(start, `dictionary ${name} is missing from ${LOCALE_FILE}`).toBeGreaterThanOrEqual(0);
  const next = src.slice(start + 1).search(/^const [A-Za-z0-9_]+\b/m);
  return next < 0 ? src.slice(start) : src.slice(start, start + 1 + next);
}

function navName(block: string, route: string): string | null {
  const m = new RegExp(`^\\s*"nav\\.${route}":\\s*"((?:[^"\\\\]|\\\\.)*)"`, "m").exec(block);
  return m ? m[1] : null;
}

/**
 * Paid-off debt, kept on purpose. Each row is a screen that was read painting the hub's own name in both
 * arms; the fix registered it in the whitelist. A row here must now be whitelisted AND must still carry its
 * name in both dictionary arms -- so reverting the fix, or renaming the label out from under the crumb, is
 * a red arm rather than a silent regression.
 */
const FORMER_DEBT: Array<{ route: string; reads: string; zh: string }> = [
  { route: "import", reads: "Import places", zh: "导入地点" },
  { route: "footprints", reads: "Footprints", zh: "足迹" },
  { route: "trash", reads: "Recycle bin", zh: "回收站" },
  { route: "access", reads: "Members and access", zh: "成员与权限" },
];

const routesSrc = read(ROUTES_FILE);
const shellSrc = read(SHELL_FILE);
const localeSrc = read(LOCALE_FILE);
const mounted = [...new Set(MOUNTED_TABLES.flatMap((t) => tableKeys(routesSrc, t)))];
const whitelisted = whitelistRoutes(shellSrc);
const fallback = fallbackKey(shellSrc);
const enBlock = dictionaryBlock(localeSrc, "en");
const zhBlock = dictionaryBlock(localeSrc, "zhCN");

test("arm 1 -- both sides parsed to a real denominator", () => {
  // A tripwire only: it says nothing about which row moved, so the named arms keep their diagnosis. The
  // first version coupled this to the debt count and swallowed arms 2 and 3's messages.
  expect(mounted.length).toBeGreaterThanOrEqual(13);
  expect(whitelisted.length).toBeGreaterThanOrEqual(13);
});

test("arm 2 -- every mounted screen is whitelisted; a new one must be judged, not defaulted", () => {
  const outside = mounted.filter((r) => !whitelisted.includes(r)).sort();
  expect(outside, `screens falling back to the hub's name: ${outside.join(" ")}`).toEqual([]);
});

test("arm 3 -- each paid-off row is still whitelisted and still a mounted screen", () => {
  for (const d of FORMER_DEBT) {
    expect(whitelisted, `${d.route} lost its whitelist row: the crumb would read "Today" again`).toContain(d.route);
    expect(mounted, `${d.route} is no longer a mounted screen: delete its row here`).toContain(d.route);
  }
});

test("arm 4 -- each paid-off row still has its own name in both arms", () => {
  for (const d of FORMER_DEBT) {
    expect(navName(enBlock, d.route), `${d.route} lost its English name`).toBe(d.reads);
    expect(navName(zhBlock, d.route), `${d.route} lost its Chinese name`).toBe(d.zh);
  }
});

test("arm 5 -- the whitelist carries no ghost row", () => {
  const ghosts = whitelisted.filter((r) => !mounted.includes(r));
  expect(ghosts, `routes whitelisted but not mounted: ${ghosts.join(" ")}`).toEqual([]);
});

test("arm 6 -- the fallback is the hub, which is why the debt reads as the hub's own name", () => {
  expect(fallback).toBe("hub");
  expect(navName(enBlock, fallback)).toBe("Today");
  expect(navName(zhBlock, fallback)).toBe("今天");
});

test("arm 7 -- tokenRoutes are not mounted screens and the shell really short-circuits them", () => {
  const tokens = tableKeys(routesSrc, "tokenRoutes");
  expect(tokens.length).toBeGreaterThanOrEqual(1);
  for (const t of tokens) {
    expect(mounted, `${t} is in both tables -- the debt list would be lying`).not.toContain(t);
    // The standalone render branch, not any mention of the key: the shell also tests
    // `deepLink.route === "shared"` while resolving an address, and that one does not keep the chrome off.
    expect(
      new RegExp(`\\n\\s*if \\(route === "${t}"\\) \\{`).test(shellSrc),
      `${t} has no standalone early branch in the shell, so it does paint a top-bar name`
    ).toBe(true);
  }
});
