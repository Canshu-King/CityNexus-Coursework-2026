/**
 * ORD-0349 — two legs, two sentences, two pictures.
 *
 * HJ-P3 measured that a leg which never connected and a leg which answered with something unusable
 * produced pixel-identical screenshots while the screen said "the server answered just now" over both.
 * This spec keeps the two apart where it can be measured: the dictionary strings, the leg kinds the
 * shipped classifier gives them, and the rendered pixels of each sentence -- with a same-sentence
 * control so the pixel comparison is shown to be capable of equality before it is trusted for
 * difference.
 *
 * It does not need a backend: the two sentences are read from the product's own dictionary through
 * `useT`, and the alert is the real `JourneyTrialSetup` mounted with them.
 */
import { expect, test } from "@playwright/test";
import { createHash } from "node:crypto";
import { writeFileSync } from "node:fs";
import { classifyReadFailure, OriginResponseUnusableError } from "../src/lib/failureAttribution";
import { ReadDeadlineMissed } from "../src/lib/readDeadline";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0349-two-legs-two-sentences.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

/** Verbatim from the shipped gate this order must not redden:
 *  `e2e/qoder-sealed-failure-speaks-and-offers-retry.spec.ts`. */
const GATE_ANSWERED_SENTENCE =
  "没读到封存演示的信息：服务器刚才答了，但答的内容我们用不了。这不是你填错了什么。";

/** What each leg may and may not say.  "刚才答了" is an assertion that an answer arrived; "没有拿到
 *  服务器的回答" is the opposite assertion.  Neither may appear in the other's sentence. */
const CLAIMS_ANSWERED = ["刚才答了", "the server answered"];
const CLAIMS_NO_ANSWER = ["没有拿到服务器的回答", "got no answer from the server"];
const CLAIMS_ANSWERED_EN = ["the server answered", "answered just now"];
/** Verbatim from the same gate's English arm (`:29-30`). */
const GATE_ANSWERED_SENTENCE_EN =
  "Couldn't read the sealed demo: the server answered just now, but what it answered isn't something we can use. Nothing you filled in caused this.";

/** Only the alert box, not the whole wizard: the surrounding form pre-fills dates from "now", and two
 *  mounts a few milliseconds apart would then differ for a reason that has nothing to do with the
 *  sentence under test -- which is exactly the false difference a control arm exists to catch.
 *
 * r3: the shot no longer asks Playwright to keep a moving element still, and it no longer moves the
 * layout itself.  On the current line with the backend up this arm hung on "element is not stable"
 * until the test timeout.  The mover is named by measurement, not guess: this instrument's own
 * blanket `position: static` override grows the wizard wrapper by a few hundred pixels every frame
 * (see the reading inside shotAlert), because ORD-0338 ruling 甲 made the CTA a grid row and
 * left `.cn-onboarding__scroll` as the only scroller.  With the override removed the document height
 * is constant, so stability is now checked on my own bounded predicate: pin the document and every
 * scrollable ancestor of the alert to 0, require the box to repeat identically across a pair of
 * animation frames, retry up to 20 times, and crop in page coordinates rather than scrolling the
 * element into view.  The two claims are untouched: two sentences still have to give two pictures,
 * one sentence repeated still has to give one picture, and each leg is still shot from its own
 * mount. */
const ALERT_SELECTOR = '[data-testid="trial-sealed-unavailable"]';

type BoxReading = { x: number; y: number; width: number; height: number };

type AlertGeometry = {
  /** viewport-relative, which is what `page.screenshot({ clip })` takes without `fullPage`. */
  clip: BoxReading;
  /** document-relative, recorded so a reader can see where the shot was taken from. */
  page: BoxReading;
  scroller: { cls: string; scrollTop: number; clientHeight: number; scrollHeight: number } | null;
  withinScroller: boolean;
  withinViewport: boolean;
};

/** Put the alert at the top of whatever actually scrolls it, then read where it landed.
 *
 * "scrollTop = 0" is not the deterministic state on this line: after ORD-0338 ruling 甲 the wizard's
 * own content is taller than `.cn-onboarding__scroll`, so at 0 the alert can sit below the
 * container's client box and the browser clips it -- a clip position that depends on how much copy
 * the sentence carries, which is precisely the thing under test.  Aligning the alert's top to the
 * scroller's top names a state that is the same for both legs. */
async function alignAndRead(page: import("@playwright/test").Page): Promise<AlertGeometry | null> {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel) as HTMLElement | null;
    if (!el) return null;
    let scroller: HTMLElement | null = el.parentElement;
    while (scroller && scroller.scrollHeight <= scroller.clientHeight) scroller = scroller.parentElement;
    for (let i = 0; i < 2; i += 1) {
      // once through the scroller, once through the window: each shift changes the other's offset.
      if (scroller) {
        const s = scroller.getBoundingClientRect();
        scroller.scrollTop += el.getBoundingClientRect().top - s.top;
      }
      window.scrollTo(0, window.scrollY + el.getBoundingClientRect().top);
    }
    const b = el.getBoundingClientRect();
    const s = scroller ? scroller.getBoundingClientRect() : null;
    return {
      clip: { x: b.x, y: b.y, width: b.width, height: b.height },
      page: { x: b.x + window.scrollX, y: b.y + window.scrollY, width: b.width, height: b.height },
      scroller: scroller
        ? {
            cls: scroller.className,
            scrollTop: scroller.scrollTop,
            clientHeight: scroller.clientHeight,
            scrollHeight: scroller.scrollHeight,
          }
        : null,
      withinScroller: s === null ? true : b.top >= s.top - 0.5 && b.bottom <= s.bottom + 0.5,
      withinViewport: b.top >= -0.5 && b.bottom <= window.innerHeight + 0.5,
    };
  }, ALERT_SELECTOR);
}

async function shotAlert(page: import("@playwright/test").Page, caseName: string): Promise<{ hash: string; geometry: AlertGeometry }> {
  /* r3: the blanket `position: static !important` this instrument used to inject on every load is
     gone, and it is the reason the arm went red on the current line.  Measured on a backend-up page,
     12 consecutive animation frames, node count and height of the document:
       without the override  h = 1837 / 1835 / 1834 / 1869 / 1869 / 1830 ...  nodes 344 -> 362 (settles)
       with the override   h = 1499 -> 1910 -> 2177 -> 2489 -> 3113 -> 3425 -> 5883, nodes 361 -> 363
     No nodes were being appended -- the same wizard wrapper grew every frame, because forcing
     `position: static` dissolves the containment ORD-0338 ruling 甲 relies on (the CTA is a grid row
     and `.cn-onboarding__scroll` is the only scroller).  A layout the instrument itself keeps
     changing cannot support any stability predicate, Playwright's or mine, which is what the
     "element is not stable" loop was measuring.
     What the override was for -- a fixed band landing in one shot and not another -- is now answered
     by the control arm instead of by an assertion of faith: the same sentence, mounted twice, must
     still hash identically.  If a band ever starts overlapping, that control goes red and the
     difference arm loses its authority. */

  /* Stability on MY predicate, with a ceiling: two aligned readings of the same box across a pair of
     animation frames.  If it never repeats, the arm says so instead of quietly shooting whatever
     frame happened to be first, and it says so in well under the test timeout rather than after it. */
  let settled: AlertGeometry | null = null;
  for (let attempt = 0; attempt < 20 && !settled; attempt += 1) {
    const first = await alignAndRead(page);
    await page.evaluate(() => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    }));
    const second = await alignAndRead(page);
    if (first && second && JSON.stringify(first.clip) === JSON.stringify(second.clip)) settled = second;
    else await page.waitForTimeout(100);
  }
  if (!settled) {
    throw new Error(
      `${FILE} :: pixels — the ${caseName} leg's box did not repeat within 20 aligned samples; ` +
        "refusing to shoot a moving element",
    );
  }
  if (!settled.withinScroller || !settled.withinViewport) {
    /* Either bound would let the container's clip rectangle, not the sentence, decide the picture. */
    throw new Error(
      `${FILE} :: pixels — the ${caseName} leg is not fully inside its scroller/viewport; ` +
        JSON.stringify({ scroller: settled.scroller, clip: settled.clip }),
    );
  }
  const buf = await page.screenshot({
    clip: {
      x: Math.round(settled.clip.x),
      y: Math.round(settled.clip.y),
      width: Math.round(settled.clip.width),
      height: Math.round(settled.clip.height),
    },
    animations: "disabled",
    caret: "hide",
    scale: "css",
  });
  return { hash: createHash("sha256").update(buf).digest("hex"), geometry: settled };
}

async function mount(page: import("@playwright/test").Page, locale: "zh-CN" | "en", caseName = "answered") {
  await page.addInitScript((loc) => {
    localStorage.setItem("citynexus.locale", loc);
    localStorage.setItem("citynexus.storage-seeded", "1");
  }, locale);
  await page.goto(`/?case=${caseName}`, { waitUntil: "load" });
  await page.addScriptTag({ type: "module", url: "/e2e/fixtures/ord0349-sealed-alert-harness.tsx" });
  await page.waitForSelector('[data-testid="ord0349-case"]');
  /* A pending webfont would move glyph pixels between the two control shots, which is the difference
     I am trying to detect, not noise to eliminate: settle first, then shoot. */
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(400);
}

test("the dictionary separates the legs, and neither sentence asserts the other", async ({ page }) => {
  await mount(page, "zh-CN");
  const answered = (await page.getByTestId("ord0349-dict-answered").innerText()).trim();
  const noAnswer = (await page.getByTestId("ord0349-dict-no-answer").innerText()).trim();
  writeFileSync(
    evidencePath("ord0349-sentences.json"),
    JSON.stringify({ answered, noAnswer, gateAnswered: GATE_ANSWERED_SENTENCE }, null, 1),
  );

  expect(answered.length, node("sentences", "the answered sentence is empty")).toBeGreaterThan(8);
  expect(noAnswer.length, node("sentences", "the no-answer sentence is empty")).toBeGreaterThan(8);
  expect(answered === noAnswer, node("sentences", "both legs still share one sentence")).toBe(false);

  for (const shape of [...CLAIMS_ANSWERED, ...CLAIMS_ANSWERED_EN]) {
    expect(noAnswer.includes(shape), node("sentences", `the no-answer sentence claims "${shape}"`)).toBe(false);
  }
  for (const shape of CLAIMS_NO_ANSWER) {
    expect(answered.includes(shape), node("sentences", `the answered sentence claims "${shape}"`)).toBe(false);
  }
  /* The leg this order must leave alone: the shipped gate's own fate (a 200 that answers HTML) reads
     the answered sentence, so its assertion still matches the product word for word. */
  expect(answered, node("sentences", "the answered sentence no longer matches the shipped gate")).toBe(
    GATE_ANSWERED_SENTENCE,
  );
});

test("the shipped classifier tells the legs apart the way the sentence choice now assumes", async () => {
  const legs = {
    refused: new TypeError("Failed to fetch"),
    deadline: new ReadDeadlineMissed(12_000),
    answeredHtml200: new OriginResponseUnusableError(200, "text/html; charset=utf-8", "html"),
    answeredEmpty500: Object.assign(new Error("CityNexus API 500"), { status: 500 }) as Error,
  };
  const kinds: Record<string, string> = {};
  for (const [name, error] of Object.entries(legs)) {
    kinds[name] = classifyReadFailure(error as unknown).evidence_kind;
  }
  writeFileSync(evidencePath("ord0349-leg-kinds.json"), JSON.stringify(kinds, null, 1));

  expect(kinds.refused, node("legs", "a refused connection is not a transport fact")).toBe("TRANSPORT");
  expect(kinds.deadline, node("legs", "a deadline that elapsed is reported as an answer")).toBe("TRANSPORT");
  expect(kinds.answeredHtml200, node("legs", "a 200 that answered HTML is not transport")).toBe("HTTP_RESPONSE");
  expect(kinds.answeredEmpty500, node("legs", "a 500 answer is not transport")).toBe("HTTP_RESPONSE");
});

test("the two legs render as two pictures, and the same sentence renders as one", async ({ page }) => {
  await mount(page, "zh-CN", "answered");
  const answered = (await shotAlert(page, "answered")).hash;
  await mount(page, "zh-CN", "no-answer");
  const noAnswer = (await shotAlert(page, "no-answer")).hash;
  /* The control: the same load again.  If this hashes differently, pixel comparison is noise here and
     the arm below has no business claiming a difference. */
  const page2 = await page.context().newPage();
  await mount(page2, "zh-CN", "no-answer");
  const again = (await shotAlert(page2, "no-answer")).hash;
  await page2.close();
  writeFileSync(
    evidencePath("ord0349-pixels.json"),
    JSON.stringify({ answered, noAnswer, noAnswerAgain: again, control_stable: noAnswer === again, separated: answered !== noAnswer }, null, 1),
  );

  /* The control first: if identical content did not hash identically, a difference would be noise. */
  expect(noAnswer === again, node("pixels", "the same sentence did not screenshot identically -- the instrument is unstable")).toBe(true);
  expect(answered !== noAnswer, node("pixels", "the two legs still produce the same pixels")).toBe(true);
});

test("the shot is taken from the alert's own box, not from wherever the page happened to scroll", async ({ page }) => {
  /* Where the two pictures above come from, stated rather than assumed: the alert is aligned to the
     top of the scroller that ruling 甲 left owning the scroll, it is inside that scroller and inside
     the viewport, and both legs land in the same box.  Nothing here compares sentences; it records
     the geometry the pixel arm depends on, so a future change to the wizard shows up as a named
     reading instead of a mystery difference. */
  await mount(page, "zh-CN", "answered");
  const first = await shotAlert(page, "answered");
  await mount(page, "zh-CN", "no-answer");
  const second = await shotAlert(page, "no-answer");
  writeFileSync(
    evidencePath("ord0349-shot-geometry.json"),
    JSON.stringify({ answered: first.geometry, noAnswer: second.geometry }, null, 1),
  );
  expect(first.geometry.scroller?.cls, node("geometry", "no single scroller was named")).toContain("cn-onboarding__scroll");
  expect(first.geometry.withinScroller, node("geometry", "the answered leg was clipped by its scroller")).toBe(true);
  expect(first.geometry.withinViewport, node("geometry", "the answered leg was outside the viewport")).toBe(true);
  expect(second.geometry.withinScroller, node("geometry", "the no-answer leg was clipped by its scroller")).toBe(true);
  expect(second.geometry.withinViewport, node("geometry", "the no-answer leg was outside the viewport")).toBe(true);
  /* What makes the two pictures comparable is that each is the alert's own box in the same column at
     the same measure -- not that the boxes are equal.  Their height is a function of the sentence,
     which is the thing the arm above claims differs, so demanding an identical box here would demand
     the opposite claim. */
  expect(first.geometry.clip.x, node("geometry", "the two legs were shot at different x")).toBe(second.geometry.clip.x);
  expect(first.geometry.clip.width, node("geometry", "the two legs were shot at different widths")).toBe(second.geometry.clip.width);
});

test("the English arm matches the shipped gate's English sentence too", async ({ page }) => {
  await mount(page, "en");
  const answered = (await page.getByTestId("ord0349-dict-answered").innerText()).trim();
  const noAnswer = (await page.getByTestId("ord0349-dict-no-answer").innerText()).trim();
  writeFileSync(evidencePath("ord0349-sentences-en.json"), JSON.stringify({ answered, noAnswer }, null, 1));
  expect(answered, node("sentences-en", "the English answered sentence drifted from the gate")).toBe(GATE_ANSWERED_SENTENCE_EN);
  for (const shape of CLAIMS_NO_ANSWER) {
    expect(answered.includes(shape), node("sentences-en", `English answered sentence claims "${shape}"`)).toBe(false);
  }
  for (const shape of CLAIMS_ANSWERED_EN) {
    expect(noAnswer.includes(shape), node("sentences-en", `English no-answer sentence claims "${shape}"`)).toBe(false);
  }
});

test("ORD-0339's rule still holds on these mounts: no exception words in the DOM", async ({ page }) => {
  await mount(page, "zh-CN");
  const html = await page.evaluate(() => document.body.innerHTML);
  expect(html.includes("data-error-"), node("q71", "a data-error-* attribute came back")).toBe(false);
});
