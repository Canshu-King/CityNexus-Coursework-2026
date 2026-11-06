import { expect, test, type Page } from "@playwright/test";
import { writeFileSync, readFileSync, existsSync } from "node:fs";
import { ensureDemoJourney, openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0035 — the "AI 帮我规划" sheet was unreadable on a real phone.
 *
 * Cause, measured: `.plan-ai-sheet` hardcoded a light background and never declared a
 * foreground, and its textarea said `color: inherit`.  Under a dark system theme the
 * inherited ink flips to near-white while the literal background stays near-white — so the
 * panel rendered as a white slab with white text.  This gate asserts the computed pair,
 * not a class name, because the bug was precisely a class name that looked fine.
 *
 * Both themes are asserted: a dark-only gate is how this shipped past review before.
 */

const SHEET = ".plan-ai-sheet";
const INPUT = ".plan-ai-sheet textarea";

interface InkReading {
  selector: string;
  color: string;
  background: string;
  contrast: number;
  fontSizePx: number;
  required: number;
}

async function readContrast(page: Page): Promise<InkReading[]> {
  return page.evaluate(
    ([sheetSel, inputSel]: string[]) => {
      const parse = (c: string): [number, number, number, number] => {
        const m = c.match(/rgba?\(([^)]+)\)/);
        if (!m) return [0, 0, 0, 1];
        const p = m[1].split(",").map((x) => parseFloat(x.trim()));
        return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
      };
      const relLum = (rgb: [number, number, number, number]) => {
        const lin = rgb.slice(0, 3).map((v) => {
          const s = v / 255;
          return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
        });
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
      };
      /* The panel's own background can be transparent, in which case what the text
         actually sits on is the nearest painted ancestor — that composite is the only
         honest denominator. */
      const effectiveBackground = (el: Element): string => {
        let node: Element | null = el;
        let r = 0, g = 0, b = 0, a = 0;
        let seen = false;
        while (node) {
          const bg = parse(getComputedStyle(node).backgroundColor);
          if (bg[3] > 0) {
            if (!seen) { [r, g, b] = bg.slice(0, 3); a = 1; seen = true; }
            else {
              r = bg[0] * bg[3] + r * (1 - bg[3]);
              g = bg[1] * bg[3] + g * (1 - bg[3]);
              b = bg[2] * bg[3] + b * (1 - bg[3]);
            }
            if (a === 1) break;
          }
          node = node.parentElement;
        }
        return seen ? `rgb(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)})` : "rgba(0,0,0,0)";
      };
      const out: {
        selector: string; color: string; background: string;
        contrast: number; fontSizePx: number; required: number;
      }[] = [];
      for (const selector of [sheetSel, inputSel]) {
        const el = document.querySelector(selector) as HTMLElement | null;
        if (!el) continue;
        const s = getComputedStyle(el);
        const fg = parse(s.color);
        const bgRgb = parse(effectiveBackground(el));
        const l1 = relLum(fg);
        const l2 = relLum([bgRgb[0], bgRgb[1], bgRgb[2], 1]);
        const contrast = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
        const px = parseFloat(s.fontSize);
        const bold = parseInt(s.fontWeight || "400", 10) >= 700;
        const large = px >= 24 || (px >= 18.66 && bold);
        out.push({
          selector,
          color: s.color,
          background: effectiveBackground(el),
          contrast: Math.round(contrast * 100) / 100,
          fontSizePx: px,
          required: large ? 3 : 4.5,
        });
      }
      return out;
    },
    [SHEET, INPUT],
  );
}

async function openSheetInScheme(page: Page, scheme: "light" | "dark") {
  await page.emulateMedia({ colorScheme: scheme, media: "screen" });
  await page.setViewportSize({ width: 390, height: 844 });
  await ensureDemoJourney(page);
  await openScreen(page, "/v8/hub", ["today-heading", "today-attention"]);
  await page.getByTestId("hub-plan-with-ai").click();
  await expect(page.getByTestId("plan-ai-sheet")).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(400);
}

test.describe("ORD-0035 plan-ai sheet ink is legible in both themes", () => {
  for (const scheme of ["light", "dark"] as const) {
    test(`${scheme}: sheet and textarea clear AA`, async ({ page }) => {
      test.setTimeout(180_000);
      await openSheetInScheme(page, scheme);
      const readings = await readContrast(page);

      const target = evidencePath("ui-overnight/plan-ai-contrast.json");
      const prior = existsSync(target) ? JSON.parse(readFileSync(target, "utf-8")) : {};
      const merged = { generatedBy: "e345b9d6", order: "ORD-0035", ...prior };
      merged[scheme] = readings;
      writeFileSync(target, JSON.stringify(merged, null, 2) + "\n", "utf-8");

      expect(readings.length, `${scheme}: neither sheet nor textarea was measurable`).toBe(2);
      for (const r of readings) {
        expect(
          r.contrast,
          `${scheme} ${r.selector}: ink ${r.color} on ${r.background} is ${r.contrast}:1 ` +
            `at ${r.fontSizePx}px, needs ${r.required}:1`,
        ).toBeGreaterThanOrEqual(r.required);
      }
    });
  }
});

/* A computed-contrast gate cannot catch `color: inherit` on a tokenised panel, because
   once the panel declares its own background the inherited ink resolves to the same
   value — measured: reverting the textarea to `color: inherit` leaves the gate green at
   15.63:1.  What must not come back is the *shape*: a literal light fill paired with an
   inherited foreground.  That is a source-level property, so it is asserted at source
   level, scoped to the .plan-ai-* block only — the wider light-only inventory is reported
   separately (review/ORD-0035-offpath-inventory.json), not silently policed here. */
test("no .plan-ai-* rule pairs a literal light fill with an inherited foreground", () => {
  const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf-8");
  const start = css.indexOf(".plan-ai-sheet {");
  const end = css.indexOf("/* Amendment F-2");
  expect(start, "the plan-ai block was not found in styles.css").toBeGreaterThan(-1);
  expect(end, "the plan-ai block's end marker moved").toBeGreaterThan(start);
  const block = css.slice(start, end);
  const rules = [...block.matchAll(/([^{}]+)\{([^{}]*)\}/g)];
  expect(rules.length, "no rules parsed out of the plan-ai block").toBeGreaterThan(5);
  const offenders = rules
    .filter(([, , body]) => {
      const bg = ((body.match(/background:\s*([^;]+)/) ?? [])[1] ?? "").trim();
      const literalLight = /#(fff|f[0-9a-f]{5}|e[0-9a-f]{5})/i.test(bg) || /white/i.test(bg);
      return literalLight && /color:\s*inherit/.test(body);
    })
    .map(([, sel]) => sel.split(/\s+/).filter(Boolean).join(" ").slice(0, 80));
  expect(
    offenders,
    `literal light fill + color:inherit in the plan-ai sheet: ${offenders.join(" | ")}`,
  ).toEqual([]);
});
