import type { Page } from "@playwright/test";

/**
 * Shared dark-mode contrast scan for the Qoder frontend gates.
 *
 * Implements GPT's final dark gate table as a single source of truth used by
 * both e2e/qoder-contrast-audit.spec.ts (real Today/Live surfaces) and
 * e2e/qoder-live-structure.spec.ts (the mocked L1/L2/L3 replan states), so the
 * four tiers are judged identically everywhere:
 *   FAIL — text AA (body <4.5, large <3), a required control boundary <3:1, a
 *          state indicator <3:1 (colour-only), and ~white un-tokenised surfaces.
 *   WARN — decorative borders (counted, never gating).
 * Intent comes from data-contrast-role in the DOM, not from guessing CSS.
 */

export interface Violation {
  kind: "text-contrast" | "light-surface" | "control-boundary" | "state-indicator"
    | "unclassified-control";
  selector: string;
  testId: string | null;
  text: string;
  color: string;
  background: string;
  fontSizePx: number;
  bold: boolean;
  contrast: number;
  required: number;
}

export interface ScanResult {
  count: number;
  rows: Violation[];
  warnings: number;
}

export async function scanContrast(
  page: Page,
  opts: { scheme?: "dark" | "light" } = {},
): Promise<ScanResult> {
  // The "light-surface" tier means "an un-tokenised near-white block now
  // floating on the dark theme". Under the light theme a white surface is
  // correct, so that tier must not fire there — otherwise the light audit is
  // mostly noise and the real text failures hide inside it.
  const { scheme = "dark" } = opts;
  return page.evaluate(({ scheme }) => {
    type Row = Violation;
    const parse = (value: string): [number, number, number, number] => {
      const m = value.match(/rgba?\(([^)]+)\)/);
      if (!m) return [0, 0, 0, value === "transparent" ? 0 : 1];
      const p = m[1].split(",").map((x) => parseFloat(x.trim()));
      return [p[0] || 0, p[1] || 0, p[2] || 0, p.length > 3 ? p[3] : 1];
    };
    const compositeOver = (
      fg: [number, number, number, number],
      bg: [number, number, number],
    ): [number, number, number] => [
      fg[0] * fg[3] + bg[0] * (1 - fg[3]),
      fg[1] * fg[3] + bg[1] * (1 - fg[3]),
      fg[2] * fg[3] + bg[2] * (1 - fg[3]),
    ];
    const channel = (c: number) => {
      const s = c / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    };
    const lum = (rgb: [number, number, number]) =>
      0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2]);
    const contrast = (a: [number, number, number], b: [number, number, number]) => {
      const la = lum(a);
      const lb = lum(b);
      return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
    };
    const backgroundUnder = (el: Element | null): [number, number, number] => {
      const stack: Array<[number, number, number, number]> = [];
      let cur: Element | null = el;
      while (cur) {
        const bg = parse(getComputedStyle(cur).backgroundColor);
        if (bg[3] > 0) {
          stack.push(bg);
          if (bg[3] >= 0.999) break;
        }
        cur = cur.parentElement;
      }
      let acc: [number, number, number] = [0, 0, 0];
      for (let i = stack.length - 1; i >= 0; i--) acc = compositeOver(stack[i], acc);
      return acc;
    };
    const hasDirectText = (el: Element): string => {
      let text = "";
      el.childNodes.forEach((n) => {
        if (n.nodeType === 3) text += n.textContent ?? "";
      });
      return text.trim();
    };
    const isVisible = (el: Element): boolean => {
      const style = getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden") return false;
      if (parseFloat(style.opacity) < 0.03) return false;
      const rect = el.getBoundingClientRect();
      if (rect.width < 2 || rect.height < 2) return false;
      if (rect.bottom < 0 || rect.top > innerHeight || rect.right < 0 || rect.left > innerWidth)
        return false;
      return true;
    };
    const describe = (el: Element): string => {
      const tid = el.getAttribute("data-testid");
      if (tid) return `[data-testid="${tid}"]`;
      const cls = (el.getAttribute("class") || "").split(/\s+/).filter(Boolean).slice(0, 3).join(".");
      return `${el.tagName.toLowerCase()}${cls ? "." + cls : ""}`;
    };

    const rows: Row[] = [];
    const all = Array.from(document.querySelectorAll<HTMLElement>("body *"));
    const bw = (st: CSSStyleDeclaration) => parseFloat(st.borderTopWidth) || 0;

    for (const el of all) {
      if (!isVisible(el)) continue;
      const style = getComputedStyle(el);
      const bg = backgroundUnder(el);
      const selfBg = parse(style.backgroundColor);

      if (scheme === "dark" && selfBg[3] > 0.6 && lum(bg) > 0.82) {
        rows.push({
          kind: "light-surface",
          selector: describe(el),
          testId: el.getAttribute("data-testid"),
          text: (el.getAttribute("class") || "").slice(0, 60),
          color: style.color,
          background: style.backgroundColor,
          fontSizePx: parseFloat(style.fontSize),
          bold: parseInt(style.fontWeight || "400", 10) >= 600,
          contrast: 0,
          required: 0,
        });
      }

      const text = hasDirectText(el);
      if (text) {
        const color = parse(style.color);
        if (color[3] >= 0.05) {
          const ratio = contrast(compositeOver(color, bg), bg);
          const sizePx = parseFloat(style.fontSize);
          const weight = parseInt(style.fontWeight || "400", 10);
          const required = sizePx >= 24 || (sizePx >= 18.66 && weight >= 700) ? 3.0 : 4.5;
          if (ratio < required) {
            rows.push({
              kind: "text-contrast",
              selector: describe(el),
              testId: el.getAttribute("data-testid"),
              text: text.slice(0, 60),
              color: style.color,
              background: style.backgroundColor,
              fontSizePx: sizePx,
              bold: weight >= 600,
              contrast: Math.round(ratio * 100) / 100,
              required,
            });
          }
        }
      }
    }

    let warnings = 0;
    for (const el of all) {
      if (!isVisible(el)) continue;
      const style = getComputedStyle(el);
      const bg = backgroundUnder(el);
      const role = el.getAttribute("data-contrast-role");
      const isControl = role === "control" || el.classList.contains("v8-outline-button");
      const isState = role === "state";
      const parentBg = el.parentElement ? backgroundUnder(el.parentElement) : bg;

      /* ORD-0509: intent must be declared, not guessed. A control a traveller can see and tap -- an
       * interactive element carrying its own test identity -- with no data-contrast-role is a finding
       * in itself: without the role this ruler silently stops measuring that element, so a screen can
       * read as clean while not one of its buttons was ever judged. This arm is what makes hiding a
       * role cost something. */
      const looksControl = el.tagName === "BUTTON" || el.getAttribute("role") === "button";
      if (looksControl && !role && el.getAttribute("data-testid")) {
        const fill = parse(style.backgroundColor);
        const fillC = fill[3] > 0.5 ? contrast(compositeOver(fill, parentBg), parentBg) : 0;
        const bc = parse(style.borderTopColor);
        const borderC = bw(style) >= 1 && bc[3] > 0.3 ? contrast(compositeOver(bc, parentBg), parentBg) : 0;
        rows.push({
          kind: "unclassified-control",
          selector: describe(el),
          testId: el.getAttribute("data-testid"),
          text: (hasDirectText(el) || "").slice(0, 40),
          color: style.borderTopColor,
          background: style.backgroundColor,
          fontSizePx: parseFloat(style.fontSize),
          bold: false,
          contrast: Math.round(Math.max(fillC, borderC) * 100) / 100,
          required: 3,
        });
        continue;
      }

      if (isControl) {
        const fill = parse(style.backgroundColor);
        const fillC = fill[3] > 0.5 ? contrast(compositeOver(fill, parentBg), parentBg) : 0;
        const bc = parse(style.borderTopColor);
        const borderC = bw(style) >= 1 && bc[3] > 0.3 ? contrast(compositeOver(bc, parentBg), parentBg) : 0;
        if (fillC < 3 && borderC < 3) {
          rows.push({
            kind: "control-boundary",
            selector: describe(el),
            testId: el.getAttribute("data-testid"),
            text: (hasDirectText(el) || el.getAttribute("class") || "").slice(0, 40),
            color: style.borderTopColor,
            background: style.backgroundColor,
            fontSizePx: parseFloat(style.fontSize),
            bold: false,
            contrast: Math.round(Math.max(fillC, borderC) * 100) / 100,
            required: 3,
          });
        }
        continue;
      }
      if (isState) {
        const fill = parse(style.backgroundColor);
        const fillC = fill[3] > 0.5 ? contrast(compositeOver(fill, parentBg), parentBg) : 0;
        const color = parse(style.color);
        const colorC = color[3] > 0.05 ? contrast(compositeOver(color, bg), bg) : 0;
        const label = el.querySelector<HTMLElement>("span") ?? el;
        const weight = parseInt(getComputedStyle(label).fontWeight || "400", 10);
        const before = getComputedStyle(el, "::before").content;
        const hasBar = !!before && before !== "none" && before !== "normal";
        const pass = fillC >= 3 || colorC >= 3 || (weight >= 600 && colorC >= 1.5) || (fillC >= 1.3 && (weight >= 600 || hasBar));
        if (!pass) {
          rows.push({
            kind: "state-indicator",
            selector: describe(el),
            testId: el.getAttribute("data-testid"),
            text: (hasDirectText(el) || el.getAttribute("class") || "").slice(0, 40),
            color: style.color,
            background: style.backgroundColor,
            fontSizePx: parseFloat(style.fontSize),
            bold: weight >= 600,
            contrast: Math.round(Math.max(fillC, colorC) * 100) / 100,
            required: 3,
          });
        }
        continue;
      }
      if (bw(style) >= 1 && parse(style.borderTopColor)[3] > 0.2) warnings += 1;
    }

    return { count: all.length, rows, warnings };
  }, { scheme });
}
