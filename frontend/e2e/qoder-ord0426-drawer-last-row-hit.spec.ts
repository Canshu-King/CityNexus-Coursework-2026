/**
 * ORD-0426 -- the drawer's last row has to be reachable, so this is the durable gate for the
 * reservation that the readouts reported as already shipped. It is deliberately not a box-height test:
 * the row above the footer is `right=bottom` against it, so a height check passes while the row is
 * still under the bar. What has to hold is that after scrolling the list to its end, a hit-test at the
 * last row's own centre comes back inside that row.
 */
import { expect, test, type Page } from "@playwright/test";

async function openDrawer(page: Page) {
  await page.goto("/", { waitUntil: "networkidle", timeout: 90000 });
  for (const cand of [page.getByText("使用封存的演示行程", { exact: false }).first(),
                      page.getByText(/sealed demo/i).first()]) {
    if (await cand.count()) {
      await cand.click();
      await page.waitForTimeout(3500);
      break;
    }
  }
  const ham = page.locator('[aria-controls="v8-nav-drawer"]');
  expect(await ham.count(), "the hamburger has to exist on a phone shape").toBeGreaterThan(0);
  await ham.first().click();
  await page.waitForTimeout(1200);
}

const LAST_ROW = () => {
  const doc = document;
  const drawer = doc.querySelector("#v8-nav-drawer");
  const nav = doc.querySelector(".v8-mobile-nav");
  const scroller = (drawer && (drawer.querySelector(".cn-onboarding__scroll")
    || drawer.querySelector("[class*=scroll]"))) || drawer;
  const rows = Array.from((drawer || doc).querySelectorAll("#v8-nav-drawer button"))
    .filter((b) => (b.textContent || "").trim());
  const last = rows[rows.length - 1] as HTMLElement | undefined;
  if (!last || !nav || !scroller) return null;
  const lb = last.getBoundingClientRect(), nb = nav.getBoundingClientRect(), sb = scroller.getBoundingClientRect();
  const mid = { x: Math.round(lb.x + lb.width / 2), y: Math.round(lb.y + lb.height / 2) };
  const hit = doc.elementFromPoint(mid.x, mid.y);
  const cs = doc.defaultView ? doc.defaultView.getComputedStyle(scroller) : null;
  const se = scroller as HTMLElement;
  return {
    rows: rows.length,
    last_row_text: (last.textContent || "").trim().slice(0, 18),
    last_row_bottom: Math.round(lb.bottom),
    hit_line: Math.round(nb.top),
    nav_height: Math.round(nb.height),
    scroller_client_width: Math.round(sb.width),
    client_width: doc.documentElement.clientWidth,
    scroller_padding_bottom: cs ? cs.paddingBottom : null,
    scrolled: Math.round(se.scrollTop),
    at_end: Math.round(se.scrollHeight - se.scrollTop - se.clientHeight),
    hit_inside: !!(hit && (hit === last || last.contains(hit))),
    hit_tag: hit ? hit.tagName.toLowerCase() : "none",
  };
};

test("ORD-0426 zh 390x844 the drawer's last row is hit-testable once the list is at its end", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openDrawer(page);
  await page.evaluate(() => {
    const drawer = document.querySelector("#v8-nav-drawer");
    const scroller = (drawer && (drawer.querySelector(".cn-onboarding__scroll")
      || drawer.querySelector("[class*=scroll]"))) || drawer;
    if (scroller) (scroller as HTMLElement).scrollTop = (scroller as HTMLElement).scrollHeight;
  });
  await page.waitForTimeout(600);
  const g = await page.evaluate(LAST_ROW);
  console.log("O426 gate zh 390x844 " + JSON.stringify(g));
  expect(g, "the drawer, its last row and the bar all have to be present").not.toBeNull();
  const geo = g as NonNullable<typeof g>;
  expect(geo.at_end, "the list must actually be scrolled to its end for this to mean anything").toBeLessThanOrEqual(2);
  expect(geo.client_width, "the scroller has to fill the viewport width so a centre hit cannot leave it").toBe(390);
  expect(geo.hit_inside, `the last row "${geo.last_row_text}" centre hit returned <${geo.hit_tag}>`).toBe(true);
  expect(geo.last_row_bottom, "the row must end above the fixed bar").toBeLessThanOrEqual(geo.hit_line);
});
