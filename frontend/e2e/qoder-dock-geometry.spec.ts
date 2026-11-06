import { expect, test } from "@playwright/test";
import { createFreshJourney, useJourney } from "./_qoderFreshJourney";
import { evidencePath } from "./_qoderEvidence";

/* pass-13 C — measure before claiming.
 *
 * pass-12 concluded from a 390x2250 fullPage capture that the Copilot pill sat
 * over the Live EVIDENCE row.  That was the wrong instrument: a `position: fixed`
 * overlay is painted at its viewport position into a stitched image, so a long
 * screenshot can show a collision that no user ever sees, and hide one that they
 * do.  This file therefore measures the real thing — the dock's rectangle and the
 * rectangles of the actionable elements under it, at 390x844, at three scroll
 * positions — and prints the numbers before it asserts anything.
 */

type Rect = { x: number; y: number; w: number; h: number };

type Probe = {
  dock: Rect & { idleCompact: string; state: string; yieldsAtRest: boolean };
  viewport: { w: number; h: number };
  scrollY: number;
  maxScroll: number;
  covered: { tag: string; label: string; rect: Rect; overlap: Rect }[];
  actionableInView: number;
  /* Reference case, reported but not asserted: the bottom navigation bar is also
     a fixed surface that the page scrolls beneath.  If it overlaps actionable
     elements at rest too, then "no resting overlap" is a shell-wide layout
     policy question rather than a dock bug, and that belongs in the report with
     numbers instead of being quietly asserted away. */
  nav: Rect & { present: boolean };
  coveredByNav: { tag: string; label: string; overlap: Rect }[];
};

async function probe(page: import("@playwright/test").Page): Promise<Probe> {
  return page.evaluate(() => {
    const dock = document.querySelector(".cn-copilot-dock") as HTMLElement | null;
    if (!dock) throw new Error("no .cn-copilot-dock on screen");
    const d = dock.getBoundingClientRect();
    const rect = (el: Element) => {
      const r = (el as HTMLElement).getBoundingClientRect();
      return { x: r.left, y: r.top, w: r.width, h: r.height };
    };
    const selectors = "button, a[href], input, select, textarea, [role=button]";
    const nav = document.querySelector(".v8-mobile-nav") as HTMLElement | null;
    const n = nav ? nav.getBoundingClientRect() : null;
    const scroller =
      document.scrollingElement ?? (document.querySelector(".v8-content") as Element | null);
    const candidates = Array.from(document.querySelectorAll(selectors)).filter((el) => {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return false;
      if (r.bottom < 0 || r.top > window.innerHeight) return false;
      return !dock.contains(el) && !el.contains(dock) && (!nav || (!nav.contains(el) && !el.contains(nav)));
    });
    const against = (band: DOMRect | null) =>
      band === null
        ? []
        : (candidates
            .map((el) => {
              const r = el.getBoundingClientRect();
              const x = Math.max(r.left, band.left);
              const y = Math.max(r.top, band.top);
              const right = Math.min(r.right, band.right);
              const bottom = Math.min(r.bottom, band.bottom);
              const w = right - x;
              const h = bottom - y;
              if (w <= 1 || h <= 1) return null;
              const label =
                (el.getAttribute("aria-label") ||
                  el.getAttribute("data-testid") ||
                  (el.textContent || "").trim().slice(0, 32) ||
                  el.tagName.toLowerCase());
              return { tag: el.tagName.toLowerCase(), label, rect: rect(el), overlap: { x, y, w, h } };
            })
            .filter(Boolean) as Probe["covered"]);
    const covered = against(d);
    const coveredByNav = against(n).map(({ tag, label, overlap }) => ({ tag, label, overlap }));
    return {
      dock: {
        ...rect(dock),
        idleCompact: dock.getAttribute("data-idle-compact") ?? "?",
        yieldsAtRest: dock.getAttribute("data-rest-overlap") === "true",
        state: dock.className.replace("cn-copilot-dock", "").trim() || "default",
      },
      viewport: { w: window.innerWidth, h: window.innerHeight },
      scrollY: Math.round(scroller ? scroller.scrollTop : window.scrollY),
      maxScroll: Math.round(scroller ? scroller.scrollHeight - scroller.clientHeight : 0),
      covered,
      actionableInView: candidates.length,
      nav: n
        ? { present: true, x: n.left, y: n.top, w: n.width, h: n.height }
        : { present: false, x: 0, y: 0, w: 0, h: 0 },
      coveredByNav,
    };
  });
}

function report(route: string, at: string, probe: Probe) {
  const lines = probe.covered.map(
    (c) =>
      `      ${c.tag} "${c.label}" rect=${Math.round(c.rect.y)}..${Math.round(
        c.rect.y + c.rect.h,
      )} overlap=${Math.round(c.overlap.w)}x${Math.round(c.overlap.h)}px`,
  );
  console.log(
    `[dock-geometry] ${route} @${at} viewport=${probe.viewport.w}x${probe.viewport.h} ` +
      `dock=${Math.round(probe.dock.x)},${Math.round(probe.dock.y)} ${Math.round(
        probe.dock.w,
      )}x${Math.round(probe.dock.h)} state=${probe.dock.state} idleCompact=${probe.dock.idleCompact} ` +
      `yieldsAtRest=${probe.dock.yieldsAtRest} ` +
      `scrollY=${probe.scrollY}/${probe.maxScroll} actionableInView=${probe.actionableInView} ` +
      `coveredByDock=${probe.covered.length} coveredByNav=${probe.coveredByNav.length} ` +
      `nav=${probe.nav.present ? `${Math.round(probe.nav.y)}..${Math.round(probe.nav.y + probe.nav.h)}` : "absent"}`,
  );
  for (const line of lines) console.log(line);
  for (const c of probe.coveredByNav) {
    console.log(
      `      (nav reference) ${c.tag} "${c.label}" overlap=${Math.round(c.overlap.w)}x${Math.round(c.overlap.h)}px`,
    );
  }
}

test.describe("Copilot dock geometry at phone size", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("citynexus.locale", "en"));
  });

  for (const route of ["/v8/live", "/v8/hub"]) {
    test(`${route}: no actionable element may rest under the dock at any settled scroll position`, async ({
      page,
      request,
    }) => {
      const { journeyId } = await createFreshJourney(request);
      await useJourney(page, journeyId);
      await page.setViewportSize({ width: 390, height: 844 });
      await page.goto(route);
      await page.waitForFunction(
        () => !!document.querySelector(".cn-copilot-dock"),
        undefined,
        { timeout: 15_000 },
      );
      await page.waitForTimeout(900);

      /* Measure the whole scroll range.  A fixed overlay necessarily passes over
         content while the page scrolls; what must never happen is a control being
         unreachable where the page comes to rest. */
      const positions = ["top", "middle", "bottom"] as const;
      const results = new Map<string, Probe>();
      for (const at of positions) {
        await page.evaluate((where) => {
          const scroller =
            document.scrollingElement ?? document.querySelector(".v8-content");
          if (!scroller) return;
          const max = scroller.scrollHeight - scroller.clientHeight;
          scroller.scrollTop = where === "top" ? 0 : where === "middle" ? max / 2 : max;
        }, at);
        await page.waitForTimeout(700);
        const measured = await probe(page);
        results.set(at, measured);
        report(route, at, measured);
      }

      if (route === "/v8/live") {
        await page.screenshot({
          path: evidencePath("dock-geometry/live-390x844-bottom.png"),
        });
      } else {
        await page.screenshot({
          path: evidencePath("dock-geometry/today-390x844-bottom.png"),
        });
      }

      /* pass-13 ruling: a stable scroll position is a stable position.  The
         middle sample below is taken after the scroll has come to rest, so an
         overlap there is not "the pill passing over content while you drag" — it
         is content sitting under the pill wherever the reader stopped.  All
         three resting positions must be clear. */
      for (const at of positions) {
        const measured = results.get(at)!;
        expect(
          measured.covered.length,
          `${route} @${at} at rest: ${measured.covered.length} actionable element(s) sit under ` +
            `the dock (${measured.covered
              .map((c) => `${c.tag} "${c.label}" overlap=${Math.round(c.overlap.w)}x${Math.round(c.overlap.h)}px`)
              .join("; ")})`,
        ).toBe(0);
      }

      /* pass-14 ruling on the navigation bar, which is deliberately different.
         A fixed bottom bar over a full-bleed column overlaps content at
         arbitrary scroll positions — that is the normal mobile scroll model, and
         forbidding it would cost a phone ~138px of every screen.  What it must
         never do is make something permanently unreachable, so the assertion
         applies at terminal scroll only.  The top and middle counts are printed
         above for exactly that reason: they are allowed, and they should still
         be visible to a reader. */
      const atEnd = results.get("bottom")!;
      expect(
        atEnd.coveredByNav.length,
        `${route} at maximum scroll: ${atEnd.coveredByNav.length} actionable element(s) cannot ` +
          `clear the fixed navigation bar (${atEnd.coveredByNav.map((c) => c.label).join(", ")})`,
      ).toBe(0);
    });
  }

  test("a yielded pill is not a ghost control: hidden, unfocusable, and back again", async ({
    page,
    request,
  }) => {
    const { journeyId } = await createFreshJourney(request);
    await useJourney(page, journeyId);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/v8/hub");
    await page.waitForFunction(() => !!document.querySelector(".cn-copilot-dock"), undefined, {
      timeout: 15_000,
    });
    await page.waitForTimeout(900);

    const dock = page.locator(".cn-copilot-dock");
    const scrollTo = (fraction: number) =>
      page.evaluate((f) => {
        const scroller = document.scrollingElement ?? document.querySelector(".v8-content");
        if (scroller) scroller.scrollTop = (scroller.scrollHeight - scroller.clientHeight) * f;
      }, fraction);

    /* Find the state rather than assume a layout: the yield depends on where the
       cards happen to rest, and a future spacing change should not be able to
       turn this into a test that quietly asserts nothing. */
    let engaged = false;
    for (const fraction of [0.5, 0.35, 0.65, 0.2, 0.8]) {
      await scrollTo(fraction);
      await page.waitForTimeout(700);
      if ((await dock.getAttribute("data-rest-overlap")) === "true") {
        engaged = true;
        break;
      }
    }
    expect(
      engaged,
      "the pill never entered yield-at-rest at any scroll position on Today, so this assertion " +
        "would be vacuous — either the overlap is gone (re-check the geometry gate) or the " +
        "yield stopped firing",
    ).toBe(true);

    await expect(dock, "a stepped-aside pill must be hidden, not merely transparent").toBeHidden();
    const focusability = await dock.evaluate((node) => {
      const control = node.querySelector("button") as HTMLButtonElement | null;
      if (!control) return { reason: "no control inside the dock" };
      control.focus();
      return {
        receivedFocus: document.activeElement === control,
        inAccessibilityTree: (node as HTMLElement).checkVisibility?.({
          checkVisibilityCSS: true,
          contentVisibilityAuto: true,
          opacityProperty: true,
          visibilityProperty: true,
        }),
      };
    });
    expect(focusability.receivedFocus, JSON.stringify(focusability)).toBe(false);
    expect(focusability.inAccessibilityTree, JSON.stringify(focusability)).toBe(false);

    /* And it must come back.  A yield that never returns is just a removed
       feature with extra steps. */
    let returned = false;
    for (const fraction of [1, 0, 0.9, 0.1]) {
      await scrollTo(fraction);
      await page.waitForTimeout(700);
      if ((await dock.getAttribute("data-rest-overlap")) === "false") {
        returned = true;
        break;
      }
    }
    expect(returned, "the pill never came back after the band cleared").toBe(true);
    await expect(dock).toBeVisible();
    await expect(page.locator(".cn-copilot-dock .cn-copilot-edge")).toBeEnabled();
    const backAgain = await dock.evaluate((node) => {
      const control = node.querySelector("button") as HTMLButtonElement | null;
      if (!control) return false;
      control.focus();
      return document.activeElement === control;
    });
    expect(backAgain, "a restored pill must be keyboard reachable again").toBe(true);
  });
});
