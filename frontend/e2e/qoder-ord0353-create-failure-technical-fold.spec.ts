/**
 * ORD-0353 -- the create-failure screen shows a recitable number, or says by name that there is none.
 *
 * HJ-P3 measured this screen with `details_blocks_on_the_failure_screen` = 0 while the shell already
 * carried a receipt the server had issued, so the number existed and had nowhere to be read aloud.
 * This spec walks the product's own wizard to a refused create and reads four shapes off the DOM:
 * a shaped receipt, no receipt key at all, a value that is not the contract shape, and a leg that
 * never produced anything to measure.  Every reading is taken from the element carrying it, and the
 * upstream sentence is planted as a sentinel so its absence is a measurement rather than a hope.
 *
 * Nothing here reuses ORD-0349's pixel baseline: this order's control is the attribute set and the
 * rendered text, both read from the failure screen itself.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0353-create-failure-technical-fold.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const DRAFT_KEY = "citynexus.v8.trial_draft";

/** Plant the upstream sentence in the body the screen reads: if any of it reaches the DOM, Q64/Q71
 *  is broken wherever it landed, including inside a data-* attribute.
 *
 *  The token is deliberately short and sits at the head of the value.  The first version of this
 *  gate searched for the whole sentence, and a mutation that copied only the leading 40 characters
 *  of the body into `data-body-head` walked through it green -- a canary long enough to be truncated
 *  is not a canary. */
const SENTINEL_TOKEN = "SENT-9F2K-4227";
const SENTINEL_TAIL = "stay window overlaps an existing commitment, upstream wording";
const SENTINEL = `${SENTINEL_TOKEN} ${SENTINEL_TAIL}`;
/** Q71 read as structure rather than as prose: these are the only carriers this fold may wear.  A
 *  new attribute is a red even when its value happens to look clean, because "clean today" is not
 *  a property of an attribute name the contract never named.  Only `data-*` / `aria-*` are judged --
 *  `class` and the `<details>` element's own `open` state are not evidence carriers. */
const ALLOWED_FOLD_CARRIERS = [
  "data-cnx-field",
  "data-cnx-receipt",
  "data-cnx-receipt-present",
  "data-cnx-value",
  "data-raw-bytes",
  "data-raw-content-type",
  "data-raw-http-status",
  "data-testid",
];
/** The contract shape (contracts/ERROR_RECEIPT_ID_V1.md): CNX- plus eight Crockford-alphabet chars. */
const RECEIPT = "CNX-7K3M9QTB";
const RECEIPT_TOKEN = /\bCNX-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{8}\b/;

const DRAFT = {
  cityName: "天津",
  arrivalLocation: "天津站",
  departureLocation: "天津滨海国际机场",
  interests: [],
  specialRequirements: "",
};

async function openReviewStep(page: Page) {
  await page.addInitScript(
    ([key, seed]) => {
      localStorage.clear();
      localStorage.setItem("citynexus.locale", "zh-CN");
      localStorage.removeItem("citynexus.v8.journey_id");
      localStorage.setItem(key, JSON.stringify({ step: 3, draft: seed }));
    },
    [DRAFT_KEY, DRAFT] as const,
  );
  await page.goto("/v8/hub", { waitUntil: "load" });
  await page.waitForSelector('[data-testid="trial-step3-body"]', { timeout: 30_000 });
}

/** The travelers leg always succeeds here: the refusal under test is the journey create. */
async function letTravelersThrough(page: Page) {
  await page.route("**/api/v1/travelers", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ id: "trv-ord0353" }),
    });
  });
}

async function refuseCreate(page: Page, body: string, contentType = "application/json; charset=utf-8") {
  await page.route("**/api/v1/journeys", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await route.fulfill({ status: 422, contentType, body });
  });
}

/** Press the create button and wait for this screen's own failure panel. */
async function failTheCreate(page: Page) {
  const create = page.getByTestId("trial-create");
  await expect(
    create,
    node("precondition", "the review step blocked the tap, so no create was ever attempted"),
  ).not.toHaveAttribute("aria-disabled", "true", { timeout: 5_000 });
  await create.click();
  await expect(
    page.getByTestId("trial-create-error"),
    node("failure-panel", "the create was refused but no failure panel appeared"),
  ).toBeVisible({ timeout: 20_000 });
}

/** Read every Q64 anchor on the screen, carrying element included. */
const anchorsOnScreen = (page: Page) =>
  page.evaluate(() =>
    [...document.querySelectorAll("[data-cnx-field]")].map((el) => ({
      field: el.getAttribute("data-cnx-field"),
      value: el.getAttribute("data-cnx-value"),
      text: (el as HTMLElement).innerText.replace(/\s+/g, " ").trim(),
    })),
  );

const foldFields = (page: Page) =>
  page.locator('[data-testid="trial-create-error"] details [data-cnx-field="http_status"], [data-testid="trial-create-error"] details [data-cnx-field="content_type"], [data-testid="trial-create-error"] details [data-cnx-field="body_bytes"]');

/** Every attribute name worn inside the fold, this element and all of its descendants. */
const foldAttrNames = (page: Page) =>
  page.locator('[data-testid="trial-create-error"] details').evaluate((root) => {
    const names = new Set<string>();
    for (const el of [root, ...Array.from(root.querySelectorAll("*"))]) {
      for (const attr of Array.from(el.attributes)) names.add(attr.name);
    }
    return [...names].sort();
  });

const readings: Record<string, unknown> = {};

test("the fold is on the failure screen, closed, and holds nothing but measurements", async ({ page }) => {
  const body = JSON.stringify({ detail: SENTINEL, error_receipt_id: RECEIPT });
  await letTravelersThrough(page);
  await refuseCreate(page, body);
  await openReviewStep(page);
  await failTheCreate(page);

  const fold = page.getByTestId("create-technical-details");
  await expect(
    fold,
    node("fold-present", "HJ counted 0 details blocks here; this order exists to make it one"),
  ).toHaveCount(1);
  /* Q76: the fold stays closed. A closed <details> is not on screen, so its contents must not be
     readable as travellers' words until the traveller asks for them. */
  await expect(
    page.getByTestId("create-raw-facts"),
    node("fold-closed", "the measured fields were already open on load"),
  ).toBeHidden();

  readings.closed_fold_html = await page.content();
  expect(
    String(readings.closed_fold_html).includes(SENTINEL_TOKEN),
    node("q64-sentinels", "the upstream sentence reached the DOM while the fold was closed"),
  ).toBe(false);

  await page.getByTestId("create-technical-toggle").click();
  await expect(
    page.getByTestId("create-raw-facts"),
    node("fold-opens", "opening the toggle showed no measurement"),
  ).toBeVisible();

  const entries = await foldFields(page).evaluateAll((els) =>
    els.map((el) => ({
      field: el.getAttribute("data-cnx-field"),
      value: el.getAttribute("data-cnx-value"),
    })),
  );
  readings.opened_fields = entries;
  /* Q70: the machine name is the contract, and it is the contract's own spelling -- `body_bytes`,
     not whatever label this screen prints beside the number. */
  expect(
    entries.map((entry) => entry.field).sort(),
    node("q70-anchors", `the fold carries ${JSON.stringify(entries.map((e) => e.field))}`),
  ).toEqual(["body_bytes", "content_type", "http_status"]);

  const byField = Object.fromEntries(entries.map((entry) => [entry.field, entry.value]));
  expect(byField.http_status, node("measured-status", `the fold read ${byField.http_status}`)).toBe("422");
  expect(
    byField.content_type,
    node("measured-content-type", `the fold read ${byField.content_type}`),
  ).toBe("application/json; charset=utf-8");
  /* The byte count is the one number here a test can compute independently: what this stub sent, in
     UTF-8, is what the screen must claim it measured. */
  const sentBytes = new TextEncoder().encode(body).length;
  expect(
    byField.body_bytes,
    node("measured-bytes", `screen says ${byField.body_bytes}, this stub sent ${sentBytes}`),
  ).toBe(String(sentBytes));

  const receipt = page.getByTestId("create-raw-facts-receipt");
  const receiptText = (await receipt.innerText()).replace(/\s+/g, " ").trim();
  const receiptAttrs = await receipt.evaluate((el) => ({
    field: el.getAttribute("data-cnx-field"),
    receipt: el.getAttribute("data-cnx-receipt"),
    present: el.getAttribute("data-cnx-receipt-present"),
  }));
  readings.receipt_present = { text: receiptText, attrs: receiptAttrs, sent_bytes: sentBytes };
  expect(
    receiptText.includes(RECEIPT),
    node("receipt-on-screen", `the line reads "${receiptText}"`),
  ).toBe(true);
  expect(
    receiptText.startsWith("服务器回执号"),
    node("receipt-attribution", `the line attributes the number wrongly: "${receiptText}"`),
  ).toBe(true);
  expect(receiptAttrs.field, node("receipt-anchor", `data-cnx-field=${receiptAttrs.field}`)).toBe(
    "error_receipt_id",
  );
  expect(receiptAttrs.receipt, node("receipt-value", `data-cnx-receipt=${receiptAttrs.receipt}`)).toBe(
    RECEIPT,
  );
  expect(receiptAttrs.present, node("receipt-flag", `present=${receiptAttrs.present}`)).toBe("true");

  const sentence = (await page.getByTestId("trial-create-error").innerText()).replace(/\s+/g, " ");
  expect(
    sentence.includes("这一步没有成功，行程还没有创建"),
    node("own-words", `the panel's own sentence is missing: "${sentence}"`),
  ).toBe(true);
  /* The number lives in the fold, not in the sentence above it: the traveller's line stays this
     screen's words (Q67/Q75) and the recitable value stays attributed to the server. */
  expect(
    RECEIPT_TOKEN.test(sentence.split("技术详情")[0]),
    node("number-placement", `the receipt appeared in the traveller's sentence: "${sentence}"`),
  ).toBe(false);

  const html = await page.content();
  expect(
    html.includes(SENTINEL_TOKEN),
    node("q71-open", "with the fold open the upstream wording is still in the DOM"),
  ).toBe(false);
  /* The same rule as a shape: nothing outside the allow-list may be worn inside this fold, so a
     carrier that forwards the answer's text is caught on the name, not on a guess about its value. */
  const worn = (await foldAttrNames(page)).filter(
    (name) => name.startsWith("data-") || name.startsWith("aria-"),
  );
  readings.fold_attributes = worn;
  expect(
    worn.filter((name) => !ALLOWED_FOLD_CARRIERS.includes(name)),
    node(
      "fold-carriers",
      `the fold wears ${JSON.stringify(worn.filter((n) => !ALLOWED_FOLD_CARRIERS.includes(n)))} outside the allow-list`,
    ),
  ).toEqual([]);
});

test("no receipt key: the line names its own absence and no number appears", async ({ page }) => {
  const body = JSON.stringify({ detail: SENTINEL });
  await letTravelersThrough(page);
  await refuseCreate(page, body);
  await openReviewStep(page);
  await failTheCreate(page);
  await page.getByTestId("create-technical-toggle").click();

  const receipt = page.getByTestId("create-raw-facts-receipt");
  await expect(receipt, node("bare-receipt-line", "the fold opened with no receipt line at all")).toBeVisible();
  const text = (await receipt.innerText()).replace(/\s+/g, " ").trim();
  const attrs = await receipt.evaluate((el) => ({
    field: el.getAttribute("data-cnx-field"),
    receipt: el.getAttribute("data-cnx-receipt"),
    present: el.getAttribute("data-cnx-receipt-present"),
  }));
  readings.receipt_absent = { text, attrs };
  expect(text, node("bare-named", `the absence is not named: "${text}"`)).toBe("服务器未给回执号：NOT-EMITTED");
  expect(attrs.receipt, node("bare-attr", `data-cnx-receipt=${attrs.receipt}`)).toBe("NOT-EMITTED");
  expect(attrs.present, node("bare-flag", `present=${attrs.present}`)).toBe("false");
  /* Q68: a slot labelled 编号 holds only a number the server sent. With none sent, the whole DOM
     must be free of anything shaped like one -- a client-written stand-in would pass this. */
  const html = await page.content();
  expect(
    RECEIPT_TOKEN.test(html),
    node("no-invented-number", "a receipt-shaped token appeared although the body carried none"),
  ).toBe(false);
});

test("a value that is not the contract shape is the same absence", async ({ page }) => {
  /* "CNX-00000000" is not a receipt: 0 is outside the alphabet the contract fixes. A screen that
     printed what it was handed would recite a number no server ever issued. */
  const body = JSON.stringify({ detail: SENTINEL, error_receipt_id: "CNX-00000000" });
  await letTravelersThrough(page);
  await refuseCreate(page, body);
  await openReviewStep(page);
  await failTheCreate(page);
  await page.getByTestId("create-technical-toggle").click();

  const receipt = page.getByTestId("create-raw-facts-receipt");
  const text = (await receipt.innerText()).replace(/\s+/g, " ").trim();
  readings.receipt_wrong_shape = { text, sent_value: "CNX-00000000" };
  expect(text, node("shape-rejected", `the malformed value was recited: "${text}"`)).toBe(
    "服务器未给回执号：NOT-EMITTED",
  );
  const html = await page.content();
  expect(
    html.includes("CNX-00000000"),
    node("shape-not-forwarded", "the malformed value was passed through into the DOM"),
  ).toBe(false);
});

test("a leg that answered with nothing to measure says so, without naming a field", async ({ page }) => {
  /* The travelers die in transit, so the shell holds no reading at all: the fold must still exist
     (the failure screen keeps its one shape) and must state that this device measured nothing. */
  await page.route("**/api/v1/travelers", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await route.abort("connectionrefused");
  });
  await openReviewStep(page);
  await failTheCreate(page);
  await page.getByTestId("create-technical-toggle").click();

  const none = page.getByTestId("create-technical-none");
  await expect(none, node("none-line", "with nothing measured the fold printed an empty block")).toBeVisible();
  await expect(
    page.getByTestId("create-raw-facts"),
    node("no-panel-when-no-facts", "a fields panel appeared without a measurement behind it"),
  ).toHaveCount(0);
  const bare = await none.evaluate((el) => ({
    text: (el as HTMLElement).innerText.replace(/\s+/g, " ").trim(),
    attributes: [...el.attributes]
      .map((a) => a.name)
      .filter((name) => name.startsWith("data-cnx")),
  }));
  readings.no_measurement_shape = bare;
  /* "nothing measured" is not "response_received = false": that would be a machine reading this
     screen never took, so the line carries prose and no contract anchor. */
  expect(
    bare.attributes,
    node("none-line-claim", `the absence line claimed fields it did not measure: ${JSON.stringify(bare.attributes)}`),
  ).toEqual([]);

  const onScreen = await anchorsOnScreen(page);
  readings.anchors_without_facts = onScreen;
});

test("the fold is the wizard's own, and every anchor on the failure screen is a measurement", async ({ page }) => {
  const body = JSON.stringify({ detail: SENTINEL, error_receipt_id: RECEIPT });
  await letTravelersThrough(page);
  await refuseCreate(page, body);
  await openReviewStep(page);
  await failTheCreate(page);
  await page.getByTestId("create-technical-toggle").click();

  const onScreen = await anchorsOnScreen(page);
  readings.anchors_with_facts = onScreen;
  /* Each carrying element names exactly one contract field, so a re-worded label cannot silently
     rename it and a second value cannot ride in beside the first. */
  for (const anchor of onScreen) {
    expect(
      anchor.field,
      node("anchor-named", `an element wore data-cnx-field with no name: ${JSON.stringify(anchor)}`),
    ).not.toBeNull();
  }
  const names = onScreen.map((anchor) => anchor.field);
  const duplicates = names.filter((name, index) => names.indexOf(name) !== index);
  expect(
    duplicates,
    node("one-field-one-carrier", `${JSON.stringify(duplicates)} is anchored twice on this screen`),
  ).toEqual([]);

  writeFileSync(
    evidencePath("ord0353-fold-readings.json"),
    JSON.stringify({ file: FILE, receipt_sent: RECEIPT, sentinel: SENTINEL, readings }, null, 2),
    "utf-8",
  );
});
