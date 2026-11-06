import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";
import { probeMobileGeometry, MIN_TOUCH_PX, type MobileGeometry } from "./_qoderMobileScan";

/**
 * Qoder overnight UI lane — widened mobile acceptance gate (ORD-0009 limit (3)).
 *
 * The gate this extends, e2e/qoder-mobile-overflow.spec.ts, is left untouched on
 * purpose: it measures 360/412/430 at a fixed 844 height on Today and Live only.
 * The Amendment-C acceptance口径 is 360x800 / 390x844 / 412x915 across four
 * surfaces, and neither Journey creation nor the Wallet was measured at any size.
 *
 * Deliberately additive: it adds surfaces, adds viewports and adds checks.  It
 * relaxes no threshold and edits no existing assertion.  The first run is the
 * red/green baseline the commander asked for, so it writes its raw measurements
 * to the evidence directory *before* it asserts — a failure must not cost us the
 * numbers that caused it.
 *
 * Presentation only: this file reads boxes and computed styles, never API truth.
 */

const VIEWPORTS = [
  { name: "360x800", width: 360, height: 800 },
  { name: "390x844", width: 390, height: 844 },
  { name: "412x915", width: 412, height: 915 },
];

const SEEDED_SCREENS = [
  {
    key: "today",
    url: "/v8/hub",
    ready: ["today-heading", "today-attention"],
    /* The hero CTA first.  It is not present in every journey state: an earlier
       test in the same serial run can drive the shared journey into a proposal
       state where the hero renders no primary action at all (measured matches=0,
       i.e. absent from the DOM, not hidden).  The hub and page-intro actions
       exist in every state, so the 44px / occlusion assertions stay meaningful
       instead of turning into a false "no CTA" red. */
    ctaCandidates: [
      '[data-testid="today-hero-cta"]',
      ".v8-trip-hero .v8-primary",
      ".v8-page .v8-primary",
      ".v8-hub-actions button",
      ".v8-page-intro button",
    ],
  },
  {
    key: "live",
    url: "/v8/live",
    ready: ["v8-live-journey", "live-journey-current"],
    /* Pass-8 ruling #3 demoted Live's two full-width buttons to text actions in
       .cn-live-toolbar, so there is no .v8-primary here by design.  The closest
       thing to a decision on this screen is the sheet that compares routes. */
    ctaCandidates: [
      ".v8-compare-button",
      '[data-testid="live-toolbar-actions"] button',
      ".v8-mode-chips button.active",
      ".cn-live-secondary button",
    ],
  },
  {
    key: "wallet",
    url: "/v8/bookings",
    ready: ["v8-bookings"],
    ctaCandidates: [
      '[data-testid="v8-bookings"] .v8-primary',
      '[data-testid="wallet-reservation"]',
      ".v8-page .v8-primary",
      '[data-testid="inbox-tab-paste"]',
      ".v10-inbox-workbench button",
    ],
  },
];

const SETUP_SCREEN = {
  key: "setup",
  url: "/v8",
  ready: ["trial-setup"],
  ctaCandidates: [
    ".trial-sticky-cta .v8-primary-button",
    ".trial-sticky-cta button",
    '[data-testid="trial-next"]',
  ],
};

type Row = {
  screen: string;
  viewport: string;
  geometry: MobileGeometry;
  failures: string[];
};

function judge(label: string, screenKey: string, g: MobileGeometry): string[] {
  const failures: string[] = [];

  if (g.docScrollWidth > g.innerWidth + 1) {
    failures.push(`${label}: page scrolls sideways (${g.docScrollWidth}px > ${g.innerWidth}px)`);
  }
  for (const o of g.offenders.slice(0, 20)) {
    failures.push(
      `${label}: overflow ${o.side} ${o.selector} right=${o.right} left=${o.left} (innerW ${g.innerWidth})`,
    );
  }
  for (const t of g.smallTargets.slice(0, 20)) {
    failures.push(
      `${label}: touch target ${t.selector} "${t.text}" is ${t.width}x${t.height} (<${MIN_TOUCH_PX}px)`,
    );
  }
  for (const t of g.truncated.slice(0, 20)) {
    failures.push(
      `${label}: truncated text ${t.selector} "${t.text}" (${t.scrollWidth}>${t.clientWidth})`,
    );
  }
  for (const c of g.chromeOverlaps.slice(0, 8)) {
    failures.push(`${label}: chrome overlaps ${c.a} x ${c.b} by ${c.overlapPx}px`);
  }
  if (g.cta) {
    const c = g.cta;
    const sd = c.scrollDiagnostic;
    const scrollNote =
      ` [scroller=${sd.scroller} scrollTop ${sd.scrollTopBefore}->${sd.scrollTopAfter}` +
      ` (wanted ${sd.requestedScroll} of ${sd.scrollHeight - sd.clientHeight}), ctaTop ${sd.topBefore}->${sd.topAfter}]`;
    if (c.offscreenTop) failures.push(`${label}: primary CTA ${c.selector} scrolled above the viewport${scrollNote}`);
    if (c.offscreenRight) failures.push(`${label}: primary CTA ${c.selector} escapes the right edge${scrollNote}`);
    if (sd.scrollBlocked) {
      /* Reported instead of an occlusion verdict: with the scroll refused, every
         rect below the fold reads as "under the nav" whether or not it is. */
      failures.push(`${label}: page refused to scroll to the primary CTA${scrollNote}`);
    } else {
      if (c.coveredByNavPx > 0) {
        failures.push(
          `${label}: primary CTA ${c.selector} covered by the bottom nav by ${c.coveredByNavPx}px${scrollNote}`,
        );
      }
      if (c.coveredByChromePx > 0) {
        failures.push(
          `${label}: primary CTA ${c.selector} covered by fixed/sticky chrome by ${c.coveredByChromePx}px${scrollNote}`,
        );
      }
    }
    if (c.rect.height < MIN_TOUCH_PX) {
      failures.push(
        `${label}: primary CTA ${c.selector} is ${Math.round(c.rect.height)}px tall (<${MIN_TOUCH_PX}px)`,
      );
    }
  } else if (screenKey !== "wallet") {
    /* Wallet may legitimately show no actionable primary control when there is
       nothing to import; that is an empty-state question for the audit, not a
       geometry violation.  The other three screens must always have one. */
    const probed = g.ctaCandidates
      .map(
        (c) =>
          `    ${c.selector} -> matches=${c.matches} painted=${c.firstPainted} display=${c.display} visibility=${c.visibility} opacity=${c.opacity} animation=${c.animation}/${c.animationState} box=${c.width}x${c.height}`,
      )
      .join("\n");
    failures.push(
      `${label}: no visible primary CTA found\n${probed}`,
    );
  }

  return failures;
}

async function measure(
  page: Page,
  screen: (typeof SEEDED_SCREENS)[number],
  vp: (typeof VIEWPORTS)[number],
): Promise<Row> {
  await page.setViewportSize({ width: vp.width, height: vp.height });
  await openScreen(page, screen.url, screen.ready);
  /* The probe scrolls the CTA into view before judging it, so the geometry it
     returns is the state a user would actually be shown. */
  const geometry = await probeMobileGeometry(page, { ctaCandidates: screen.ctaCandidates });
  const label = `${screen.key}@${vp.name}`;
  return {
    screen: screen.key,
    viewport: vp.name,
    geometry,
    failures: judge(label, screen.key, geometry),
  };
}

test.describe("Qoder mobile-overnight gate (Today/Live/Wallet x 360/390/412)", () => {
  test("no overflow, 44px targets, unblocked CTA, no truncation", async ({ page }) => {
    test.setTimeout(300_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await ensureDemoJourney(page);

    const rows: Row[] = [];
    for (const vp of VIEWPORTS) {
      for (const screen of SEEDED_SCREENS) {
        rows.push(await measure(page, screen, vp));
      }
    }

    writeFileSync(
      evidencePath("ui-overnight/geometry-seeded.json"),
      JSON.stringify({ generatedBy: "e345b9d6", head: "00dfd62", rows }, null, 2) + "\n",
      "utf-8",
    );

    const failures = rows.flatMap((r) => r.failures);
    expect(
      failures,
      `mobile acceptance failures:\n${failures.join("\n")}`,
    ).toEqual([]);
  });
});

test.describe("Qoder mobile-overnight gate (Journey creation, first entry)", () => {
  test("no overflow, 44px targets, unblocked CTA, no truncation", async ({ page }) => {
    test.setTimeout(180_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    /* First entry = no journey id in storage. V8Experience.tsx auto-opens
       JourneyTrialSetup when it finds none, and :818 returns it *outside*
       .v8-product-shell, which is why this surface is measured on its own. */
    await page.addInitScript(() => {
      try {
        localStorage.clear();
      } catch {
        /* storage can be unavailable before the first commit; nothing to clear */
      }
    });

    const rows: Row[] = [];
    const onboarding: Array<{ viewport: string; facts: MobileGeometry["onboarding"] }> = [];
    for (const vp of VIEWPORTS) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await openScreen(page, SETUP_SCREEN.url, SETUP_SCREEN.ready);
      const geometry = await probeMobileGeometry(page, {
        ctaCandidates: SETUP_SCREEN.ctaCandidates,
      });
      const label = `${SETUP_SCREEN.key}@${vp.name}`;
      onboarding.push({ viewport: vp.name, facts: geometry.onboarding });
      const f = geometry.onboarding;
      const extra: string[] = [];
      /* Amendment F-1: onboarding carries exactly one navigation. A visible bottom
         tab bar here would also be an exit into half-initialised destinations. */
      if (f.bottomNavItemCount > 0) {
        extra.push(
          `${label}: onboarding renders ${f.bottomNavItemCount} bottom-nav item(s); F-1 requires 0`,
        );
      }
      if (!f.stickyCtaVisible) extra.push(`${label}: sticky CTA is not visible`);
      else if (f.stickyCtaBottom > f.innerHeight + 1) {
        extra.push(`${label}: sticky CTA bottom ${f.stickyCtaBottom} escapes viewport ${f.innerHeight}`);
      }
      /* env() computes to 0 without a notch, so the measured padding cannot tell
         "declared" from "missing" — assert the authored declaration instead. */
      if (!f.stickyCtaDeclaresSafeArea) {
        extra.push(
          `${label}: .trial-sticky-cta does not declare env(safe-area-inset-bottom) ` +
            `[matched ${f.stickyCtaRuleCount} rule(s); padding-bottom="${f.stickyCtaPaddingBottomDeclared}"; ` +
            `authored=${JSON.stringify(f.stickyCtaRuleTexts)}]`,
        );
      }
      rows.push({
        screen: SETUP_SCREEN.key,
        viewport: vp.name,
        geometry,
        failures: [...judge(label, SETUP_SCREEN.key, geometry), ...extra],
      });
    }

    writeFileSync(
      evidencePath("ui-overnight/geometry-setup.json"),
      JSON.stringify({ generatedBy: "e345b9d6", head: "00dfd62", onboarding, rows }, null, 2) + "\n",
      "utf-8",
    );

    const failures = rows.flatMap((r) => r.failures);
    expect(failures, `journey-creation failures:\n${failures.join("\n")}`).toEqual([]);
  });
});

/**
 * Amendment F-2, the other direction.
 *
 * The contract is layered: >=44px on touch/mobile, 30px allowed as compact fine-pointer
 * density on desktop.  The mobile half is already asserted by the touch-target check in
 * every row above.  This test exists because without it, "we raised it to 44px" and
 * "we inflated every control in the app" produce identical mobile numbers — only the
 * desktop measurement distinguishes them.
 */
test.describe("Qoder touch-target contract is layered (F-2)", () => {
  /* Each control lives on its own screen; measuring both on one route would report
     "nothing rendered to measure" for whichever is not there. */
  const PAIRS = [
    { url: "/v8/hub", ready: ["today-heading", "today-attention"], selector: ".v8-hub-actions .v8-quiet-button" },
    { url: "/v8/live", ready: ["v8-live-journey", "live-journey-current"], selector: ".cn-live-toolbar .v8-quiet-button" },
  ];
  const VIEWS = [
    { name: "desktop-1280x800", width: 1280, height: 800 },
    { name: "mobile-390x844", width: 390, height: 844 },
  ];

  const heights = (page: Page, selector: string) =>
    page.evaluate((sel: string) => {
      return Array.from(document.querySelectorAll<HTMLElement>(sel))
        .filter((el) => {
          const s = getComputedStyle(el);
          if (s.display === "none" || s.visibility === "hidden") return false;
          const r = el.getBoundingClientRect();
          return r.width >= 2 && r.height >= 2;
        })
        .map((el) => Math.round(el.getBoundingClientRect().height));
    }, selector);

  test("fine-pointer desktop keeps the compact density, mobile takes >=44px", async ({ page }) => {
    test.setTimeout(240_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await ensureDemoJourney(page);

    const measured: Record<string, Record<string, number[]>> = {};
    for (const view of VIEWS) {
      await page.setViewportSize({ width: view.width, height: view.height });
      measured[view.name] = {};
      for (const pair of PAIRS) {
        await openScreen(page, pair.url, pair.ready);
        measured[view.name][pair.selector] = await heights(page, pair.selector);
      }
    }

    writeFileSync(
      evidencePath("ui-overnight/touch-contract-layering.json"),
      JSON.stringify({ generatedBy: "e345b9d6", head: "00dfd62", measured }, null, 2) + "\n",
      "utf-8",
    );

    const failures: string[] = [];
    for (const pair of PAIRS) {
      const d = measured["desktop-1280x800"][pair.selector] ?? [];
      const m = measured["mobile-390x844"][pair.selector] ?? [];
      if (!d.length) failures.push(`desktop ${pair.selector}: nothing rendered to measure`);
      if (!m.length) failures.push(`mobile ${pair.selector}: nothing rendered to measure`);
      for (const h of d) {
        if (h > 34) {
          failures.push(`desktop ${pair.selector} is ${h}px; F-2 keeps compact density here, not 44`);
        }
      }
      for (const h of m) {
        if (h < 44) failures.push(`mobile ${pair.selector} is ${h}px; below the >=44px touch contract`);
      }
    }
    expect(failures, `touch contract:\n${failures.join("\n")}`).toEqual([]);
  });
});
