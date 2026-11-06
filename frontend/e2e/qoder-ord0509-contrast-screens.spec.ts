import { test, expect } from "@playwright/test";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

import { scanContrast } from "./_qoderContrastScan";

/*
 * ORD-0509 -- the surfaces that grew this afternoon, read under BOTH colour schemes with the
 * project's own contrast ruler: one control at a time (its box in px, whether a tap at its centre
 * lands on it, what colours it carries), plus every contrast row the ruler returns.
 *
 * Receipts, not just assertions: a green suite says nothing about what was on the screen. Each pass
 * drops one JSON per (screen, scheme) under review/ord0509/run/, and the test still fails when the
 * ruler reports a violation, so the number and the verdict come from the same pass.
 *
 * A screen this stack cannot reach is written as NOT_AVAILABLE_BY_CMDR with the URL, the route tried
 * and what was actually in the DOM -- "the test did not throw" is never recorded as a reading.
 */

const HERE = path.dirname(fileURLToPath(import.meta.url));
const RUN_DIR = path.join(HERE, "..", "..", "review", "ord0509", "run");

type Page_ = import("@playwright/test").Page;

type Screen = {
  key: string;
  anchor: string;
  controls: string[];
  route: string;
  prepare?: (p: Page_) => Promise<string>;
};

const SCREENS: Screen[] = [
  {
    key: "collections",
    /* The shelf renders on the discover route (v8PanelRegistry: `when: (s) => s.route === "discover"`). */
    route: "/v8/discover",
    anchor: "collection-panel",
    controls: ["collection-shelf", "collection-item", "collection-tally", "collection-retry",
      "collection-empty", "collection-no-traveler"],
  },
  {
    key: "recycle-bin",
    route: "/v8/trash",
    anchor: "journey-trash-panel",
    controls: ["trash-list", "trash-count", "trash-bring-back", "trash-empty"],
  },
  {
    key: "poster-days",
    route: "/v8/trips",
    anchor: "trip-poster",
    controls: ["poster-open", "poster-days", "poster-day-1", "poster-day-2", "poster-download"],
    prepare: async (p) => {
      const open = p.locator('[data-testid="poster-open"]');
      if (await open.count()) {
        await open.first().click();
        await p.waitForTimeout(900);
        return "poster-open clicked";
      }
      return "no poster-open button on /v8/trips";
    },
  },
  {
    key: "share-receiving",
    route: "/v8/hub",
    anchor: "shared-language",
    controls: ["shared-language", "shared-day", "shared-map", "shared-no-token", "shared-plan-empty"],
    prepare: async (p) => {
      /* The token is cut through the product's own write door; the contract requires visibility and
       * created_by, and the token arrives on the response -- read, do not guess. */
      const journey = await p.evaluate(() => localStorage.getItem("citynexus.v8.journey_id"));
      if (!journey) return "no journey id in store (the seed project owns that key)";
      const created = await p.request.post(`/api/v1/journeys/${journey}/share-grants`, {
        data: { visibility: "LINK_READ", created_by: "ord0509-contrast-readings" },
      });
      if (!created.ok()) return `grant refused http ${created.status()}: ${(await created.text()).slice(0, 90)}`;
      const body = await created.json();
      const token = body.token ?? (body.grant && body.grant.token);
      if (!token) return "grant answered without a token field";
      await p.goto(`/v8/shared/${token}`, { waitUntil: "load" });
      await p.waitForTimeout(1500);
      return `shared token route (grant ${body.id ?? "?"})`;
    },
  },
  {
    key: "mobile-drawer",
    route: "/v8/hub",
    anchor: "v8-nav-drawer",
    controls: ["v8-mobile-menu", "v8-drawer-close", "v8-drawer-entry-collections",
      "v8-drawer-entry-trips", "v8-drawer-entry-trash"],
    prepare: async (p) => {
      const ham = p.locator('[data-testid="v8-mobile-menu"]');
      const seen = await ham.count();
      if (!seen) {
        const entries = await p.evaluate(() =>
          document.querySelectorAll("[data-testid^='v8-drawer-entry-']").length);
        return `NOT AVAILABLE: no v8-mobile-menu at ${p.viewportSize()?.width}x${p.viewportSize()?.height}`
          + ` (drawer entry nodes in DOM: ${entries})`;
      }
      await ham.first().click();
      await p.waitForTimeout(600);
      return "mobile menu tapped open";
    },
  },
];

async function readControls(page: Page_, ids: string[]) {
  return page.evaluate((wanted) => {
    const out: Record<string, unknown> = {};
    for (const id of wanted) {
      const el = document.querySelector<HTMLElement>(`[data-testid="${id}"]`);
      if (!el) {
        out[id] = { present: false };
        continue;
      }
      const r = el.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const at = document.elementFromPoint(cx, cy);
      const style = getComputedStyle(el);
      out[id] = {
        present: true,
        box: { w_px: Math.round(r.width * 10) / 10, h_px: Math.round(r.height * 10) / 10 },
        center: { x: Math.round(cx), y: Math.round(cy) },
        hit: at === el || (at !== null && el.contains(at)),
        hit_element: at ? (at.getAttribute("data-testid") ?? at.tagName.toLowerCase()) : null,
        in_viewport: r.top >= 0 && r.bottom <= innerHeight,
        font_px: parseFloat(style.fontSize),
        role_declared: el.getAttribute("data-contrast-role"),
      };
    }
    return out;
  }, ids);
}

fs.mkdirSync(RUN_DIR, { recursive: true });

function record(screen: Screen, scheme: "light" | "dark", page: Page_, reached: string,
  anchorSeen: number, controls: Record<string, unknown>, scan: Awaited<ReturnType<typeof scanContrast>>) {
  const receipt = {
    order: "ORD-0509",
    screen: screen.key,
    scheme,
    route_tried: screen.route,
    reached_by: reached,
    anchor: screen.anchor,
    anchor_present: !!anchorSeen,
    state: anchorSeen ? "RENDERED"
      : `NOT_AVAILABLE_BY_CMDR: ${screen.anchor} absent after ${reached}; url=${page.url()}`,
    controls,
    contrast: {
      elements_scanned: scan.count,
      violations: scan.rows,
      by_kind: scan.rows.reduce<Record<string, number>>((acc, r) => {
        acc[r.kind] = (acc[r.kind] ?? 0) + 1;
        return acc;
      }, {}),
      decorative_warnings: scan.warnings,
    },
    viewport: { w: page.viewportSize()?.width ?? null, h: page.viewportSize()?.height ?? null },
    run_id: process.env.CITYNEXUS_E2E_RUN_ID ?? null,
  };
  fs.writeFileSync(path.join(RUN_DIR, `${screen.key}.${scheme}.json`),
    JSON.stringify(receipt, null, 1), "utf-8");
  return receipt;
}

for (const screen of SCREENS) {
  for (const scheme of ["light", "dark"] as const) {
    test(`ORD-0509 ${screen.key} under ${scheme}`, async ({ page }, testInfo) => {
      if (screen.key === "mobile-drawer") await page.setViewportSize({ width: 390, height: 844 });
      await page.emulateMedia({ colorScheme: scheme, media: "screen" });
      await page.goto(screen.route, { waitUntil: "load", timeout: 90_000 });
      await page.waitForTimeout(900);
      const reached = screen.prepare ? await screen.prepare(page) : "route";
      const anchor = page.locator(`[data-testid="${screen.anchor}"]`);
      const anchorSeen = await anchor.count();
      if (anchorSeen) await anchor.first().waitFor({ timeout: 15_000 }).catch(() => null);

      const controls = await readControls(page, screen.controls);
      const scan = await scanContrast(page, { scheme });
      const receipt = record(screen, scheme, page, reached, anchorSeen, controls, scan);
      testInfo.annotations.push({
        type: "receipt",
        description: `${screen.key}.${scheme}: state=${receipt.state.slice(0, 24)} `
          + `kinds=${JSON.stringify(receipt.contrast.by_kind)}`,
      });
      expect(scan.rows, `${screen.key}/${scheme}: `
        + JSON.stringify(scan.rows.slice(0, 5))).toEqual([]);
    });
  }
}

test.describe("ORD-0509 the ruler charges for a hidden role", () => {
  test.use({ viewport: { width: 1280, height: 800 } });
  test(`roles declared vs stripped, on ${"poster-days"}`, async ({ page }) => {
    /* The mutant the order asks for. The poster panel is the one new surface that already declares
     * intent (its open button carries data-contrast-role="control"), so the same screen can be read
     * twice: with the role, the button is judged as a control; without it, the ruler must NOT quietly
     * drop it -- an undeclared control is a finding, so the row count goes UP, not down. */
    await page.emulateMedia({ colorScheme: "dark", media: "screen" });
    await page.goto("/v8/trips", { waitUntil: "load" });
    /* The shell paints after the bundle resolves; clicking at zero milliseconds is how the first
     * take of this arm found no roles on the page and reported an empty declaration list. */
    await page.waitForTimeout(900);
    const open = page.locator('[data-testid="poster-open"]');
    if (await open.count()) {
      await open.first().click();
      await page.waitForTimeout(900);
    }
    const declared = await page.evaluate(() =>
      Array.from(document.querySelectorAll("[data-contrast-role]")).map((el) =>
        [el.getAttribute("data-testid") ?? el.className, el.getAttribute("data-contrast-role")]));
    const withRoles = await scanContrast(page, { scheme: "dark" });
    await page.evaluate(() => {
      document.querySelectorAll("[data-contrast-role]").forEach((el) => el.removeAttribute("data-contrast-role"));
    });
    const without = await scanContrast(page, { scheme: "dark" });
    const unclassified = without.rows.filter((r) => r.kind === "unclassified-control");
    fs.writeFileSync(path.join(RUN_DIR, "mutant.role-hidden.json"), JSON.stringify({
      screen: "poster-days", scheme: "dark",
      roles_declared: declared,
      rows_with_roles: withRoles.rows.length,
      rows_without_roles: without.rows.length,
      unclassified_control_rows: unclassified.length,
      sample_unclassified: unclassified.slice(0, 4).map((r) => [r.selector, r.contrast, r.required]),
    }, null, 1), "utf-8");
    expect(declared.length, "no element on this screen declares a contrast role").toBeGreaterThan(0);
    expect(unclassified.length, "stripping every role left the ruler blind -- the tier is not guarding")
      .toBeGreaterThan(0);
    expect(without.rows.length, `rows with roles ${withRoles.rows.length} -> without ${without.rows.length}`)
      .toBeGreaterThan(withRoles.rows.length);
  });
});
