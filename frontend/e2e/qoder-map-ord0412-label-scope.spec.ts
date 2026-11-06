import { expect, test, type Page } from "@playwright/test";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";
import { MAP_LABEL_FORBIDDEN, MAP_LABEL_SCOPE, mapLabelViolations } from "./_mapLabelRules";
import { mapCopy } from "../src/i18n/mapPanelCopy";

/**
 * ORD-0412 — the canvas-label whitelist has no jurisdiction over the basemap attribution.
 *
 * `MAP_LABEL_FORBIDDEN`'s `source` class matches `openstreetmap`, `©` and `provider`, which is
 * exactly what the basemap licence obliges the screen to show.  Today the two never touch because
 * the list judges strings painted on the glass while the sentence is DOM text -- but that is a
 * fact about the DOM, not a stated rule, so the first arm reads the rule off `e2e/_mapLabelRules.ts`
 * itself and the on-screen arms prove the two surfaces really are disjoint:
 *
 *   scope-declared  — the module names the judged surface and the exempt one, and says why.
 *   surfaces-<loc>  — glass corpus clean under `mapLabelViolations`, the attribution readable in
 *                     the DOM, the attribution NOT inside the judged corpus, and the negative
 *                     control: fed to the same list the attribution DOES raise `source`, so the
 *                     exemption is load-bearing and a widened scope would demand deleting it.
 *   readable-320    — the compliance sentence survives the narrowest shape we support.
 */

const FILE = "e2e/qoder-map-ord0412-label-scope.spec.ts";
const HOST = "tile.openstreetmap.org";

async function enterMap(page: Page): Promise<void> {
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/hub", ["today-heading", "today-attention"]);
  await page.getByTestId("nav-map").click();
  await expect(page.getByTestId("v8-maplibre-panel").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(1_500);
}

const readSurfaces = (page: Page) =>
  page.evaluate((host: string) => {
    const canvas = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const markers = Array.from(document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker"));
    const attr = document.querySelector<HTMLElement>("[data-testid='v8-map-attribution']");
    const panel = document.querySelector<HTMLElement>(".v8-map-panel");
    const glassCorpus = [
      canvas?.innerText ?? "",
      ...markers.map((m) => (m.textContent ?? "").trim()),
      ...markers.map((m) => m.getAttribute("aria-label") ?? ""),
    ].join("\n");
    return {
      attributionText: (attr?.innerText ?? "").trim(),
      attributionTransform: attr ? getComputedStyle(attr).textTransform : "no-element",
      attributionVisible: !!attr && (attr.getBoundingClientRect().height > 0),
      glassCorpus,
      markerTexts: markers.map((m) => (m.textContent ?? "").trim()),
      panelText: panel?.innerText ?? "",
      bodyHasHost: (document.body.innerText ?? "").includes(host),
      glassHasHost: glassCorpus.includes(host),
    };
  }, HOST);

const push = (arm: string, v: Record<string, unknown>) => {
  /* one artefact per arm, written at the moment the arm reads: Playwright spreads a file's tests
     over workers, and a single afterAll aggregate only ever holds whichever worker wrote last --
     it silently dropped this file's `scope-declared` arm. */
  writeFileSync(
    evidencePath("ord0412-scope", `reading_${arm}.json`),
    JSON.stringify({ file: FILE, arm, scope: MAP_LABEL_SCOPE, ...v }, null, 1),
  );
  console.log(`${FILE} :: ${arm} — ${JSON.stringify(v)}`);
};

test("scope-declared — the rule itself says which surface it judges and why", async () => {
  const gate = readFileSync("e2e/qoder-map-ord0403-named-control-and-labels.spec.ts", "utf-8");
  const judged = MAP_LABEL_SCOPE.judged as readonly string[];
  const notJudged = MAP_LABEL_SCOPE.notJudged as readonly string[];
  push("scope-declared", {
    judged,
    notJudged,
    becauseOf: MAP_LABEL_SCOPE.becauseOf,
    forbiddenNames: MAP_LABEL_FORBIDDEN.map((r) => r.name),
    // one list, two consumers: the labels gate must import it rather than keep its own copy
    ord0403ImportsThisModule: gate.includes('from "./_mapLabelRules"'),
  });
  expect(judged, "only canvas-painted label text is in scope").toEqual(["canvas-label-strings"]);
  expect(notJudged, "the DOM attribution surface must be named as exempt").toContain("dom-attribution");
  expect(MAP_LABEL_SCOPE, "the carve-out must travel inside the rule's own data")
    .toHaveProperty("becauseOf");
  expect(MAP_LABEL_SCOPE.becauseOf, "the carve-out must name the obligation it protects")
    .toMatch(/ORD-A/);
  expect(MAP_LABEL_SCOPE.becauseOf).toMatch(/attribution/i);
  expect(MAP_LABEL_FORBIDDEN.map((r) => r.name), "the `source` class is the one that collides")
    .toContain("source");
  expect(gate.includes('from "./_mapLabelRules"'),
    "the labels gate must read this list, not keep its own copy").toBe(true);
  expect(existsSync("src/lib/mapLabels.ts"),
    "ORD-0415: this rule is not consulted by any product code, so it must not sit in the product tree").toBe(false);
});

for (const arm of [{ locale: "en", toggle: false }, { locale: "zh", toggle: true }] as const) {
  test(`surfaces-${arm.locale} — glass is judged, the attribution is not`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await enterMap(page);
    if (arm.toggle) {
      await page.getByTestId("topbar-language").click();
      await page.waitForTimeout(1_200);
    }
    const s = await readSurfaces(page);
    const authored = mapCopy(arm.locale, "attributionRaster");
    const glassViolations = mapLabelViolations(s.glassCorpus);
    const attributionViolations = mapLabelViolations(s.attributionText);
    const panelViolations = mapLabelViolations(s.panelText);
    const shot = evidencePath("ord0412-scope", `attribution_${arm.locale}.png`);
    await page.getByTestId("v8-map-attribution").screenshot({ path: shot });
    push(`surfaces-${arm.locale}`, {
      authored,
      attributionText: s.attributionText,
      attributionTransform: s.attributionTransform,
      attributionVisible: s.attributionVisible,
      bodyHasHost: s.bodyHasHost,
      markerCount: s.markerTexts.length,
      markerTextSample: s.markerTexts.slice(0, 12),
      glassViolations,
      // the negative control, recorded rather than hidden: the same list DOES flag the compliance
      // sentence, which is why the scope has to be written down instead of inferred from the DOM
      attributionViolationsIfFed: attributionViolations,
      panelViolationsIfFed: panelViolations,
      attributionInsideGlassCorpus: s.glassHasHost,
      shot,
    });
    expect(s.attributionVisible, "the attribution must be rendered, not just present").toBe(true);
    expect(s.attributionTransform, "no text-transform, so the painted text is the authored text")
      .toBe("none");
    expect(s.attributionText, `screen must show the authored sentence, got ${s.attributionText}`)
      .toBe(authored);
    expect(s.bodyHasHost, `${HOST} must be readable in body.innerText`).toBe(true);
    expect(glassViolations, `off-whitelist text painted on the glass: ${JSON.stringify(glassViolations)}`)
      .toEqual([]);
    expect(attributionViolations, "the `source` class must match the attribution -- that is the risk")
      .toContain("source");
    expect(s.glassHasHost, "the compliance sentence must not be inside the judged glass corpus")
      .toBe(false);
    /* Load-bearing, not decorative: if the scope were ever widened to the whole panel, this is the
       reading that turns red -- the panel's own text does raise `source`. */
    expect(panelViolations, "widening the scope to the panel would flag the required sentence")
      .toContain("source");
  });
}

test("readable-320 — the obligation still reads at the narrowest shape", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await enterMap(page);
  const s = await readSurfaces(page);
  const clip = await page.evaluate(() => {
    const el = document.querySelector<HTMLElement>("[data-testid='v8-map-attribution']");
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {
      w: Math.round(r.width), h: Math.round(r.height),
      insideViewport: r.left >= 0 && r.top >= 0 && r.right <= window.innerWidth && r.bottom <= window.innerHeight,
      scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth,
    };
  });
  const shot = evidencePath("ord0412-scope", "attribution_320.png");
  await page.getByTestId("v8-map-attribution").screenshot({ path: shot });
  push("readable-320", { attributionText: s.attributionText, glassViolations: mapLabelViolations(s.glassCorpus), ...clip, shot });
  expect(s.attributionText, `at 320 the sentence must still read, got ${s.attributionText}`).toContain(HOST);
  expect(clip!.insideViewport, "the attribution must not be pushed out of the viewport").toBe(true);
  expect(clip!.scrollW, "and must not create horizontal page scroll").toBeLessThanOrEqual(clip!.innerW + 1);
});

test.afterAll(() => {
  /* deliberately no aggregate file: each arm already wrote its own reading, and a combined one
     would only ever carry the rows of whichever worker finished last. */
});
