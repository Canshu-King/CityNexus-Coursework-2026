/**
 * ORD-0480 -- the drawer has three ways out, and each one has to be a real way out.
 *
 * Why this is a gate and not a note: ORD-0473 started as "the menu is dead" and turned out to be a
 * witness problem -- the drawer opened, but nothing about it was addressable, so a reading that
 * counts painted `data-testid`s reported 47 -> 47 and the door looked dead to everyone. The
 * identity is there now (`v8-nav-drawer`, `v8-drawer-close`, one `v8-drawer-entry-*` per route),
 * so what still has no defence is the three closing paths themselves. Nothing in the product
 * asserts that pressing 关闭, tapping the backdrop, or hitting Escape removes the drawer; if any
 * one of them stops working, a phone traveller is left inside an overlay with no way back.
 *
 * Each arm is its own test, and each arm carries a positive control: it fails if the drawer did
 * not open, so an arm cannot go green by the drawer never having been there. Breaking any single
 * path reddens exactly that arm -- that is the counterexample the order asked for, and it is
 * demonstrated in the delivery block, not asserted here.
 *
 * Assertions read what is on screen: the drawer node count, the backdrop node count, the painted
 * hamburger's own `aria-expanded`, and a hit-test at the hamburger's centre -- which must come
 * back to the hamburger again once the overlay is gone, because that is what "I can use the page
 * again" means on a touch screen.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-bac10f40-ord0480-drawer-close-arms.spec.ts";
const PHONE = { width: 390, height: 844 };
const DRAWER = "#v8-nav-drawer";
const BACKDROP = ".v8-drawer-backdrop";
const HAM = '[aria-controls="v8-nav-drawer"]';

const rows: Record<string, unknown>[] = [];
const push = (arm: string, v: Record<string, unknown>) => {
  rows.push({ arm, ...v });
  console.log(`${FILE} :: ${arm} — ${JSON.stringify(v)}`);
};

/** Painted state of the three things an arm judges, plus the hamburger's own geometry. */
const SEL = { DRAWER, BACKDROP, HAM };

/* page.evaluate serialises the function alone, so anything it needs has to arrive as an
   argument -- module constants are not in the page. */
const witness = (sel: typeof SEL) => {
  const doc = document;
  const drawer = doc.querySelector(sel.DRAWER);
  const ham = doc.querySelector(sel.HAM) as HTMLElement | null;
  const hb = ham ? ham.getBoundingClientRect() : null;
  const centre = hb ? { x: Math.round(hb.left + hb.width / 2), y: Math.round(hb.top + hb.height / 2) } : null;
  const at = centre ? doc.elementFromPoint(centre.x, centre.y) : null;
  const cs = ham ? doc.defaultView!.getComputedStyle(ham) : null;
  return {
    drawer_nodes: drawer ? 1 : 0,
    backdrop_nodes: doc.querySelectorAll(sel.BACKDROP).length,
    entry_nodes: doc.querySelectorAll("[data-testid^='v8-drawer-entry-']").length,
    close_button_present: !!doc.querySelector("[data-testid='v8-drawer-close']"),
    ham_text: ham ? (ham.textContent || "").trim().slice(0, 8) : null,
    ham_box: hb ? { x: Math.round(hb.left), y: Math.round(hb.top), w: Math.round(hb.width), h: Math.round(hb.height) } : null,
    ham_centre: centre,
    ham_expanded: ham ? ham.getAttribute("aria-expanded") : null,
    ham_visible: !!(cs && cs.display !== "none" && cs.visibility !== "hidden"),
    hit_at_ham_centre: at ? at.tagName.toLowerCase() + "." + String(at.className || "").slice(0, 20) : "none",
    hit_is_ham: !!(at && ham && (at === ham || ham.contains(at))),
  };
};

/** The suite's default browser locale is English, and the words this order names are Chinese:
 *  the arms have to press the control whose painted text is 关闭, not any close-looking button. */
async function openDrawer(page: Page) {
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "zh-CN"));
  await page.goto("/", { waitUntil: "networkidle", timeout: 90000 });
  for (const cand of [
    page.getByText("使用封存的演示行程", { exact: false }).first(),
    page.getByText(/sealed demo/i).first(),
  ]) {
    if (await cand.count()) {
      await cand.click();
      await page.waitForTimeout(3500);
      break;
    }
  }
  const ham = page.locator(HAM).first();
  expect(await ham.count(), "the phone shape has to show a 菜单 control").toBeGreaterThan(0);
  await ham.click();
  await page.waitForTimeout(1200);
}

/** Positive control: the drawer is up, its own entries are addressable, and the page behind it is not. */
async function expectOpen(page: Page, arm: string) {
  const open = await page.evaluate(witness, SEL);
  expect(open.drawer_nodes, `${arm}: the drawer has to be open before a way out can be judged`).toBe(1);
  expect(open.backdrop_nodes, `${arm}: the backdrop has to be mounted while the drawer is open`).toBe(1);
  expect(open.entry_nodes, `${arm}: the drawer has to carry its witnessable entries`).toBeGreaterThan(0);
  expect(open.ham_expanded, `${arm}: the hamburger has to report itself expanded`).toBe("true");
  expect(open.ham_text, `${arm}: the hamburger has to be the painted 菜单 the order names`).toBe("菜单");
  expect(open.hit_is_ham, `${arm}: while the drawer is open the hamburger is covered, so a press there must NOT reach it`).toBe(false);
  return open;
}

/** What every way out has to leave behind. */
async function expectClosed(page: Page, arm: string) {
  const after = await page.evaluate(witness, SEL);
  expect(after.drawer_nodes, `${arm}: ${DRAWER} is still mounted -- that path did not close the drawer`).toBe(0);
  expect(after.backdrop_nodes, `${arm}: ${BACKDROP} is still mounted -- the overlay is still eating taps`).toBe(0);
  expect(after.ham_expanded, `${arm}: the hamburger still reports aria-expanded=true`).toBe("false");
  expect(after.hit_is_ham, `${arm}: the page behind the drawer is not reachable again (centre hit = ${after.hit_at_ham_centre})`).toBe(true);
  return after;
}

test("ORD-0480 zh 390x844 arm=close_button 关闭 really takes the drawer away", async ({ page }) => {
  await page.setViewportSize(PHONE);
  await openDrawer(page);
  const open = await expectOpen(page, "close_button");
  const target = page.locator("[data-testid='v8-drawer-close']").first();
  expect((await target.innerText()).trim(), "the close control has to say 关闭 on screen").toBe("关闭");
  const box = await target.boundingBox();
  expect(box, "the 关闭 control has to have a box to press").not.toBeNull();
  await target.click();
  await page.waitForTimeout(1000);
  const after = await expectClosed(page, "close_button");
  push("close_button", {
    locale: "zh-CN", viewport: PHONE, ham_box: open.ham_box,
    close_box: { x: Math.round(box!.x), y: Math.round(box!.y), w: Math.round(box!.width), h: Math.round(box!.height) },
    entries_before: open.entry_nodes, after,
  });
});

test("ORD-0480 zh 390x844 arm=backdrop a press outside the panel takes it away", async ({ page }) => {
  await page.setViewportSize(PHONE);
  await openDrawer(page);
  const open = await expectOpen(page, "backdrop");
  /* The panel is 300px of the 390px width, so press in the strip to its right: that is the
     backdrop, and it is the only place outside the panel that the overlay owns. */
  const point = { x: 350, y: 400 };
  const who = await page.evaluate((p) => {
    const el = document.elementFromPoint(p.x, p.y);
    return el ? el.tagName.toLowerCase() + "." + String(el.className || "").slice(0, 22) : "none";
  }, point);
  expect(who, "the press point has to be the backdrop itself").toContain("v8-drawer-backdrop");
  await page.mouse.click(point.x, point.y);
  await page.waitForTimeout(1000);
  const after = await expectClosed(page, "backdrop");
  push("backdrop", { locale: "zh-CN", viewport: PHONE, ham_box: open.ham_box, press_point: point, hit_before: who, entries_before: open.entry_nodes, after });
});

test("ORD-0480 zh 390x844 arm=escape Escape takes it away", async ({ page }) => {
  await page.setViewportSize(PHONE);
  await openDrawer(page);
  const open = await expectOpen(page, "escape");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(1000);
  const after = await expectClosed(page, "escape");
  push("escape", { locale: "zh-CN", viewport: PHONE, ham_box: open.ham_box, key: "Escape", entries_before: open.entry_nodes, after });
});

test("ORD-0480 zh 390x844 the three arms write their readings", async ({ page }) => {
  await page.setViewportSize(PHONE);
  await page.addInitScript(() => localStorage.setItem("citynexus.locale", "zh-CN"));
  await page.goto("/", { waitUntil: "networkidle", timeout: 90000 });
  const idle = await page.evaluate(witness, SEL);
  push("closed_baseline", { locale: "zh-CN", viewport: PHONE, ...idle });
  writeFileSync(
    /* The run id belongs in the file name. Two earlier copies of this evidence were overwritten
       by a mutant run that shared the name, which turned "the clean readings" into a reading of a
       broken tree -- the same trap the block records, fixed at the place that writes. */
    evidencePath(`qoder-bac10f40-ord0480-drawer-close-arms--${process.env.CITYNEXUS_E2E_RUN_ID || "dev"}.json`),
    JSON.stringify({ file: FILE, viewport: PHONE, rows }, null, 1),
    "utf8",
  );
  /* A reading nobody can re-run is a rumour: this file has to exist for the block to cite it. */
  expect(rows.length).toBeGreaterThanOrEqual(4);
});
