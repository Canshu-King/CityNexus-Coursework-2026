import type { Page } from "@playwright/test";

/**
 * The two text scans one gate should never re-implement differently each time.
 *
 * Measured twice on this machine (2026-09-22, ledger): a `body.innerText` assertion counted
 * `.v8-sr-only` nodes as "on screen" and produced a retracted report, while the same screen read
 * through leaf elements with a real box said `0 visible instances`.  So "is it visible" and "does
 * anything speak it" are **two** measurements, and a gate states which one it is making:
 *
 *  - `visual`  -- text actually painted: excludes `aria-hidden`, `display:none`, `visibility:hidden`,
 *    `opacity:0`, zero-box nodes and `.v8-sr-only`.
 *  - `accessible` -- everything an announced surface can carry, i.e. the same leaves *including* the
 *    screen-reader-only ones, plus rendered `aria-label` / `title` / `alt`.
 *
 * A raw internal token must be absent from **both**: leaking into the visible layer is a human-facing
 * defect, leaking into the accessible one is an accessibility defect.
 */
export type TextScan = { visual: string; accessible: string; visualLeaves: number };

export async function scanScreenText(page: Page): Promise<TextScan> {
  return await page.evaluate(() => {
    const rendered = (el: Element): boolean => {
      const box = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      if (el.closest('[aria-hidden="true"]')) return false;
      if (style.display === "none" || style.visibility === "hidden") return false;
      /* Measured on this Chromium (Playwright 1.63): a child of a **closed** `<details>` still
         computes `display:block`, `visibility:visible`, `content-visibility:visible` and a real
         330x77 box, so no style read sees the collapse.  The engine-independent signal is the DOM
         state itself -- the traveller has to open it. */
      const host = el.closest("details");
      if (host && !host.open && !el.closest("summary")) return false;
      /* Engines that do implement it hide the content this way; kept because it is free and correct. */
      if (style.contentVisibility === "hidden") return false;
      if (Number(style.opacity) === 0) return false;
      return box.width > 0 && box.height > 0;
    };
    const srOnly = (el: Element): boolean => !!el.closest(".v8-sr-only");
    const visual: string[] = [];
    const accessible: string[] = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let next = walker.nextNode(); next; next = walker.nextNode()) {
      const el = (next as Text).parentElement;
      const text = (next.textContent ?? "").trim();
      if (!el || !text) continue;
      if (el.tagName === "SCRIPT" || el.tagName === "STYLE") continue;
      if (!rendered(el)) continue;
      accessible.push(text);
      if (!srOnly(el)) visual.push(text);
    }
    for (const el of Array.from(document.querySelectorAll("[aria-label], [title], [alt]"))) {
      if (!rendered(el)) continue;
      for (const name of ["aria-label", "title", "alt"]) {
        const value = (el.getAttribute(name) ?? "").trim();
        if (value) accessible.push(value);
      }
    }
    return { visual: visual.join(" "), accessible: accessible.join(" "), visualLeaves: visual.length };
  });
}

/**
 * A surface painted with a fade reads `opacity: 0` at first paint, which is how a real hint was once
 * reported as invisible.  Wait for the element to be painted **and** settled before judging it, and
 * bound the wait so an absent element fails in seconds rather than at the test timeout.
 */
export async function settledBox(page: Page, selector: string, timeout = 20_000) {
  await page.waitForFunction(
    (target: string) => {
      const el = document.querySelector(target);
      if (!el) return false;
      const style = getComputedStyle(el);
      const box = el.getBoundingClientRect();
      return (
        Number(style.opacity) === 1 &&
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        box.width > 0 &&
        box.height > 0
      );
    },
    selector,
    { timeout },
  );
  return await page.locator(selector).boundingBox();
}
