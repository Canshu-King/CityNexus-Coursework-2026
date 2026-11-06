import type { Page } from "@playwright/test";

/* Shared mobile-geometry probe for the overnight UI lane.
 *
 * This module deliberately does NOT replace e2e/qoder-mobile-overflow.spec.ts.
 * That spec is a gate the arbiter has already signed, so its file is left
 * byte-for-byte alone and its overflow algorithm is reproduced here verbatim
 * (same display/visibility/opacity filters, same <2px skip, same allow-x and
 * scroller exemptions, same +1px tolerance).  A new surface must never be
 * covered by a weaker rule than the surface it inherits from.
 *
 * Everything measured here is presentation-only.  Nothing reads API payloads,
 * execution truth, cursor state or commitment state: a red result means a box
 * is wrong on a phone, never that a fact is wrong.
 */

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
  top: number;
  right: number;
  bottom: number;
  left: number;
}

export interface Offender {
  selector: string;
  testId: string | null;
  right: number;
  left: number;
  side: "right" | "left";
  scrollWidth: number;
  clientWidth: number;
}

export interface SmallTarget {
  selector: string;
  testId: string | null;
  text: string;
  width: number;
  height: number;
}

export interface TruncatedText {
  selector: string;
  testId: string | null;
  text: string;
  scrollWidth: number;
  clientWidth: number;
}

export interface ChromeOverlap {
  a: string;
  b: string;
  overlapPx: number;
}

export interface CtaReport {
  selector: string;
  text: string;
  rect: Rect;
  innerWidth: number;
  innerHeight: number;
  offscreenTop: boolean;
  offscreenBottom: boolean;
  offscreenRight: boolean;
  coveredByNavPx: number;
  coveredByChromePx: number;
  navPresent: boolean;
  /* Occlusion is only a real finding if the probe could actually move the page.
     Without these, "covered by the nav" and "my scrollIntoView did nothing"
     look identical in the report, and they call for opposite fixes. */
  scrollDiagnostic: {
    topBefore: number;
    topAfter: number;
    scroller: string;
    scrollHeight: number;
    clientHeight: number;
    scrollTopBefore: number;
    scrollTopAfter: number;
    requestedScroll: number;
    scrollBlocked: boolean;
  };
}

export interface CtaCandidateProbe {
  selector: string;
  matches: number;
  firstPainted: boolean;
  display: string;
  visibility: string;
  opacity: string;
  animation: string;
  animationState: string;
  width: number;
  height: number;
}

export interface OnboardingFacts {
  /* Amendment F-1 acceptance, made measurable:
     - an onboarding flow must not also render the app's bottom tab bar;
     - its sticky action must sit inside the viewport;
     - and it must actually reserve the safe area.  On a device without a notch
       env(safe-area-inset-bottom) computes to 0, so the *measured* padding cannot
       distinguish "declared" from "missing" — the declared value is what we assert. */
  bottomNavItemCount: number;
  stickyCtaVisible: boolean;
  stickyCtaBottom: number;
  innerHeight: number;
  stickyCtaDeclaresSafeArea: boolean;
  stickyCtaRuleCount: number;
  stickyCtaPaddingBottomDeclared: string;
  stickyCtaRuleTexts: string[];
}

export interface MobileGeometry {
  innerWidth: number;
  innerHeight: number;
  docScrollWidth: number;
  offenders: Offender[];
  smallTargets: SmallTarget[];
  truncated: TruncatedText[];
  chromeOverlaps: ChromeOverlap[];
  cta: CtaReport | null;
  /* A negative result has to be traceable: "no visible primary CTA" is only
     evidence if the report says what each candidate actually looked like. */
  ctaCandidates: CtaCandidateProbe[];
  onboarding: OnboardingFacts;
}

/** Controls that a thumb has to hit deliberately, on these four screens. */
export const PRIMARY_CONTROL_SELECTOR = [
  ".v8-primary",
  ".v8-primary-button",
  ".v8-outline-button",
  ".v8-rain-button",
  ".v8-mobile-nav button",
  ".v8-mode-chips button",
  ".v8-compare-button",
  ".v8-page-intro button",
  ".trial-chips button",
  ".trial-sticky-cta button",
].join(", ");

const MIN_TOUCH_PX = 44;

export interface ProbeOptions {
  ctaCandidates: string[];
  controlSelector?: string;
  minTouchPx?: number;
}

export async function probeMobileGeometry(
  page: Page,
  options: ProbeOptions,
): Promise<MobileGeometry> {
  return page.evaluate(
    async (opts) => {
      const innerWidth = window.innerWidth;
      const innerHeight = window.innerHeight;
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

      const rectOf = (el: Element): Rect => {
        const r = el.getBoundingClientRect();
        return {
          x: r.x,
          y: r.y,
          width: r.width,
          height: r.height,
          top: r.top,
          right: r.right,
          bottom: r.bottom,
          left: r.left,
        };
      };

      const painted = (el: Element): boolean => {
        const style = getComputedStyle(el);
        if (style.display === "none" || style.visibility === "hidden") return false;
        if (parseFloat(style.opacity) < 0.03) return false;
        const r = el.getBoundingClientRect();
        return r.width >= 2 && r.height >= 2;
      };

      /* ---- 1. horizontal overflow ----
         Verbatim from qoder-mobile-overflow.spec.ts for the right edge, plus the
         left edge it does not check: an element can escape past x=0 exactly as
         easily as past innerWidth, and the journey-creation eyebrow does. */
      const offenders: Offender[] = [];
      const push = (el: HTMLElement, rect: DOMRect, side: "right" | "left") => {
        offenders.push({
          selector: describe(el),
          testId: el.getAttribute("data-testid"),
          right: Math.round(rect.right),
          left: Math.round(rect.left),
          side,
          scrollWidth: el.scrollWidth,
          clientWidth: el.clientWidth,
        });
      };
      for (const el of Array.from(document.querySelectorAll<HTMLElement>("body *"))) {
        const style = getComputedStyle(el);
        if (style.display === "none" || style.visibility === "hidden") continue;
        if (parseFloat(style.opacity) < 0.03) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2) continue;
        const escapesRight = rect.right > innerWidth + 1;
        const escapesLeft = rect.left < -1;
        const inner = el.scrollWidth > el.clientWidth + 1;
        const allowX = !!el.closest("[data-allow-x]");
        const isScroller = /auto|scroll/.test(style.overflowX);
        /* An opt-in horizontal scroller is only a violation when it also blows
           past its own content box — escaping the viewport alone is permitted
           there, which is the whole point of data-allow-x. */
        const guilty = (escapes: boolean) => escapes && (!allowX || inner);
        if (!isScroller) {
          if (guilty(escapesRight)) push(el, rect, "right");
          if (guilty(escapesLeft)) push(el, rect, "left");
        }
      }

      /* ---- 2. touch targets on primary controls ---- */
      const minTouch = opts.minTouchPx ?? 44;
      const smallTargets: SmallTarget[] = [];
      for (const el of Array.from(
        document.querySelectorAll<HTMLElement>(opts.controlSelector ?? ""),
      )) {
        if (!painted(el)) continue;
        /* Box size is scroll-independent, so a control below the fold is judged
           exactly like one above it.  Filtering these to the current viewport
           slice let Live's mode chips and compare button escape the check
           entirely — they only appear after a scroll. */
        if (el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true") continue;
        const rect = el.getBoundingClientRect();
        if (rect.height < minTouch || rect.width < minTouch) {
          smallTargets.push({
            selector: describe(el),
            testId: el.getAttribute("data-testid"),
            text: (el.textContent || "").trim().slice(0, 40),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
          });
        }
      }

      /* ---- 3. truncated text ---- */
      const truncated: TruncatedText[] = [];
      for (const el of Array.from(document.querySelectorAll<HTMLElement>("body *"))) {
        if (!painted(el)) continue;
        const style = getComputedStyle(el);
        const clips = /hidden|clip/.test(style.overflow);
        const ellipsis = style.textOverflow === "ellipsis";
        if (!clips || !ellipsis) continue;
        if (el.scrollWidth > el.clientWidth + 1) {
          truncated.push({
            selector: describe(el),
            testId: el.getAttribute("data-testid"),
            text: (el.textContent || "").trim().slice(0, 48),
            scrollWidth: el.scrollWidth,
            clientWidth: el.clientWidth,
          });
        }
      }

      /* ---- 4. fixed / sticky chrome overlapping each other ---- */
      const chrome: { selector: string; rect: Rect }[] = [];
      for (const el of Array.from(document.querySelectorAll<HTMLElement>("body *"))) {
        const style = getComputedStyle(el);
        if (style.position !== "fixed" && style.position !== "sticky") continue;
        if (!painted(el)) continue;
        const rect = el.getBoundingClientRect();
        // Ignore thin sticky separators and full-page backdrops.
        if (rect.width < innerWidth * 0.25 && rect.height < 40) continue;
        if (rect.height > innerHeight * 0.92 && rect.width > innerWidth * 0.92) continue;
        chrome.push({ selector: describe(el), rect });
      }
      const chromeOverlaps: ChromeOverlap[] = [];
      for (let i = 0; i < chrome.length; i += 1) {
        for (let j = i + 1; j < chrome.length; j += 1) {
          const a = chrome[i];
          const b = chrome[j];
          const dy = Math.min(a.rect.bottom, b.rect.bottom) - Math.max(a.rect.top, b.rect.top);
          const dx = Math.min(a.rect.right, b.rect.right) - Math.max(a.rect.left, b.rect.left);
          if (dy > 1 && dx > 1) {
            chromeOverlaps.push({
              a: a.selector,
              b: b.selector,
              overlapPx: Math.round(dy),
            });
          }
        }
      }

      /* ---- 5. the screen's primary action, once scrolled into view ---- */
      let cta: CtaReport | null = null;
      let chosen: HTMLElement | null = null;
      let chosenSelector = "";
      const ctaCandidates: CtaCandidateProbe[] = [];
      for (const candidate of opts.ctaCandidates) {
        const all = Array.from(document.querySelectorAll<HTMLElement>(candidate));
        const found = all.filter((el) => painted(el));
        if (all.length) {
          const probe = all[0];
          const ps = getComputedStyle(probe);
          const pr = probe.getBoundingClientRect();
          ctaCandidates.push({
            selector: candidate,
            matches: all.length,
            firstPainted: found.length > 0,
            display: ps.display,
            visibility: ps.visibility,
            opacity: ps.opacity,
            animation: ps.animationName,
            animationState: ps.animationPlayState,
            width: Math.round(pr.width),
            height: Math.round(pr.height),
          });
        } else {
          ctaCandidates.push({
            selector: candidate,
            matches: 0,
            firstPainted: false,
            display: "-",
            visibility: "-",
            opacity: "-",
            animation: "-",
            animationState: "-",
            width: 0,
            height: 0,
          });
        }
        if (found.length && !chosen) {
          chosen = found[0];
          chosenSelector = candidate;
        }
      }
      if (chosen) {
        const topBefore = Math.round(chosen.getBoundingClientRect().top);
        const docScroll = document.scrollingElement ?? document.documentElement;
        const scrollTopBefore = docScroll.scrollTop;
        const maxScroll = docScroll.scrollHeight - docScroll.clientHeight;
        /* Centre the control by moving the document ourselves.  scrollIntoView
           reported scrollTop 0->0 with 1031px available, so a helper that only
           asks nicely cannot be trusted to produce an occlusion verdict. */
        const requestedScroll = Math.max(
          0,
          Math.min(
            maxScroll,
            scrollTopBefore + topBefore - (innerHeight - chosen.getBoundingClientRect().height) / 2,
          ),
        );
        docScroll.scrollTop = requestedScroll;
        window.scrollTo(0, requestedScroll);
        await new Promise<void>((done) => {
          requestAnimationFrame(() => requestAnimationFrame(() => done()));
        });
        await new Promise<void>((done) => setTimeout(done, 60));
        const rect = rectOf(chosen);
        const scrollTopAfter = docScroll.scrollTop;
        const scrollDiagnostic = {
          topBefore,
          topAfter: Math.round(rect.top),
          scroller: describe(docScroll),
          scrollHeight: docScroll.scrollHeight,
          clientHeight: docScroll.clientHeight,
          scrollTopBefore,
          scrollTopAfter,
          requestedScroll,
          /* The page fought the scroll back: only report an occlusion verdict
             when the scroll actually took effect. */
          scrollBlocked:
            requestedScroll - scrollTopAfter > 4 &&
            Math.abs(scrollTopAfter - scrollTopBefore) < 4,
        };
        const nav = document.querySelector<HTMLElement>(".v8-mobile-nav");
        const navPainted = nav ? painted(nav) : false;
        const coveredBy = (other: Element | null): number => {
          if (!other) return 0;
          const o = other.getBoundingClientRect();
          const dy = Math.min(rect.bottom, o.bottom) - Math.max(rect.top, o.top);
          const dx = Math.min(rect.right, o.right) - Math.max(rect.left, o.left);
          return dy > 0 && dx > 0 ? Math.round(dy) : 0;
        };
        let coveredChrome = 0;
        for (const el of Array.from(document.querySelectorAll<HTMLElement>("body *"))) {
          const style = getComputedStyle(el);
          if (style.position !== "fixed" && style.position !== "sticky") continue;
          if (el === chosen || el.contains(chosen) || chosen.contains(el)) continue;
          if (!painted(el)) continue;
          coveredChrome = Math.max(coveredChrome, coveredBy(el));
        }
        cta = {
          selector: chosenSelector,
          text: (chosen.textContent || "").trim().slice(0, 40),
          rect,
          innerWidth,
          innerHeight,
          offscreenTop: rect.top < -1,
          offscreenBottom: rect.bottom > innerHeight + 1,
          offscreenRight: rect.right > innerWidth + 1,
          coveredByNavPx: navPainted ? coveredBy(nav) : 0,
          coveredByChromePx: coveredChrome,
          navPresent: navPainted,
          scrollDiagnostic,
        };
      }

      /* ---- 6. Amendment F-1 onboarding facts ---- */
      const navButtons = Array.from(
        document.querySelectorAll<HTMLElement>(".v8-mobile-nav button"),
      ).filter((el) => painted(el));
      const stickyBar = document.querySelector<HTMLElement>(".trial-sticky-cta");
      const stickyButton = stickyBar
        ? Array.from(stickyBar.querySelectorAll<HTMLElement>("button")).find((el) => painted(el)) ?? null
        : null;
      const stickyRect = stickyButton ? stickyButton.getBoundingClientRect() : null;
      /* getComputedStyle resolves env() to a used value (0 on a device without a
         notch), so it cannot tell "declared" from "missing".  Read the authored
         declaration out of the stylesheet instead. */
      let declaresSafeArea = false;
      const stickyCtaRules: { paddingBottom: string; cssText: string }[] = [];
      for (const sheet of Array.from(document.styleSheets)) {
        let rules: CSSRuleList | null = null;
        try {
          rules = sheet.cssRules;
        } catch {
          continue;
        }
        const walk = (list: CSSRuleList | CSSRuleList | null): void => {
          if (!list) return;
          for (const rule of Array.from(list)) {
            if ((rule as CSSStyleRule).selectorText?.includes(".trial-sticky-cta")) {
              const declared =
                        (rule as CSSStyleRule).style?.getPropertyValue("padding-bottom") || "";
              stickyCtaRules.push({
                paddingBottom: declared,
                cssText: (rule as CSSStyleRule).cssText ?? "",
              });
              if (/safe-area-inset-bottom/.test(declared)) declaresSafeArea = true;
            }
            const nested = (rule as CSSMediaRule).cssRules;
            if (nested) walk(nested);
          }
        };
        walk(rules);
      }
      /* A shorthand the engine refuses to expose longhand still counts, provided the
         authored text is present — otherwise this check reports a missing declaration
         that is actually a lookup failure. */
      if (!declaresSafeArea) {
        declaresSafeArea = stickyCtaRules.some((r) =>
          /safe-area-inset-bottom/.test(r.cssText),
        );
      }
      const onboarding: OnboardingFacts = {
        bottomNavItemCount: navButtons.length,
        stickyCtaVisible: !!stickyRect && stickyRect.height > 1,
        stickyCtaBottom: stickyRect ? Math.round(stickyRect.bottom) : -1,
        innerHeight,
        stickyCtaDeclaresSafeArea: declaresSafeArea,
        stickyCtaRuleCount: stickyCtaRules.length,
        stickyCtaPaddingBottomDeclared: stickyCtaRules.map((r) => r.paddingBottom).join(" | "),
        stickyCtaRuleTexts: stickyCtaRules.map((r) => r.cssText.slice(0, 220)),
      };

      return {
        innerWidth,
        innerHeight,
        docScrollWidth,
        offenders,
        smallTargets,
        truncated,
        chromeOverlaps,
        cta,
        ctaCandidates,
        onboarding,
      };
    },
    {
      ...options,
      controlSelector: options.controlSelector ?? PRIMARY_CONTROL_SELECTOR,
      minTouchPx: options.minTouchPx ?? MIN_TOUCH_PX,
    },
  );
}

export { MIN_TOUCH_PX };
