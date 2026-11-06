/**
 * ORD-0415 — where do the strings painted on the map glass come from?
 *
 * The rule module (now `e2e/_mapLabelRules.ts`; ORD-0415 took it out of `src/lib/` on this branch) owns
 * order asks whether anything on the product path actually consults them. Two arms, both read off the
 * running screen rather than off a grep:
 *
 *   legacy-map    — `/legacy` is the only route that mounts `LegacyApp`, and `JourneyMap` is the only
 *                   component in the repo with a `symbol` layer. This arm collects the `title` values
 *                   out of the payloads that screen received and runs them through the rule's own list,
 *                   so the answer is about the strings that WOULD be painted, not about my reading of
 *                   the source.
 *   v8-glass-corpus — reproduce the string set that the existing ORD-0412 gate feeds to
 *                   `mapLabelViolations` and report how much of it comes from the glass. The gate's
 *                   corpus is `canvas.innerText` plus the DOM markers' text; a canvas has no innerText,
 *                   so this arm measures how many characters of that corpus are actually canvas text.
 */
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";
import { MAP_LABEL_FORBIDDEN, mapLabelViolations } from "./_mapLabelRules";

const HOST = "tile.openstreetmap.org";
const out: Record<string, unknown> = {};

function write(name: string, body: unknown) {
  const p = evidencePath("qoder-ord0415", name + ".json");
  mkdirSync(dirname(p), { recursive: true });
  writeFileSync(p, JSON.stringify(body, null, 1), "utf8");
  console.log("O415 " + name + " " + JSON.stringify(body));
}

/** Every `activities[]` array reachable from a decoded payload, so the arm does not hardcode a route. */
function collectTitles(bodies: unknown[]): { title: string; from: string }[] {
  const found: { title: string; from: string }[] = [];
  const walk = (v: unknown, path: string) => {
    if (Array.isArray(v)) {
      v.forEach((item, i) => walk(item, path + "[" + i + "]"));
      return;
    }
    if (v && typeof v === "object") {
      const o = v as Record<string, unknown>;
      if (Array.isArray(o.activities)) {
        (o.activities as unknown[]).forEach((a, i) => {
          const t = (a as Record<string, unknown>)?.title;
          if (typeof t === "string") found.push({ title: t, from: path + ".activities[" + i + "]" });
        });
      }
      Object.keys(o).forEach((k) => walk(o[k], path + "." + k));
    }
  }
  bodies.forEach((b, i) => walk(b, "payload[" + i + "]"));
  return found;
}

test("ORD-0415 arm 1 /legacy the titles the one symbol layer would paint", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const bodies: unknown[] = [];
  const urls: string[] = [];
  page.on("response", async (r) => {
    const u = r.url();
    if (!u.includes("/api/v1/")) return;
    try {
      const j = await r.json();
      bodies.push(j);
      urls.push(u.replace(/^https?:\/\/[^/]+/, ""));
    } catch {
      /* non-JSON responses carry no activity list */
    }
  });
  await ensureDemoJourney(page);
  await page.goto("/legacy", { waitUntil: "networkidle", timeout: 90000 });
  await page.waitForTimeout(4000);
  const shell = await page.evaluate(() => ({
    path: location.pathname,
    legacyRoot: !!document.querySelector("main.shell"),
    v8Shell: !!document.querySelector(".v8-product-shell"),
    mapCard: !!document.querySelector("[data-testid='journey-map']"),
    cardText: (document.querySelector("[data-testid='journey-map']")?.innerText ?? "").slice(0, 120),
    canvasCount: document.querySelectorAll("[data-testid='journey-map'] canvas").length,
    canvasBox: (() => {
      const c = document.querySelector("[data-testid='journey-map'] canvas");
      const b = c?.getBoundingClientRect();
      return b ? { w: Math.round(b.width), h: Math.round(b.height) } : null;
    })(),
    attribution: (document.querySelector("[data-testid='maplibre-attribution']")?.innerText ?? "").trim(),
    mapCardEl: !!document.querySelector(".map-card"),
    fallbackTexts: Array.from(document.querySelectorAll(".map-fallback")).map(
      (e) => (e.textContent ?? "").trim().slice(0, 80)),
    canvasCountAnywhere: document.querySelectorAll("canvas").length,
    h1: (document.querySelector("h1")?.innerText ?? "").trim().slice(0, 60),
  }));
  const titles = collectTitles(bodies);
  const judged = titles.map((t) => ({ title: t.title, from: t.from, violations: mapLabelViolations(t.title) }));
  write("arm1_legacy", {
    endpoint_response_count: bodies.length,
    endpoint_urls_sample: urls.slice(0, 12),
    ...shell,
    title_count: titles.length,
    distinct_title_count: new Set(titles.map((t) => t.title)).size,
    titles_with_violations: judged.filter((j) => j.violations.length).length,
    forbidden_rule_names: MAP_LABEL_FORBIDDEN.map((f) => f.name),
    judged,
    not_measured: [
      "the strings actually rasterised on the glass: maplibre keeps no handle on the container and this"
      + " arm refuses to add a window hook to the product for measurement",
    ],
  });
  expect(shell.legacyRoot, "/legacy has to mount LegacyApp for this arm to mean anything").toBe(true);
  expect(titles.length, "the arm needs the activity list the screen received").toBeGreaterThan(0);
});

test("ORD-0415 arm 2 /v8 map what the ORD-0412 gate feeds to mapLabelViolations is DOM, not glass", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/hub", ["today-heading", "today-attention"]);
  await page.getByTestId("nav-map").click();
  await expect(page.getByTestId("v8-maplibre-panel").first()).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(2000);
  const read = await page.evaluate((host: string) => {
    const canvas = document.querySelector<HTMLElement>(".v8-maplibre-canvas");
    const markers = Array.from(document.querySelectorAll<HTMLElement>(".v8-map-panel .v9-map-marker"));
    const glassCorpus = [
      canvas?.innerText ?? "",
      ...markers.map((m) => (m.textContent ?? "").trim()),
      ...markers.map((m) => m.getAttribute("aria-label") ?? ""),
    ].join("\n");
    return {
      canvasTag: canvas ? canvas.tagName.toLowerCase() : "none",
      canvasInnerTextChars: (canvas?.innerText ?? "").length,
      canvasChildElementCount: canvas ? canvas.childElementCount : -1,
      markerCount: markers.length,
      markerTexts: markers.map((m) => (m.textContent ?? "").trim()),
      markerAriaCount: markers.filter((m) => (m.getAttribute("aria-label") ?? "").trim()).length,
      glassCorpusChars: glassCorpus.length,
      glassCorpusFromCanvasChars: (canvas?.innerText ?? "").length,
      corpusHasHost: glassCorpus.includes(host),
      realCanvasCount: document.querySelectorAll("canvas").length,
      realCanvasInnerTextChars: (() => {
        const c = document.querySelector(".v8-map-panel canvas");
        return c ? (c as HTMLElement).innerText.length : -1;
      })(),
      panelAriaLabels: Array.from(document.querySelectorAll(".v8-map-panel [aria-label]"))
        .map((e) => (e.getAttribute("aria-label") ?? "").trim()).filter(Boolean).slice(0, 12),
    };
  }, HOST);
  write("arm2_v8_corpus", read);
  expect(read.markerCount, "the v8 screen has to have its DOM markers for this to be about anything")
    .toBeGreaterThan(0);
});
