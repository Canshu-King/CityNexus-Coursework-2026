import { expect, test, type Page } from "@playwright/test";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * Qoder overnight UI lane — before/after capture (ORD-0009, acceptance §"四界面各
 * before/after").  This spec asserts nothing.  It exists so the visual evidence is
 * produced by the same seeded, authoritative backend the gates run against, rather
 * than by a hand-driven browser that may be pointed at a neighbour's :8000.
 *
 * CITYNEXUS_SHOT_LABEL must be set explicitly to "before" or "after"; without it
 * the capture skips, so no run can silently repaint a baseline.
 * Everything lands under CITYNEXUS_EVIDENCE_DIR, never in the tracked tree.
 */

/* An unlabelled run must never be allowed to overwrite a baseline.  The first
   version defaulted to "before", and a whole-suite `npx playwright test` then
   silently repainted every before/ image with post-change pixels. */
const RAW_LABEL = process.env.CITYNEXUS_SHOT_LABEL ?? "";
const LABEL = /^(before|after)$/.test(RAW_LABEL) ? RAW_LABEL : "";

const SHOT = { width: 412, height: 915 };

const SHOTS: {
  name: string;
  url: string;
  ready: string[];
  fresh?: boolean;
  step?: 1 | 2 | 3;
  openSheet?: { trigger: string; appear: string };
}[] = [
  { name: "today", url: "/v8/hub", ready: ["today-heading", "today-attention"] },
  { name: "live", url: "/v8/live", ready: ["v8-live-journey", "live-journey-current"] },
  { name: "wallet", url: "/v8/bookings", ready: ["v8-bookings"] },
  { name: "setup-step1", url: "/v8", ready: ["trial-setup"], fresh: true },
  { name: "setup-step3", url: "/v8", ready: ["trial-setup"], fresh: true, step: 3 },
  /* ORD-0035: the surface the user actually photographed. Not a route — a sheet
     opened from Today, so it needs its own trigger. */
  {
    name: "plan-ai-sheet",
    url: "/v8/hub",
    ready: ["today-heading", "today-attention"],
    openSheet: { trigger: "hub-plan-with-ai", appear: "plan-ai-sheet" },
  },
];

async function shoot(page: Page, shot: (typeof SHOTS)[number], scheme: "light" | "dark") {
  await page.emulateMedia({ colorScheme: scheme, media: "screen" });
  await page.setViewportSize({ width: SHOT.width, height: SHOT.height });
  if (shot.fresh) {
    await page.addInitScript(() => {
      try {
        localStorage.clear();
      } catch {
        /* nothing to clear before the first commit */
      }
    });
  }
  await openScreen(page, shot.url, shot.ready);
  if (shot.openSheet) {
    await page.getByTestId(shot.openSheet.trigger).click();
    await expect(page.getByTestId(shot.openSheet.appear)).toBeVisible({ timeout: 15_000 });
  }
  if (shot.step === 3) {
    // Walk the three-step form to its review page with the demo defaults.
    await page.getByTestId("trial-city").fill("Tianjin");
    await page.getByTestId("trial-arrival").fill("Tianjin West Station");
    await page.getByTestId("trial-departure").fill("Tianjin Binhai Airport");
    await page.getByTestId("trial-next").click();
    await page.waitForTimeout(400);
    await page.getByTestId("trial-next").click();
    await page.waitForTimeout(600);
  }
  const target = evidencePath(`${LABEL}/${shot.name}-${scheme}-${SHOT.width}x${SHOT.height}.png`);
  await page.screenshot({ path: target, fullPage: true });
  // A viewport-sized frame too: fullPage hides what is actually above the fold.
  const foldTarget = evidencePath(
    `${LABEL}/${shot.name}-${scheme}-${SHOT.width}x${SHOT.height}-fold.png`,
  );
  await page.screenshot({ path: foldTarget, fullPage: false });
  console.log(`[shots] wrote ${target}`);
}

test.describe("Qoder ui-overnight before/after capture", () => {
  for (const shot of SHOTS) {
    test(`${shot.name} light + dark`, async ({ page }) => {
      test.skip(!LABEL, "set CITYNEXUS_SHOT_LABEL=before|after to capture; refusing to guess");
      test.setTimeout(120_000);
      if (!shot.fresh) await ensureDemoJourney(page);
      await shoot(page, shot, "light");
      await shoot(page, shot, "dark");
    });
  }
});
