/**
 * ORD-0406 -- the fixed bottom bar eats a 76 px band at the foot of the first screen, and Today's
 * "需要关注" row was sitting inside it. Four shapes x two language arms, read before any scrolling.
 *
 * Two geometries are recorded per cell, on purpose:
 *   box   = getBoundingClientRect() of the band element;
 *   range = Range.getClientRects() over its text nodes, i.e. the actual painted lines.
 * A wrapped label has one box but two client rects, and the second one can sit under the bar while the
 * box still looks clear, so the box alone can print a green that a traveller cannot reach. The verdict
 * uses the range; the box is kept so the difference is visible in the evidence, not argued about.
 *
 * Every cell writes its own evidence file, because two cells sharing one file cannot be compared:
 * the "before" phase and the "after" phase each get one file per shape per arm.
 */
import { expect, test, type Browser, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PHASE = process.env.Q406_PHASE || "after";
const OUT_DIR = resolve(HERE, "..", "review", "evidence", "q406", PHASE);
const SHAPES = [[390, 844], [360, 800], [320, 653], [414, 896]];
const SEL = {
  hero: ".v8-trip-hero",
  strip: '[data-testid="today-strip"]',
  band: '[data-testid="today-attention-band"]',
  map: ".v8-map-panel",
  nav: ".v8-mobile-nav",
};

async function openToday(page: Page) {
  await page.goto("/", { waitUntil: "networkidle", timeout: 90000 });
  for (const cand of [page.getByText("使用封存的演示行程", { exact: false }).first(),
                      page.getByText(/sealed demo/i).first()]) {
    if (await cand.count()) {
      await cand.click();
      await page.waitForTimeout(3500);
      break;
    }
  }
  await page.waitForTimeout(900);
}

function read(page: Page, sels: Record<string, string>) {
  return page.evaluate((s: Record<string, string>) => {
    const box = (sel: string) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
    };
    const band = document.querySelector(s.band) as HTMLElement | null;
    let rects: { x: number; y: number; w: number; h: number }[] = [];
    let centre: { x: number; y: number } | null = null;
    let inside = false;
    if (band) {
      const range = document.createRange();
      range.selectNodeContents(band);
      rects = Array.from(range.getClientRects())
        .filter((r) => r.width > 0 && r.height > 0)
        .map((r) => ({ x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }));
      const b = band.getBoundingClientRect();
      centre = { x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2) };
      const hit = centre ? document.elementFromPoint(centre.x, centre.y) : null;
      inside = !!(hit && (hit === band || band.contains(hit)));
    }
    const nav = document.querySelector(s.nav);
    const shell = document.querySelector(".v8-product-shell");
    return {
      hero: box(s.hero), strip: box(s.strip), band: box(s.band), map: box(s.map), nav: box(s.nav),
      bandRangeRects: rects, bandCentre: centre, hitTestInside: inside,
      hitLine: nav ? Math.round(nav.getBoundingClientRect().top) : null,
      navHeight: nav ? Math.round(nav.getBoundingClientRect().height) : null,
      shellPaddingBottom: shell ? getComputedStyle(shell).paddingBottom : null,
      scrollY: (document.scrollingElement || (document as any).documentElement).scrollTop,
      innerHeight: window.innerHeight, innerWidth: window.innerWidth,
      bandText: band ? (band.textContent || "").trim().slice(0, 60) : null,
    };
  }, sels);
}

function verdict(g: any) {
  const clearsBox = !!(g.band && g.hitLine != null && g.band.y + g.band.h <= g.hitLine);
  const last = g.bandRangeRects && g.bandRangeRects.length
    ? Math.max(...g.bandRangeRects.map((r: any) => r.y + r.h)) : null;
  const clearsRange = last != null && g.hitLine != null ? last <= g.hitLine : false;
  return { clearsBox, clearsRange, rangeBottom: last, hit: clearsRange && !!g.hitTestInside };
}

function save(name: string, payload: unknown) {
  mkdirSync(OUT_DIR, { recursive: true });
  writeFileSync(resolve(OUT_DIR, name), JSON.stringify(payload, null, 1), "utf8");
}

for (const arm of ["zh", "en"]) {
  for (const [w, h] of SHAPES) {
    test(`ORD-0406 ${arm} ${w}x${h} the attention row is above the bar before scrolling`, async ({ browser }) => {
      const ctx = await browser.newContext({ viewport: { width: w, height: h }, hasTouch: true, locale: arm });
      const page = await ctx.newPage();
      try {
        await openToday(page);
        const g = await read(page, SEL);
        const v = verdict(g);
        const file = `${arm}_${w}x${h}.json`;
        save(file, { phase: PHASE, arm, shape: [w, h], ...g, ...v });
        console.log(`O406 ${PHASE} ${arm} ${w}x${h} nav_height=${g.navHeight} hit_line=${g.hitLine} `
          + `band_box=[${g.band.y},${g.band.y + g.band.h}] band_range_rects=${g.bandRangeRects.length} `
          + `band_range_bottom=${v.rangeBottom} clears_box=${v.clearsBox} clears_range=${v.clearsRange} `
          + `hit_test_inside=${g.hitTestInside} band_hit=${v.hit} scroll_y=${g.scrollY} `
          + `hero_y=${g.hero.y} map_y=${g.map ? g.map.y : "none"} evidence=${PHASE}/${file}`);
        expect(g.band, "the band must be on screen to be measured").not.toBeNull();
        expect(g.nav, "the fixed bar must exist on a phone shape").not.toBeNull();
        expect(g.bandRangeRects.length, "the band's text has to paint at least one client rect").toBeGreaterThan(0);
        expect(g.scrollY, "this reading is taken before any scrolling").toBe(0);
        expect(v.clearsRange, `the band's last painted line ends at ${v.rangeBottom}, the bar starts at ${g.hitLine}`).toBe(true);
        expect(g.hitTestInside, "a hit-test at the band's centre must land back inside it").toBe(true);
      } finally {
        await ctx.close();
      }
    });
  }
}

test("ORD-0406 zh 1280x800 desktop keeps all three surfaces", async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: "zh" });
  const page = await ctx.newPage();
  try {
    await openToday(page);
    const g = await read(page, SEL);
    const v = verdict(g);
    save("zh_1280x800_desktop.json", { phase: PHASE, arm: "zh", shape: [1280, 800], ...g, ...v });
    console.log(`O406 ${PHASE} zh desktop 1280x800 band_box=[${g.band.y},${g.band.y + g.band.h}] `
      + `hero_y=${g.hero.y} map_y=${g.map.y} nav_present=${!!g.nav} `
      + `evidence=${PHASE}/zh_1280x800_desktop.json`);
    expect(g.band && g.hero && g.map, "all three surfaces still render on a desktop shape").toBeTruthy();
  } finally {
    await ctx.close();
  }
});

test("ORD-0406 zh 390x844 the shell reserves at least the rendered bar", async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, locale: "zh" });
  const page = await ctx.newPage();
  try {
    await openToday(page);
    const g = await read(page, SEL);
    const pad = parseInt(String(g.shellPaddingBottom), 10);
    save("zh_390x844_padding.json", { phase: PHASE, ...g, pad });
    console.log(`O406 ${PHASE} zh padding 390x844 nav_height=${g.navHeight} shell_padding_bottom=`
      + `${g.shellPaddingBottom} pad_covers_nav=${pad >= g.navHeight} evidence=${PHASE}/zh_390x844_padding.json`);
    expect(pad, "the scroll container has to reserve the bar's height").toBeGreaterThanOrEqual(g.navHeight);
  } finally {
    await ctx.close();
  }
});
