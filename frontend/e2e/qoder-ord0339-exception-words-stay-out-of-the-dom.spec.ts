/**
 * ORD-0339 (Q71) — an exception's own words may not enter the DOM through `data-*`.
 *
 * Q71 says a `data-*` attribute is not "on screen" but is "in the DOM", so an exception name or
 * sentence parked there is reachable by a screen reader, an export, or any later render.  Before
 * this order the sealed-demo alert held exactly that (`data-error-name` / `data-error-message`),
 * and D's ORD-0273 gate asserted the attribute was non-empty.  This spec replaces that surface with
 * the rule, in both directions:
 *
 * CLEAN ARM: the alert still speaks (same i18n sentence, same retry CTA -- nothing about
 *            ORD-0273's user-facing promise is traded away), and nothing on the element carries an
 *            exception's words or a shape outside the Q64 field list.
 * TEETH ARM: a dirty element is built in the page -- the pre-ORD-0339 attributes, verbatim, with a
 *            canary sentence -- and the *same* predicate must report it.  If it does not, this spec
 *            fails, so the clean arm cannot pass by being blind.
 *
 * Runs without a live backend: the one read this state needs is stubbed to the commander's own
 * failure shape (200 + HTML body), which is what makes the sealed-demo entry fail in the first place.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0339-exception-words-stay-out-of-the-dom.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

/** Held here, never imported from the product file, so the copy is asserted not echoed. */
const FROZEN_SENTENCE = "没读到封存演示的信息：服务器刚才答了，但答的内容我们用不了。这不是你填错了什么。";

/** Q64=1's allowed field names, spelled as the ruling spells them. */
const Q64_FIELDS = [
  "http_status",
  "content_type",
  "body_bytes",
  "response_received",
  "elapsed_ms",
  "request_id",
] as const;

/** What may sit on the alert element: its own identity attributes, and Q70's anchored field pair. */
const PLAIN_ATTRS = ["class", "data-testid", "role", "style"];
const ANCHOR_ATTRS = ["data-cnx-field", "data-cnx-value"];

/** The words an exception, not a measurement, produces.  Named from ORD-0273's own leak list. */
const EXCEPTION_SHAPES = [
  "text/html",
  "text/plain",
  "charset",
  "error",
  "exception",
  "unexpected",
  "TypeError",
  "SyntaxError",
  "CANARY",
];

/**
 * The predicate both arms share.  It returns the reasons an element is dirty, so the teeth arm can
 * show *which* rule fired rather than only "something was wrong".
 */
function inspectElement(attrs: Array<[string, string]>): string[] {
  const dirty: string[] = [];
  for (const [name, value] of attrs) {
    if (name.startsWith("data-error")) {
      dirty.push(`${name}: a data-error-* surface is the shape ORD-0339 withdrew`);
      continue;
    }
    if (PLAIN_ATTRS.includes(name) || ANCHOR_ATTRS.includes(name)) {
      if (name === "data-cnx-field" && value !== "" && !Q64_FIELDS.includes(value as (typeof Q64_FIELDS)[number])) {
        dirty.push(`data-cnx-field="${value}": not one of the six Q64 field names`);
      }
      continue;
    }
    dirty.push(`${name}: an attribute this element is not allowed to carry`);
    continue;
  }
  /* A measured value is a number, a token or a media type.  An exception is a sentence, and
     sentences carry spaces and MIME punctuation -- so a space-bearing value is reported whether or
     not it matches one of the named shapes above. */
  for (const [name, value] of attrs) {
    if (value === "" || PLAIN_ATTRS.includes(name)) continue;
    const lower = value.toLowerCase();
    for (const shape of EXCEPTION_SHAPES) {
      if (lower.includes(shape.toLowerCase())) {
        dirty.push(`${name}="${value}": carries the exception shape "${shape}"`);
      }
    }
    if (/\s/.test(value) && name !== "class") {
      dirty.push(`${name}="${value}": a sentence, not a measured value`);
    }
  }
  return dirty;
}

async function attrsOf(page: Page, selector: string): Promise<Array<[string, string]>> {
  return page.$eval(selector, (el) =>
    Array.from(el.attributes).map((a) => [a.name, a.value] as [string, string]),
  );
}

async function openWizardWithBrokenScene(page: Page) {
  /* The product component, mounted with the props the shell really passes -- see the fixture's
     header for why this is the narrower and truer surface for this order. */
  await page.goto("/", { waitUntil: "load" });
  await page.addScriptTag({ type: "module", url: "/e2e/fixtures/ord0339-sealed-alert-harness.tsx" });
  await page.waitForFunction(
    () => !!document.querySelector('[data-testid="trial-sealed-unavailable"]'),
    null,
    { timeout: 30_000 },
  );
}

test("ORD-0339 clean arm — the alert still speaks, and holds no exception words", async ({ page }) => {
  test.setTimeout(180_000);
  await openWizardWithBrokenScene(page);

  const alert = page.getByTestId("trial-sealed-unavailable");
  await expect(alert, node("clean", "the failure alert never rendered")).toBeVisible({ timeout: 20_000 });

  /* ORD-0273's promise, kept: the sentence is still the i18n value and the retry still exists. */
  const sentence = (await alert.locator("p").first().innerText()).replace(/\s+/g, " ").trim();
  expect(sentence, node("clean", `the sentence reads "${sentence}"`)).toBe(FROZEN_SENTENCE);
  await expect(
    page.getByTestId("trial-sealed-retry"),
    node("clean", "the retry CTA disappeared with the attributes"),
  ).toBeVisible({ timeout: 10_000 });

  /* The canary is what an exception's own words look like here.  It may not be anywhere in the
     document -- not in an attribute, not in a text node, not in a collapsed panel. */
  const canary = (await page.evaluate("window.__ord0339.canaryMessage")) as string;
  const html = await page.evaluate(() => document.body.innerHTML);
  expect(html.includes("data-error-"), node("clean", "a data-error-* attribute came back")).toBe(false);
  expect(html.includes(canary), node("clean", "the exception's sentence is in the document")).toBe(false);

  const attrs = await attrsOf(page, '[data-testid="trial-sealed-unavailable"]');
  const dirty = inspectElement(attrs);
  writeFileSync(
    evidencePath("ord0339-clean-reading.json"),
    JSON.stringify({ arm: "clean", sentence, attributes: attrs, dirty, canaryInHtml: html.includes(canary) }, null, 1),
  );
  expect(dirty, node("clean", `the alert element is dirty: ${dirty.join(" | ")}`)).toEqual([]);
});

test("ORD-0339 teeth arm — the pre-fix attributes must be reported by the same predicate", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await page.goto("about:blank");
  /* The withdrawn surface, rebuilt verbatim with a canary in the sentence position. */
  await page.setContent(`<!doctype html><div class="trial-city-warning"
    data-testid="trial-sealed-unavailable" role="alert"
    data-error-name="TypeError"
    data-error-message="CANARY Unexpected token &lt; in JSON at position 0 while parsing text/html"></div>`);

  const attrs = await attrsOf(page, '[data-testid="trial-sealed-unavailable"]');
  const dirty = inspectElement(attrs);
  writeFileSync(
    evidencePath("ord0339-teeth-reading.json"),
    JSON.stringify({ arm: "teeth", attributes: attrs, dirty, rulesFired: dirty.length }, null, 1),
  );

  expect(dirty.length, node("teeth", "the predicate saw nothing wrong with a known-dirty element")).toBeGreaterThan(0);
  for (const name of ["data-error-name", "data-error-message"]) {
    expect(
      dirty.some((d) => d.startsWith(name)),
      node("teeth", `the predicate did not report ${name}`),
    ).toBe(true);
  }
  expect(
    dirty.some((d) => d.includes("CANARY")),
    node("teeth", "the canary sentence in an attribute went unreported"),
  ).toBe(true);
  expect(
    dirty.some((d) => d.includes("a sentence, not a measured value")),
    node("teeth", "the space-bearing-value rule never fired"),
  ).toBe(true);
});
