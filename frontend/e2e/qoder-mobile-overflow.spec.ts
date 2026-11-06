import { expect, test, type Page } from "@playwright/test";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";

/**
 * Qoder frontend track — mobile horizontal-overflow gate (GPT final call #2:
 * "Today 的 …360/412/430 overflow… 全绿后，再做 Live").
 *
 * The Apple-style refactor changes wrappers, section order and hero layout, so
 * a durable, programmatic no-horizontal-scroll check is the guard that lets me
 * restructure without a human eye on a screenshot. It fails if the document
 * itself scrolls sideways or any visible element pokes past the viewport edge.
 */

const WIDTHS = [360, 412, 430];

interface Offender {
  selector: string;
  testId: string | null;
  right: number;
  scrollWidth: number;
  clientWidth: number;
}

async function scanOverflow(page: Page): Promise<{
  docScrollWidth: number;
  innerWidth: number;
  offenders: Offender[];
}> {
  return page.evaluate(() => {
    const innerWidth = window.innerWidth;
    const docScrollWidth = Math.max(
      document.documentElement.scrollWidth,
      document.body.scrollWidth,
    );
    const describe = (el: Element): string => {
      const tid = el.getAttribute("data-testid");
      if (tid) return `[data-testid="${tid}"]`;
      const cls = (el.getAttribute("class") || "")
        .split(/\s+/)
        .filter(Boolean)
        .slice(0, 2)
        .join(".");
      return `${el.tagName.toLowerCase()}${cls ? "." + cls : ""}`;
    };
    const offenders: Offender[] = [];
    for (const el of Array.from(document.querySelectorAll<HTMLElement>("body *"))) {
      const style = getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden") continue;
      if (parseFloat(style.opacity) < 0.03) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < 2 || rect.height < 2) continue;
      // A clipped-scroll container is fine; only flag content that escapes the
      // viewport horizontally, or a box wider than its own clipped content.
      const escapes = rect.right > innerWidth + 1;
      const inner = el.scrollWidth > el.clientWidth + 1;
      // Ignore elements that deliberately scroll inside (allow-x), they can't
      // push the page wider.
      const isScroller = /auto|scroll/.test(style.overflowX);
      if (escapes && !isScroller && !el.closest("[data-allow-x]")) {
        offenders.push({
          selector: describe(el),
          testId: el.getAttribute("data-testid"),
          right: Math.round(rect.right),
          scrollWidth: el.scrollWidth,
          clientWidth: el.clientWidth,
        });
      } else if (inner && !isScroller && rect.right > innerWidth + 1) {
        offenders.push({
          selector: describe(el),
          testId: el.getAttribute("data-testid"),
          right: Math.round(rect.right),
          scrollWidth: el.scrollWidth,
          clientWidth: el.clientWidth,
        });
      }
    }
    return { docScrollWidth, innerWidth, offenders };
  });
}

test.describe("Qoder mobile-overflow (Today + Live)", () => {
  test("no horizontal overflow at 360/412/430 on Today and Live", async ({ page }) => {
    test.setTimeout(120_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await ensureDemoJourney(page);

    const failures: string[] = [];

    for (const width of WIDTHS) {
      await page.setViewportSize({ width, height: 844 });

      const today = {
        url: "/v8/hub",
        ready: ["today-heading", "today-attention"],
        label: `today@${width}`,
      };
      const live = {
        url: "/v8/live",
        ready: ["v8-live-journey", "live-journey-current"],
        label: `live@${width}`,
      };

      for (const screen of [today, live]) {
        await openScreen(page, screen.url, screen.ready);
        const result = await scanOverflow(page);
        if (result.docScrollWidth > result.innerWidth + 1) {
          failures.push(`${screen.label}: page scrolls sideways (${result.docScrollWidth}px > ${result.innerWidth}px)`);
        }
        for (const o of result.offenders.slice(0, 8)) {
          failures.push(`${screen.label}: ${o.selector} right=${o.right} (innerW ${result.innerWidth})`);
        }
      }
    }

    expect(
      failures,
      `mobile horizontal overflow:\n${failures.join("\n")}`,
    ).toEqual([]);
  });
});
