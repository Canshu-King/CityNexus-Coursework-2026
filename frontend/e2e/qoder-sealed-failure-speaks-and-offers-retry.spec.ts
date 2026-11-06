/**
 * ORD-0273 — the sealed-demo failure speaks from an i18n key and offers a working next step.
 *
 * TEXT: the user sentence must equal the registered `trial.sealedUnreadable` dictionary value; the
 * raw error facts (message, error name, MIME shapes) must never reach the main copy.  The leak shapes
 * are named precisely -- no wide regex.
 *
 * TEXT-EN: the same sentence has to hold in the *other* locale too.  The first cut of this fix put
 * the Chinese sentence into both dictionaries, which is ORD-0240's defect wearing the other coat --
 * an English screen speaking Chinese.  So this node reads the painted copy under `en` as well, and
 * checks the language of each arm with a CJK scan that is proven to fire on the Chinese arm.
 *
 * ACTION: the failure screen carries an executable next step -- 「重试」 bound to the same read.
 * The read is deadline-bounded by ORD-0263's READ_DEADLINE_MS, so a retry cannot open a second
 * unbounded wait (0273 cell 1's single-clock principle).
 *
 * ORD-0341 (Q72: ORD-0339 beats ORD-0273, and D re-derives its own two nodes).  ORD-0339's Q71
 * sweep deleted the `data-error-name` / `data-error-message` carriers these two nodes used to read --
 * measured on `38277c4e…` (run_id=run403138): both nodes failed at exactly that assertion, nothing
 * else.  So the "technical facts stay diagnosable" half no longer belongs to the DOM at all:
 *  - these two nodes now judge the DOM in the *other* direction (Q71): the exception's own words may
 *    appear neither in the visible copy nor in any `data-*` / `aria-*` value on the screen, and the
 *    scan carries its own arm so "no hit" cannot come from a dead scan;
 *  - the diagnosability promise itself moved to `ord0341-telemetry-*` below, which reads the channel
 *    that was always the home for these facts -- `failureAttribution.recordAttribution` mirrors every
 *    record onto `window.citynexusAttribution` (failureAttribution.ts).
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-sealed-failure-speaks-and-offers-retry.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

/** The registered i18n value, held by this gate and never imported from the product file. */
const FROZEN_SENTENCE = "没读到封存演示的信息：服务器刚才答了，但答的内容我们用不了。这不是你填错了什么。";
/** The registered English value, likewise held here -- equality with the disk copy is checked twice. */
const FROZEN_SENTENCE_EN =
  "Couldn't read the sealed demo: the server answered just now, but what it answered isn't something we can use. Nothing you filled in caused this.";
/** The named leak shapes (commander's list): none of these may appear in the main copy. */
const LEAK_SHAPES = ["origin answered", "unusable body", "text/html", "text/plain", "Error", "TypeError"];
const hasCJK = (text: string) => /[\u3400-\u9fff]/.test(text);

/* The shape the commander's 08:40 reproduction installs, named once so the upstream prose below is
   derived from it instead of hand-copied: `OriginResponseUnusableError` renders its message from
   exactly (status, content-type) -- failureAttribution.ts. */
const BROKEN_STATUS = 200;
const BROKEN_CONTENT_TYPE = "text/html; charset=utf-8";
const UPSTREAM_ERROR_NAME = "OriginResponseUnusableError";
const UPSTREAM_MESSAGE = `origin answered ${BROKEN_STATUS} with an unusable body (${BROKEN_CONTENT_TYPE})`;
/**
 * Q71's discriminant for `data-*` / `aria-*` is the exception's OWN WORDS, not every technical token:
 * a measured scalar may legitimately sit on a Q70 anchor under its contract name, while a sentence
 * produced inside an Error object may not be in the DOM in any form -- from there a screen reader, an
 * export or any later render can speak it.
 */
const UPSTREAM_PROSE = [UPSTREAM_MESSAGE, UPSTREAM_ERROR_NAME, "origin answered", "unusable body"];

type AttrValue = { testid: string; attr: string; value: string };

async function openWizardWithBrokenScene(page: Page, locale = "zh-CN") {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length, node("seed", "/api/v1/demo/scene returned no journey_id")).toBeGreaterThan(8);
  await page.addInitScript(
    ([id, loc]) => {
      if (localStorage.getItem("citynexus.storage-seeded") !== "1") {
        localStorage.clear();
        localStorage.setItem("citynexus.storage-seeded", "1");
      }
      localStorage.setItem("citynexus.locale", loc);
      localStorage.removeItem("citynexus.v8.journey_id");
    },
    [journeyId, locale] as const,
  );
  /* The commander's 08:40 shape: the sealed-demo read answers 200 with an HTML document. */
  await page.route("**/api/v1/demo/scene*", async (route) => {
    await route.fulfill({
      status: BROKEN_STATUS,
      contentType: BROKEN_CONTENT_TYPE,
      body: "<!doctype html><html><body>spa fallback</body></html>",
    });
  });
  await page.goto("/v8", { waitUntil: "load" });
  await page.waitForTimeout(1_000);
  return journeyId;
}

/** Every `data-*` / `aria-*` value the rendered screen carries, with the test id of its element. */
async function domAttributeValues(page: Page): Promise<AttrValue[]> {
  return await page.evaluate(() => {
    const out: { testid: string; attr: string; value: string }[] = [];
    for (const el of Array.from(document.querySelectorAll("*"))) {
      const host = el as HTMLElement;
      const testid = host.getAttribute("data-testid") ?? "";
      for (const item of Array.from(host.attributes)) {
        if (item.name.startsWith("data-") || item.name.startsWith("aria-")) {
          out.push({ testid, attr: item.name, value: item.value });
        }
      }
    }
    return out;
  });
}

const proseHits = (values: AttrValue[]) =>
  values.filter((v) => UPSTREAM_PROSE.some((p) => v.value.toLowerCase().includes(p.toLowerCase())));

/** Everything a traveller (or a screen reader reading the tree) can see as text on this screen. */
async function visibleText(page: Page): Promise<string> {
  return await page.evaluate(() => document.body.innerText);
}

const visibleProseHits = (text: string) =>
  UPSTREAM_PROSE.filter((p) => text.toLowerCase().includes(p.toLowerCase()));

/** `recordAttribution`'s mirror -- the non-DOM channel these facts belong on. */
async function attributionRecords(page: Page) {
  return await page.evaluate(
    () =>
      (
        window as unknown as {
          citynexusAttribution?: {
            at?: string;
            context?: string;
            reason?: string;
            http_status?: number | null;
            content_type?: string | null;
            telemetry?: string;
          }[];
        }
      ).citynexusAttribution ?? [],
  );
}

test("ord0273-text-the-sentence-is-the-key-and-no-shape-leaks — 主文案是键值，不漏技术形状", async ({
  page,
}) => {
  test.setTimeout(180_000);
  await openWizardWithBrokenScene(page);
  await page.getByTestId("trial-tianjin-demo").click();

  const alert = page.getByTestId("trial-sealed-unavailable");
  await expect(alert, node("ord0273-text", "the failure alert never rendered")).toBeVisible({
    timeout: 15_000,
  });
  const sentence = (await alert.locator("p").innerText()).replace(/\s+/g, " ").trim();
  expect(
    sentence,
    node("ord0273-text", `the user sentence reads "${sentence}" -- it must equal the i18n value exactly`),
  ).toBe(FROZEN_SENTENCE);
  for (const shape of LEAK_SHAPES) {
    expect(
      sentence.toLowerCase().includes(shape.toLowerCase()),
      node("ord0273-text", `the leak shape "${shape}" reached the main copy`),
    ).toBe(false);
  }

  /* ORD-0341 / Q71, the other direction: the exception's words may not ride in the DOM or on the
     screen at all -- neither as text a traveller reads nor as a `data-*` / `aria-*` value a screen
     reader, an export or a later render could speak.  Scope note for the red team: ORD-0339's own
     gate judges the *alert element* against a shape list; this one sweeps the *whole rendered
     screen* against the words this exception actually produced, so an attribute or a line of copy
     anywhere else on the page is in range here and out of range there.  The arm below is the teeth. */
  const attrs = await domAttributeValues(page);
  const clean = proseHits(attrs);
  const visible = await visibleText(page);
  const cleanVisible = visibleProseHits(visible);
  const injected = await page.evaluate((message) => {
    const probe = document.createElement("div");
    probe.setAttribute("data-testid", "ord0341-probe");
    probe.setAttribute("data-error-message", message);
    probe.setAttribute("aria-label", message);
    probe.textContent = message;
    document.body.appendChild(probe);
    return probe.getAttribute("data-error-message");
  }, UPSTREAM_MESSAGE);
  /* Arm landed?  Read the injected attribute back before trusting the re-scan. */
  expect(
    injected,
    node("ord0341-arm", "the arm did not land -- its attribute read back empty"),
  ).toBe(UPSTREAM_MESSAGE);
  const afterArm = await domAttributeValues(page);
  const armHits = proseHits(afterArm);
  const armVisibleHits = visibleProseHits(await visibleText(page));
  await page.evaluate(() => document.querySelector('[data-testid="ord0341-probe"]')?.remove());
  const afterRemoval = proseHits(await domAttributeValues(page));
  const afterRemovalVisible = visibleProseHits(await visibleText(page));

  writeFileSync(
    evidencePath("ord0273-text-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0273-text",
        run_id: process.env.CITYNEXUS_E2E_RUN_ID ?? "dev",
        viewport: VIEWPORT,
        locale: "zh-CN",
        sentence,
        leakShapesChecked: LEAK_SHAPES,
        upstreamProseChecked: UPSTREAM_PROSE,
        domAttributeCount: attrs.length,
        proseInDomAttributes: clean,
        proseInVisibleText: cleanVisible,
        arm: {
          injected_value: injected,
          hits_with_arm_present: armHits.length,
          visible_hits_with_arm_present: armVisibleHits.length,
          hits_after_arm_removed: afterRemoval.length,
          visible_hits_after_arm_removed: afterRemovalVisible.length,
        },
        /* The carrier this node used to read (data-error-name / data-error-message) is gone by Q71's
           ruling; the facts it held are judged on the telemetry channel by `ord0341-telemetry-*`. */
        alertAttributes: await alert.evaluate((el) =>
          Array.from(el.attributes).map((a) => `${a.name}=${a.value}`),
        ),
      },
      null,
      1,
    ),
  );

  expect(
    clean.length,
    node(
      "ord0273-text",
      `the exception's own words sit in ${clean.length} data-*/aria-* value(s): ` +
        clean.map((c) => `${c.testid || "?"}.${c.attr}="${c.value}"`).join(" | "),
    ),
  ).toBe(0);
  expect(
    cleanVisible,
    node("ord0273-text", `the exception's own words are painted as visible text: ${cleanVisible.join(" | ")}`),
  ).toEqual([]);
  /* Teeth: the same scans must fire when the words really are on the page, or "0 hits" proves nothing. */
  expect(
    armHits.length > 0,
    node("ord0341-arm", "the attribute scan did not fire on the injected exception text -- the scan is dead"),
  ).toBe(true);
  expect(
    armVisibleHits.length > 0,
    node("ord0341-arm", "the visible-text scan did not fire on the injected exception text -- the scan is dead"),
  ).toBe(true);
  expect(
    afterRemoval.length,
    node("ord0341-arm", "removing the arm left attribute hits behind -- the scan counts something else"),
  ).toBe(0);
  expect(
    afterRemovalVisible,
    node("ord0341-arm", "removing the arm left visible hits behind -- the scan counts something else"),
  ).toEqual([]);

  await page.screenshot({
    path: evidencePath(`ui-overnight/ord0273-sealed-failure-${process.env.CITYNEXUS_E2E_RUN_ID ?? "dev"}.png`),
  });
});

test("ord0273-text-en-arm-speaks-english-and-neither-side-leaks — 英文臂不得说中文", async ({
  page,
}) => {
  test.setTimeout(180_000);
  await openWizardWithBrokenScene(page, "en");
  await page.getByTestId("trial-tianjin-demo").click();

  const alert = page.getByTestId("trial-sealed-unavailable");
  await expect(alert, node("ord0273-text-en", "the failure alert never rendered under locale=en")).toBeVisible({
    timeout: 15_000,
  });
  const sentence = (await alert.locator("p").innerText()).replace(/\s+/g, " ").trim();
  expect(
    sentence,
    node(
      "ord0273-text-en",
      `the English screen reads "${sentence}" -- it must equal the registered English value`,
    ),
  ).toBe(FROZEN_SENTENCE_EN);
  /* Two-way control for the language scan: it has to fire on the Chinese arm's own frozen sentence,
     otherwise "the English copy contains no CJK" is a scan that can never fail. */
  expect(
    hasCJK(FROZEN_SENTENCE),
    node("ord0273-text-en", "the CJK scan did not fire on the Chinese constant -- the scan is dead"),
  ).toBe(true);
  expect(
    hasCJK(sentence),
    node("ord0273-text-en", `Chinese copy reached the English screen: "${sentence}"`),
  ).toBe(false);
  for (const shape of LEAK_SHAPES) {
    expect(
      sentence.toLowerCase().includes(shape.toLowerCase()),
      node("ord0273-text-en", `the leak shape "${shape}" reached the English main copy`),
    ).toBe(false);
  }
  /* Same Q71 reverse assertion on the English screen -- the arm is not re-run here, because the scans
     are the two this file defines and the Chinese node proved both of them fire. */
  const attrs = await domAttributeValues(page);
  const clean = proseHits(attrs);
  const cleanVisible = visibleProseHits(await visibleText(page));

  writeFileSync(
    evidencePath("ord0273-text-en-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0273-text-en",
        run_id: process.env.CITYNEXUS_E2E_RUN_ID ?? "dev",
        viewport: VIEWPORT,
        locale: "en",
        sentence,
        cjkInEnglishArm: hasCJK(sentence),
        cjkControlOnChineseConstant: hasCJK(FROZEN_SENTENCE),
        leakShapesChecked: LEAK_SHAPES,
        upstreamProseChecked: UPSTREAM_PROSE,
        domAttributeCount: attrs.length,
        proseInDomAttributes: clean,
        proseInVisibleText: cleanVisible,
        alertAttributes: await alert.evaluate((el) =>
          Array.from(el.attributes).map((a) => `${a.name}=${a.value}`),
        ),
      },
      null,
      1,
    ),
  );

  expect(
    clean.length,
    node(
      "ord0273-text-en",
      `the exception's own words sit in ${clean.length} data-*/aria-* value(s) under locale=en: ` +
        clean.map((c) => `${c.testid || "?"}.${c.attr}="${c.value}"`).join(" | "),
    ),
  ).toBe(0);
  expect(
    cleanVisible,
    node(
      "ord0273-text-en",
      `the exception's own words are painted as visible English-screen text: ${cleanVisible.join(" | ")}`,
    ),
  ).toEqual([]);
});

test("ord0341-telemetry-the-facts-have-a-sink-off-the-dom — 离开 DOM 之后技术事实仍有落点", async ({
  page,
}) => {
  test.setTimeout(180_000);

  /* Read A -- the sealed-demo failure.  Whatever this path leaves on the channel is the whole of what
     a support engineer could recover; the DOM is forbidden to hold it (Q71). */
  const journeyId = await openWizardWithBrokenScene(page);
  await page.getByTestId("trial-tianjin-demo").click();
  const alert = page.getByTestId("trial-sealed-unavailable");
  await expect(alert, node("ord0341-telemetry", "the failure alert never rendered")).toBeVisible({
    timeout: 15_000,
  });
  const afterSealed = await attributionRecords(page);

  /* Read B -- the control, built as close to read A as the product allows: the SAME answer shape
     (200 + `text/html; charset=utf-8`) on a read that does go through `classifyReadFailure` +
     `recordAttribution` -- boot's journey read (V8Experience.tsx).  So this half proves two
     things at once: this gate can see the channel, and the channel carries exactly the fact class
     read A drops (status 200 with the media type the origin sent).  If it came back silent, read A's
     silence would prove nothing about the product -- only that the instrument is blind.
     An init script rather than `setItem` + reload, because the script above clears this very key on
     every navigation and init scripts run in the order they were added -- this one is the last word
     the page gets before boot reads storage. */
  await page.route("**/api/v1/journeys/**", async (route) => {
    await route.fulfill({
      status: BROKEN_STATUS,
      contentType: BROKEN_CONTENT_TYPE,
      body: "<!doctype html><html><body>spa fallback</body></html>",
    });
  });
  await page.addInitScript((id) => {
    localStorage.setItem("citynexus.v8.journey_id", id);
  }, journeyId);
  await page.reload({ waitUntil: "load" });
  await page
    .waitForFunction(() => ((window as unknown as { citynexusAttribution?: unknown[] }).citynexusAttribution ?? []).length > 0, null, {
      timeout: 30_000,
    })
    .catch(() => undefined);
  const afterControl = await attributionRecords(page);

  writeFileSync(
    evidencePath("ord0341-telemetry-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0341-telemetry",
        run_id: process.env.CITYNEXUS_E2E_RUN_ID ?? "dev",
        viewport: VIEWPORT,
        upstream_message_expected: UPSTREAM_MESSAGE,
        after_sealed_demo_failure: afterSealed,
        after_control_boot_read: afterControl,
        control_answer: { status: BROKEN_STATUS, content_type: BROKEN_CONTENT_TYPE },
        control_context: "shell.boot.journeyRead",
        sealed_sink_contexts_required_to_include: "a context naming this read (wizard/sealedDemo)",
      },
      null,
      1,
    ),
  );

  const controlRecord = afterControl.find(
    (r) =>
      (r.context ?? "").includes("shell.boot.journeyRead") &&
      r.reason === "ORIGIN_INVALID_RESPONSE" &&
      r.http_status === BROKEN_STATUS &&
      String(r.content_type ?? "").includes("text/html"),
  );
  expect(
    controlRecord,
    node(
      "ord0341-telemetry",
      `the control read did not put an ORIGIN_INVALID_RESPONSE/200/text-html record on ` +
        `window.citynexusAttribution (${JSON.stringify(afterControl)}) -- the channel itself is not ` +
        "reachable from this gate, so read A says nothing either",
    ),
  ).toBeTruthy();

  /* The claim under test: a technical fact that is no longer allowed in the DOM has to be recorded
     somewhere a diagnosis can be made.  It is not -- `enterSealedDemo`'s catch
     (src/app/V8Experience.tsx) sets `sealedDemoDetail`, which JourneyTrialSetup still accepts
     as a prop (JourneyTrialSetup.tsx) and no longer renders or reads anywhere, and it never calls
     `recordAttribution`.  Red by product truth, not by instrument: the facts have no sink. */
  const sealedRecord = afterSealed.find((r) =>
    /sealed|wizard/i.test(String(r.context ?? "")),
  );
  expect(
    sealedRecord,
    node(
      "ord0341-telemetry",
      `the sealed-demo failure left ${afterSealed.length} record(s) on the attribution channel ` +
        `(contexts seen: ${afterSealed.map((r) => r.context ?? "?").join(", ") || "none"}) -- Q71 took the ` +
        "words out of the DOM and nothing replaced them, so this failure is undiagnosable off the screen. " +
        "Fix site: V8Experience.tsx:894 catch -- recordAttribution(classifyReadFailure(error), " +
        '"wizard.sealedDemo.enter")',
    ),
  ).toBeTruthy();
});

test("ord0273-action-retry-is-a-real-executable-step — 失败屏带着能走的那一步", async ({ page }) => {
  test.setTimeout(180_000);
  await openWizardWithBrokenScene(page);
  await page.getByTestId("trial-tianjin-demo").click();
  const alert = page.getByTestId("trial-sealed-unavailable");
  await expect(alert).toBeVisible({ timeout: 15_000 });

  /* The retry CTA exists, is enabled, and re-runs the SAME read (which still answers the same broken
     body here) -- so the second attempt must land in the same named state, not a second unbounded
     wait: the single-clock principle holds on the retry path too. */
  const retry = page.getByTestId("trial-sealed-retry");
  await expect(retry, node("ord0273-action", "the retry CTA is missing")).toBeVisible({ timeout: 10_000 });
  await expect(retry).toBeEnabled();
  const secondAttempt = page.waitForTimeout(14_000).then(() => "settled");
  await retry.click();
  await expect(alert).toBeVisible({ timeout: 16_000 });
  const still = (await alert.locator("p").innerText()).replace(/\s+/g, " ").trim();
  expect(
    still,
    node("ord0273-action", `after the retry the screen reads "${still}" -- the named state must return, not hang`),
  ).toBe(FROZEN_SENTENCE);
  await secondAttempt;

  writeFileSync(
    evidencePath("ord0273-action-reading.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: "ord0273-action",
        retryLabel: (await retry.innerText()).trim(),
        afterRetrySentence: still,
        note: "retry re-enters the same deadline-bounded read; the named failure state returns, no second clock",
      },
      null,
      1,
    ),
  );
});
