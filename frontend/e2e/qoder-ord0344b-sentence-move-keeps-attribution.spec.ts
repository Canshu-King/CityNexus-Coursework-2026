/**
 * ORD-0344-b — moving the server's sentence into the technical-detail fold, witnessed in both
 * directions against the predicates lane D's shipped gate actually runs.
 *
 * The gate is `e2e/qoder-continuation-resume-carries-identity.spec.ts`; its two relevant assertions
 * are: the refusal line's own text (`:273`), the attributed span being on screen and holding the
 * server's words (`:275`-`:283`), and no client-written HTTP number inside the refusal paragraph
 * (`:284`-`:287`).  That gate needs a live backend to run at all, so this spec replays its three
 * predicates against the two DOM shapes -- the one that shipped before this order and the one this
 * order writes -- and asserts what each one does.  Reading the shapes is a measurement; pretending a
 * collapsed fold is invisible to a traveller would be a guess.
 */
import { expect, test } from "@playwright/test";
import { classifyReadFailure, copyFor } from "../src/lib/failureAttribution";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0344b-sentence-move-keeps-attribution.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

/** The server's own sentence as this refusal actually arrives (lane D's gate names the same shape). */
const SERVER_SENTENCE = "another request is still writing this journey";
/** The refusal line, held here exactly as the shipped gate holds it
 *  (`qoder-continuation-resume-carries-identity.spec.ts`, whose value is the dictionary entry keyed
 *  `hub.continuationResumeRefused` in `src/i18n/LocaleContext.tsx`, zh-CN arm) -- a fixture that
 *  invents its own sentence tests nothing real.  The key name is the anchor on purpose: the file's
 *  line numbers move every generation, and an anchor that drifts silently starts describing whatever
 *  line happens to sit there.  Guarded by `qoder-a68b0631-label-anchors-not-line-numbers-r1`. */
const OUR_SENTENCE = "服务器拒绝了这一次尝试。没有产生新东西，上次的记录还在，你可以再试一次。";
const HTTP = 409;

/** The three predicates the shipped gate runs, lifted here so the comparison is its own, not mine. */
async function gatePredicates(page: import("@playwright/test").Page) {
  const outcomeText = (await page.getByTestId("continuation-resume-outcome").innerText())
    .replace(/\s+/g, " ")
    .trim();
  const line = page.getByTestId("continuation-resume-server-reason");
  return {
    refusalSentenceOnTheLine: outcomeText.includes(OUR_SENTENCE),
    serverLineVisible: await line.isVisible().catch(() => false),
    serverLineHoldsItsOwnWords: (await line.innerText().catch(() => "")).includes(SERVER_SENTENCE),
    noClientWrittenHttpStatusInThatParagraph: !/HTTP\s?\d\d\d/.test(outcomeText),
    outcomeText,
  };
}

const BEFORE_SHAPE = `
  <p class="today-continuation-resume-outcome" data-testid="continuation-resume-outcome"
     data-outcome="refused">${OUR_SENTENCE}<span data-testid="continuation-resume-server-reason">
     服务器给出的原因：${SERVER_SENTENCE}</span></p>`;

const AFTER_SHAPE = `
  <p class="today-continuation-resume-outcome" data-testid="continuation-resume-outcome"
     data-outcome="refused">${OUR_SENTENCE}</p>
  <details class="today-continuation-technical" data-testid="continuation-technical-details">
    <summary data-testid="continuation-technical-toggle">技术详情</summary>
    <span data-testid="continuation-resume-server-reason">服务器给出的原因：${SERVER_SENTENCE}</span>
    <span data-testid="continuation-resume-http-status" data-cnx-field="http_status"
          data-cnx-value="${HTTP}">这台设备看到的回读：HTTP ${HTTP}</span>
  </details>`;

async function mount(page: import("@playwright/test").Page, shape: string) {
  await page.goto("about:blank");
  await page.setContent(`<!doctype html><html><body>${shape}</body></html>`);
}

test("the shape that shipped before ORD-0344-b: all three predicates pass (baseline is real)", async ({
  page,
}) => {
  await mount(page, BEFORE_SHAPE);
  const p = await gatePredicates(page);
  writeFileSync(evidencePath("ord0344b-before-shape.json"), JSON.stringify(p, null, 1));
  expect(p.refusalSentenceOnTheLine, node("before", "baseline predicate broken")).toBe(true);
  expect(p.serverLineVisible, node("before", "baseline predicate broken")).toBe(true);
  expect(p.serverLineHoldsItsOwnWords, node("before", "baseline predicate broken")).toBe(true);
  expect(p.noClientWrittenHttpStatusInThatParagraph, node("before", "baseline predicate broken")).toBe(true);
});

test("the shape ORD-0344-b writes: the paragraph is ours, and a closed fold is not on screen", async ({
  page,
}) => {
  await mount(page, AFTER_SHAPE);
  const p = await gatePredicates(page);
  const anchor = await page.getByTestId("continuation-resume-http-status").evaluate((el) => ({
    field: el.getAttribute("data-cnx-field"),
    value: el.getAttribute("data-cnx-value"),
  }));
  writeFileSync(evidencePath("ord0344b-after-closed.json"), JSON.stringify({ ...p, anchor }, null, 1));

  expect(p.refusalSentenceOnTheLine, node("after-closed", "our own line disappeared")).toBe(true);
  expect(p.noClientWrittenHttpStatusInThatParagraph, node("after-closed", "HTTP leaked back into the paragraph")).toBe(true);
  expect(p.outcomeText.includes(SERVER_SENTENCE), node("after-closed", "the server's words are still on the traveller's line")).toBe(false);
  /* Measured, not assumed: this is exactly what lane D's `:275` assertion sees against a collapsed
     fold, and why one toggle click -- not a rewrite -- is what the move costs. */
  expect(p.serverLineVisible, node("after-closed", "a closed fold reported as visible")).toBe(false);
  expect(p.serverLineHoldsItsOwnWords, node("after-closed", "a closed fold reported as readable")).toBe(false);
  expect(anchor.field, node("after-closed", "Q70 anchor name missing")).toBe("http_status");
  expect(anchor.value, node("after-closed", "Q70 anchor value is not the measured number")).toBe(String(HTTP));
});

test("the same shape with the fold opened: the attributed words are intact for a reader", async ({
  page,
}) => {
  await mount(page, AFTER_SHAPE);
  await page.getByTestId("continuation-technical-toggle").click();
  const p = await gatePredicates(page);
  writeFileSync(evidencePath("ord0344b-after-opened.json"), JSON.stringify(p, null, 1));
  expect(p.serverLineVisible, node("after-opened", "opened fold still invisible")).toBe(true);
  expect(p.serverLineHoldsItsOwnWords, node("after-opened", "opened fold lost the server's words")).toBe(true);
  expect(p.noClientWrittenHttpStatusInThatParagraph, node("after-opened", "the status inside the fold bled into the paragraph")).toBe(true);
});

test("the channel the commander named, measured: copyFor's title is not the sentence the gate holds", async () => {
  /* Imported from the product module, so this is the channel's own output rather than my reading
     of it.  It is why the main line keeps the i18n refusal: swapping in copyFor's title here would
     redden `:273`-`:274`, which is the same shipped node the order says not to touch. */
  const attribution = classifyReadFailure(null, { status: HTTP, code: null, message: null });
  const copy = copyFor(attribution, true);
  writeFileSync(
    evidencePath("ord0344b-channel-words.json"),
    JSON.stringify({ status: HTTP, reason: attribution.reason, messageKey: attribution.message_key,
                     title: copy.title, body: copy.body, gateExpected: OUR_SENTENCE,
                     titleEqualsGateExpected: copy.title === OUR_SENTENCE,
                     titleCarriesHttpStatus: /HTTP\s?\d\d\d/.test(copy.title) }, null, 1),
  );
  expect(copy.title === OUR_SENTENCE, node("channel", "copyFor's title now equals the gate's sentence -- re-check the plan")).toBe(false);
  expect(/HTTP\s?\d\d\d/.test(copy.title), node("channel", "the title now carries an HTTP number")).toBe(false);
});
