/**
 * ORD-0134 ② -- who owns a drag, stated as a rule and measured both ways with one instrument.
 *
 * The traveller's words: "滑不动，用力滑就滑到旁边的面板".  `cooperativeGestures: true` asked for two
 * fingers to pan the map, and on a phone one finger then did neither thing.  That flag is gone.  What
 * replaces it is not a vibes claim about "interaction being back on" but a rule this gate measures in
 * both directions at 390x844 with `hasTouch`:
 *
 *   a drag that starts on the map   => the map pans, the page does not move;
 *   a drag that starts on the page  => the page scrolls, the map does not move.
 *
 * Every gesture here is a hand-dispatched touchStart -> touchMove* -> touchEnd, i.e. the same event
 * sequence a finger produces, and every start point is first confirmed with `document.elementFromPoint`
 * so a covered pixel cannot be reported as "the map refused" or as "the map answered".  That mattered:
 * the first run of this gate was red for the wrong reason -- at the at-rest pose the map's centre is
 * under `nav.v8-mobile-nav` (y 768..844 measured at 390x844), so the finger was hitting a nav button
 * and no claim about the map could be made from it.  The gate now refuses to drag at a point it cannot
 * attribute, and says which element owns it instead.
 *
 * This replaces `ord0104-one-finger-on-the-map-scrolls-the-page`, whose criterion ("a one-finger swipe
 * on the map must scroll the page") ORD-0134 reversed.  The reversal is recorded in the ledger rather
 * than left as a silently deleted test, and the old readings stay on disk in
 * `review/evidence/ui-overnight/ord0104-map-gestures.json`.
 *
 * Two-finger zoom is deliberately *not* asserted.  In this harness `Input.synthesizeScrollGesture`
 * moves nothing at all (measured: 0 -> 0px on the page), so a synthesised pinch would be an unfired
 * stub reading green; whether two fingers zoom on a real device is left as an open question in the
 * evidence file rather than written as a pass.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-map-gesture-ownership.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };

async function readScene(page: Page): Promise<Record<string, string | null>> {
  const response = await page.request.get("/api/v1/demo/scene");
  return (await response.json()) as Record<string, string | null>;
}

type Camera = { lng: number; lat: number; zoom: number } | null;

async function camera(page: Page): Promise<Camera> {
  return page.evaluate(() => {
    const host = document.querySelector<HTMLElement>("[data-testid='v8-maplibre-canvas']");
    const map = (
      host as
        | (HTMLElement & {
            citynexusMap?: { getCenter: () => { lng: number; lat: number }; getZoom: () => number };
          })
        | null
    )?.citynexusMap;
    if (!map) return null;
    const center = map.getCenter();
    return {
      lng: Number(center.lng.toFixed(6)),
      lat: Number(center.lat.toFixed(7)),
      zoom: Number(map.getZoom().toFixed(4)),
    };
  });
}

/** MapLibre keeps easing after the finger lifts; compare cameras only once it has settled. */
async function settledCamera(page: Page): Promise<Camera> {
  let previous = await camera(page);
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await page.waitForTimeout(300);
    const current = await camera(page);
    if (JSON.stringify(current) === JSON.stringify(previous)) return current;
    previous = current;
  }
  return previous;
}

async function pageScroll(page: Page): Promise<number> {
  return page.evaluate(() => {
    const boxes = [
      document.documentElement,
      document.body,
      ...Array.from(document.querySelectorAll<HTMLElement>("main, .v8-content, .v8-page")),
    ];
    let top = 0;
    boxes.forEach((box) => {
      if (box.scrollTop > top) top = box.scrollTop;
    });
    return top;
  });
}

/** Who would actually receive a finger at this pixel, from the element up to `document`. */
async function hitChain(page: Page, x: number, y: number): Promise<string[]> {
  return page.evaluate(
    ([px, py]) => {
      const chain: string[] = [];
      let el: Element | null = document.elementFromPoint(px as number, py as number);
      let steps = 0;
      while (el && steps < 12) {
        const classes = (el as HTMLElement).className?.toString?.() ?? "";
        chain.push(`${el.tagName.toLowerCase()}.${classes.slice(0, 70)}`);
        el = el.parentElement;
        steps += 1;
      }
      return chain;
    },
    [x, y],
  );
}

const ownsMap = (chain: string[]) =>
  chain.some(
    (entry) => entry.includes("maplibregl-map") || entry.includes("maplibregl-canvas"),
  );

/** A finger: touchStart, a column of touchMoves, touchEnd. */
async function fingerDrag(
  page: Page,
  cdp: Awaited<ReturnType<Page["context"]["newCDPSession"]>>,
  from: { x: number; y: number },
  dy: number,
  steps = 10,
): Promise<void> {
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: [{ x: from.x, y: from.y, id: 1, force: 1.0 }],
  });
  for (let step = 1; step <= steps; step += 1) {
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchMove",
      touchPoints: [
        {
          x: from.x,
          y: Math.round(from.y + (dy * step) / steps),
          id: 1,
          force: 1.0,
        },
      ],
    });
    await page.waitForTimeout(30);
  }
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
}

test("ord0134-map-gesture-ownership — 拖地图地图动、拖页面页面动，各不抢对方的手势", async ({
  page,
  browser,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0134-map-gesture-ownership";
  const seeded = await readScene(page);
  const journeyId = String(seeded.journey_id ?? "");
  const use = test.info().project.use as { baseURL?: string; storageState?: string };
  const readings: Record<string, unknown> = {};

  const phone = await browser.newContext({
    hasTouch: true,
    viewport: VIEWPORT,
    baseURL: use.baseURL,
    storageState: use.storageState,
  });
  const target = await phone.newPage();
  const cdp = await phone.newCDPSession(target);
  try {
    await target.addInitScript(
      ([id]) => {
        localStorage.clear();
        localStorage.setItem("citynexus.v8.journey_id", id as string);
        localStorage.setItem("citynexus.locale", "zh-CN");
      },
      [journeyId],
    );
    await target.goto("/v8/hub", { waitUntil: "load" });
    await expect(target.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
    const canvas = target.getByTestId("v8-maplibre-canvas");
    await expect(
      canvas,
      node(NODE, "there is no live map on Today, so the ownership rule has no subject"),
    ).toBeVisible({ timeout: 30_000 });
    await target.waitForTimeout(2_500);

    readings["nav"] = await target.evaluate(() => {
      const nav = document.querySelector<HTMLElement>("nav.v8-mobile-nav");
      if (!nav) return null;
      const rect = nav.getBoundingClientRect();
      return { top: Math.round(rect.top), bottom: Math.round(rect.bottom) };
    });

    /**
     * Drag the map from one named pixel and assert both halves of the rule about that pixel.
     *
     * An earlier revision of this gate *collected* the pixels the map happened to own and skipped the
     * rest.  `killG2` (the caption given its `pointer-events` back) then SURVIVED: the caption point
     * fell out of the list, the bare centre still passed, and the gate read green while the panel was
     * swallowing a drag.  A covered point is itself the defect, so it is asserted, not filtered.
     */
    const dragMapFrom = async (
      pose: string,
      name: string,
      point: { x: number; y: number },
    ): Promise<void> => {
      const chain = await hitChain(target, point.x, point.y);
      expect(
        ownsMap(chain),
        node(
          NODE,
          `${pose}.${name}: a finger at (${point.x},${point.y}) belongs to ${JSON.stringify(
            chain[0] ?? null,
          )}, not to the map (chain ${JSON.stringify(chain.slice(0, 3))}) — the panel's own overlay is swallowing a drag the map was told to own`,
        ),
      ).toBe(true);
      const camBefore = await settledCamera(target);
      const scrollBefore = await pageScroll(target);
      await fingerDrag(target, cdp, point, -160);
      await target.waitForTimeout(600);
      const camAfter = await settledCamera(target);
      const scrollAfter = await pageScroll(target);
      const dLat = Math.abs(Number(camAfter?.lat ?? 0) - Number(camBefore?.lat ?? 0));
      expect(
        dLat,
        node(
          NODE,
          `a one-finger drag starting on the map at (${point.x},${point.y}) panned it ${dLat.toFixed(
            7,
          )}° in latitude (need >0.0005; ${JSON.stringify(camBefore)} -> ${JSON.stringify(camAfter)}) — the panel is still refusing a gesture it owns`,
        ),
      ).toBeGreaterThan(0.0005);
      expect(
        scrollAfter - scrollBefore,
        node(
          NODE,
          `the drag that started on the map at (${point.x},${point.y}) also scrolled the page by ${
            scrollAfter - scrollBefore
          }px (${scrollBefore} -> ${scrollAfter}); one gesture, two owners`,
        ),
      ).toBe(0);
      readings[`${pose}.${name}`] = {
        point,
        chain: chain.slice(0, 2),
        camBefore,
        camAfter,
        scrollBefore,
        scrollAfter,
      };
    };

    /**
     * The first pixel down the panel a finger can claim for the map at the pose it is in, scanning
     * top-down.  This one scan *may* skip covered pixels -- that is what "where can I even touch the
     * map" means -- but it records who covers each one and the caller fails if none is free.
     */
    const firstOwnedMapPixel = async (pose: string) => {
      const box = await canvas.boundingBox();
      if (!box) return null;
      const centreX = Math.round(box.x + box.width / 2);
      for (let offset = 12; offset <= box.height; offset += 12) {
        const y = Math.round(box.y + offset);
        if (y < 1 || y > VIEWPORT.height - 1) break;
        const chain = await hitChain(target, centreX, y);
        if (ownsMap(chain)) return { x: centreX, y, chain };
        readings[`rejected.${pose}.y${y}`] = { point: { x: centreX, y }, ownedBy: chain[0] ?? null };
      }
      return null;
    };

    /* --- Arm 1: the map owns a drag in the pose the traveller is actually in when it appears.
           Whether a pixel of the panel is above the fold at `scrollTop 0` is a property of how much
           content Today is carrying, not of gesture ownership (with three reservations on the hero the
           whole panel sits under the nav band, measured in the five-spec chain).  So this arm records
           reachability and drags where the finger can land; the ownership rule itself is asserted in
           every pose that *is* reachable, and Arm 2 guarantees at least one such pose exists. */
    const atRest = await firstOwnedMapPixel("at-rest");
    readings["at-rest.reachable"] = Boolean(atRest);
    readings["at-rest.geometry"] = {
      canvas: await canvas.boundingBox(),
      nav: readings["nav"],
      viewport: [VIEWPORT.width, VIEWPORT.height],
    };
    if (atRest) {
      await dragMapFrom("at-rest", "first-owned-pixel", { x: atRest.x, y: atRest.y });
    }

    /* --- Arm 2: same rule once the panel is clear of the nav, at the centre and over its words. */
    await target.evaluate(
      ([height]) => {
        const host = document.querySelector<HTMLElement>("[data-testid='v8-maplibre-canvas']")!;
        const nav = document.querySelector<HTMLElement>("nav.v8-mobile-nav");
        const navTop = nav ? nav.getBoundingClientRect().top : (height as number);
        window.scrollBy(0, host.getBoundingClientRect().top - (navTop - 60) / 2);
      },
      [VIEWPORT.height],
    );
    await target.waitForTimeout(600);
    readings["raised.setupScroll"] = await pageScroll(target);
    const mapBox = await canvas.boundingBox();
    expect(mapBox, node(NODE, "the map panel has no box to drag")).not.toBeNull();
    await dragMapFrom("raised", "canvas-centre", {
      x: Math.round(mapBox!.x + mapBox!.width / 2),
      y: Math.round(mapBox!.y + mapBox!.height / 2),
    });
    const caption = target.locator(".v8-map-caption");
    await expect(
      caption,
      node(NODE, "the map panel prints no caption, so the overlay that could steal a drag is absent and the rule is untested there"),
    ).toBeVisible();
    const captionBox = await caption.boundingBox();
    expect(captionBox, node(NODE, "the caption has no box: it is not on screen")).not.toBeNull();
    expect(
      captionBox!.y >= 0 && captionBox!.y + captionBox!.height <= VIEWPORT.height,
      node(NODE, `the caption sits at y ${Math.round(captionBox!.y)}..${Math.round(captionBox!.y + captionBox!.height)}, outside the ${VIEWPORT.height}px viewport -- "over-the-caption" would be measuring a finger that cannot reach it`),
    ).toBe(true);
    await dragMapFrom("raised", "over-the-caption", {
      x: Math.round(captionBox!.x + captionBox!.width / 2),
      y: Math.round(captionBox!.y + captionBox!.height / 2),
    });

    /* --- The panel must say what it now does.  The measured rule above is worthless if the words on
           screen still promise the cooperative-era gesture. */
    const hint = target.getByTestId("v8-map-gesture-hint");
    await expect(
      hint,
      node(NODE, "the map panel prints no gesture hint, so the ownership rule is invisible to the traveller"),
    ).toBeVisible();
    const hintText = (await hint.textContent()) ?? "";
    /* `toContain("单指")` was the first version of this and `killG3` walked straight through it: the
       mutant sentence "双指拖动地图 · 单指无效" contains 单指 too.  The criterion is the *promise*, so
       the phrase that carries it and the phrases that contradict it are both named. */
    const PROMISES_ONE_FINGER_PAN = ["单指拖动地图", "One finger drags the map"];
    const PROMISES_THE_OLD_GESTURE = [
      "双指拖动",
      "单指无效",
      "单指不能",
      "单指不可",
      "two fingers to pan",
      "two fingers pan",
    ];
    expect(
      PROMISES_ONE_FINGER_PAN.some((phrase) => hintText.includes(phrase)),
      node(
        NODE,
        `the panel tells the traveller "${hintText}"; it has to name the gesture that actually moves the map (one of ${JSON.stringify(
          PROMISES_ONE_FINGER_PAN,
        )}), which is what the two arms above measured`,
      ),
    ).toBe(true);
    const contradicting = PROMISES_THE_OLD_GESTURE.find((phrase) => hintText.includes(phrase));
    expect(
      contradicting,
      node(
        NODE,
        `the panel still says "${contradicting}" inside "${hintText}" -- the cooperative-era gesture it removed`,
      ),
    ).toBeUndefined();

    /* --- Arm 3: the page owns a drag that starts on it. */
    await target.evaluate(() => {
      document.documentElement.scrollTop = 0;
    });
    await target.waitForTimeout(600);
    const hero = await target.getByTestId("today-hero").boundingBox();
    expect(hero, node(NODE, "the Today hero is missing, so there is no page area to drag")).not.toBeNull();
    const pagePoint = {
      x: Math.round(hero!.x + hero!.width / 2),
      y: Math.round(hero!.y + hero!.height - 14),
    };
    const heroChain = await hitChain(target, pagePoint.x, pagePoint.y);
    expect(
      ownsMap(heroChain),
      node(NODE, `the page-area point (${pagePoint.x},${pagePoint.y}) is inside the map; arm 3 would be measuring the wrong subject`),
    ).toBe(false);
    const camBeforePage = await settledCamera(target);
    const scrollBeforePage = await pageScroll(target);
    await fingerDrag(target, cdp, pagePoint, -200, 12);
    await target.waitForTimeout(900);
    const scrollAfterPage = await pageScroll(target);
    expect(
      scrollAfterPage - scrollBeforePage,
      node(
        NODE,
        `a one-finger drag starting on the page at (${pagePoint.x},${pagePoint.y}) scrolled it ${
          scrollAfterPage - scrollBeforePage
        }px (need >60) — the page cannot scroll what the map is not holding`,
      ),
    ).toBeGreaterThan(60);
    expect(
      await settledCamera(target),
      node(NODE, "the page drag moved the map camera as well; ownership leaked"),
    ).toEqual(camBeforePage);
    readings["page-area"] = {
      point: pagePoint,
      chain: heroChain.slice(0, 3),
      camBefore: camBeforePage,
      camAfter: await settledCamera(target),
      scrollBefore: scrollBeforePage,
      scrollAfter: scrollAfterPage,
    };
    await target.screenshot({ path: evidencePath("ui-overnight/ord0134-map-ownership.png") });
  } finally {
    await cdp.detach().catch(() => undefined);
    await phone.close();
  }

  writeFileSync(
    evidencePath("ui-overnight/ord0134-map-ownership.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        order: "ORD-0134 residual 2",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        viewport: [VIEWPORT.width, VIEWPORT.height],
        hasTouch: true,
        rule: "drag starting on the map pans the map and not the page; drag starting on the page scrolls the page and not the map",
        instrument:
          "Input.dispatchTouchEvent touchStart -> touchMove xN -> touchEnd, same sequence for all three arms; every start point confirmed with document.elementFromPoint before it is used",
        replaces: "ord0104-one-finger-on-the-map-scrolls-the-page (criterion reversed by ORD-0134 ②)",
        notAsserted:
          "two-finger zoom: Input.synthesizeScrollGesture is inert in this harness (measured 0 -> 0px on the page during the probe run), so a synthesised pinch would be an unfired stub reading green. Whether two fingers zoom on a real device stays open.",
        coveredAtRest:
          "the map panel's centre is under nav.v8-mobile-nav until the page is scrolled; the at-rest arm therefore drags the first pixel the hit test attributes to the map rather than a point a nav button owns",
        readings,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});
