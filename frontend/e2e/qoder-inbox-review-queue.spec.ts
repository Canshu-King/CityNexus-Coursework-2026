/**
 * ORD-0146 (P0) -- if the screen says a booking needs review, the review has to be somewhere.
 *
 * HJ-P1 walked this on a phone at 390x844 and wrote down what she could press:
 * `审核并保护` (the button that had just produced the sentence), `打开扫描器`, `粘贴确认信息`,
 * `手动填写`, `提取文本草稿`, `提取图片草稿`.  Every one of those is an *intake* control.  The screen
 * had just told her "预订需要审核后才能成为承诺", and there was no review control on the page --
 * so a commitment can never be made.  That is a `HUMAN_BLOCKER`, not a wording nit.
 *
 * The mechanism is now measured in code rather than assumed: `JourneyInboxWorkbench` is the surface
 * that can pass a review (it is the only component that calls `POST .../inbox/{id}/confirm`), and it
 * **never reads `GET .../inbox`** -- its draft panel renders only the draft that the *same mount*
 * created a moment ago.  Import through the structured-JSON aside, or reload the page, and the queue
 * the sentence points at does not exist.  The server, meanwhile, has been answering all along:
 * `_draft_payload` carries `status`, `unresolved_fields` and `confirmed_at`, and `confirm` refuses an
 * unresolved draft with a field-level 409.  So the fix is to show what the server already knows.
 *
 * Three assertions, in the order the traveller meets them:
 *  N1 the review entry is on the page that asked for the review (red today);
 *  N2 passing a review is the server's answer, not the front end's (red today: the panel stamps
 *     `status:"CONFIRMED"` and a client-side `new Date()` without reading anything back);
 *  N3 a "受保护" badge needs the server's own commitment link (projection agreement, ORD-0146 ③).
 *
 * Deliberately *not* asserted: a 退回/reject action.  There is no route for it -- the inbox surface is
 * import / list / text-extract / vision / PATCH / smart-apply / confirm -- and inventing one in the
 * front end is what ⑤ forbids.  It is recorded as a contract ask instead.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-inbox-review-queue.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };

/* The order is a phone criterion (HJ-P1 walked it on a handset), and the `chromium` project carries
   Playwright's 1280x720 desktop viewport by default -- without this the file would be measuring a
   desktop layout while claiming 390x844. */
test.use({ viewport: VIEWPORT, hasTouch: true });

type Draft = {
  id: string;
  status: string;
  unresolved_fields: string[];
  confirmation_ref?: string;
  extracted?: Record<string, unknown>;
};

async function readScene(page: Page): Promise<Record<string, string | null>> {
  const response = await page.request.get("/api/v1/demo/scene");
  return (await response.json()) as Record<string, string | null>;
}

/** Every button the traveller can actually press on the current screen -- P1's denominator. */
async function visibleButtons(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll<HTMLElement>("button, [role=button], a.v8-primary, summary"))
      .filter((el) => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return (
          rect.width > 0 &&
          rect.height > 0 &&
          style.visibility !== "hidden" &&
          style.display !== "none"
        );
      })
      .map((el) => (el.textContent ?? "").trim().replace(/\s+/g, " ").slice(0, 40))
      .filter((text) => text.length > 0),
  );
}

async function openBookings(page: Page, journeyId: string): Promise<void> {
  /* `page.evaluate` before the first navigation runs on `about:blank`, where touching localStorage
     throws SecurityError -- all three nodes died there in ~340ms on the first pass.  Seed it as an
     init script so it lands in the document that is about to exist. */
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.goto("/v8/bookings", { waitUntil: "load" });
  await expect(page.getByTestId("v13-2-memory")).toBeVisible({ timeout: 30_000 });
}

async function inbox(page: Page, journeyId: string): Promise<{ drafts: Draft[] }> {
  const response = await page.request.get(`/api/v1/journeys/${journeyId}/inbox`);
  return (await response.json()) as { drafts: Draft[] };
}

/** Type into the structured-JSON aside and press the button that produced the sentence. */
async function importThroughAside(page: Page, reservation: Record<string, unknown>): Promise<void> {
  await page.getByTestId("bookings-advanced").locator("summary").click();
  await page.getByTestId("bookings-json-input").fill(JSON.stringify(reservation));
  await page.getByRole("button", { name: "审核并保护" }).click();
}

test("ord0146-review-entry-is-on-the-page-that-asked-for-review — 说了要审核，这一屏就得有审核的地方", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0146-review-entry-is-on-the-page-that-asked-for-review";
  const seeded = await readScene(page);
  const journeyId = String(seeded.journey_id ?? "");
  const evidence: Record<string, unknown> = { node: NODE, order: "ORD-0146 ① ②" };

  await openBookings(page, journeyId);
  const reference = `ORD146-${Math.floor(Math.random() * 1e6)
    .toString()
    .padStart(6, "0")}`;
  /* Deliberately missing the fields the server calls critical (origin, destination, both times), so
     it lands as NEEDS_REVIEW -- the exact state that produced "预订需要审核后才能成为承诺。" */
  await importThroughAside(page, {
    reservation_type: "RAIL",
    title: "天津西 → 北京南 高铁",
    confirmation_ref: reference,
  });

  /* The sentence is set after the import round trip, so reading it immediately measures an empty
     div -- that is how the first pass reported `the screen says ""` and would have made every wording
     assertion below vacuous. */
  const message = page.locator(".v8-message");
  await expect(
    message,
    node(NODE, "the shell printed no message at all after an import the server refused to auto-confirm, so there is no sentence to judge"),
  ).toBeVisible({ timeout: 15_000 });
  const messageText = ((await message.first().textContent()) ?? "").trim();
  evidence.message = messageText;

  const after = await inbox(page, journeyId);
  const pending = after.drafts.filter((item) => item.status !== "CONFIRMED");
  const mine =
    pending.find(
      (item) => String(item.extracted?.confirmation_ref ?? "") === reference,
    ) ?? null;
  evidence.inbox = { total: after.drafts.length, pending: pending.length, mine };
  expect(
    pending.length,
    node(NODE, `the server itself reports no un-reviewed draft after the import (inbox holds ${after.drafts.length}); the premise of this order would be wrong and the gate would be asserting a queue for nothing`),
  ).toBeGreaterThan(0);

  evidence.buttonsBeforeFix = await visibleButtons(page);
  const rows = page.getByTestId("inbox-pending-row");
  const rowCount = await rows.count();
  evidence.queueRows = rowCount;
  expect(
    rowCount,
    node(
      NODE,
      `the screen says "${messageText}" and offers ${JSON.stringify(
        evidence.buttonsBeforeFix,
      )} — every one of those is an intake control. Nothing on this page opens the review the sentence asks for, so the commitment can never be made. GET /inbox already reports ${pending.length} un-reviewed draft(s).`,
    ),
  ).toBeGreaterThan(0);

  /* Once a queue exists it has to name what is missing, in the server's own words. */
  const queueText = rowCount ? (await rows.allInnerTexts()).join(" | ") : "";
  evidence.queueText = queueText;
  const named = (mine?.unresolved_fields ?? []).some((field) =>
    /出发|目的|时间|订单|arrival|departure|origin|destination/i.test(queueText),
  );
  expect(
    named,
    node(
      NODE,
      `the queue exists but does not name the missing fields the server listed (${JSON.stringify(
        mine?.unresolved_fields ?? [],
      )}); the traveller cannot tell what to fix — queue reads "${queueText.slice(0, 200)}"`,
    ),
  ).toBe(true);
  expect(
    /出发|目的|时间|订单|待审/.test(messageText),
    node(
      NODE,
      `the sentence is "${messageText}" while the server listed ${JSON.stringify(
        mine?.unresolved_fields ?? [],
      )} — it orders the traveller to review without naming what is missing or where the review lives`,
    ),
  ).toBe(true);

  /* A queue that goes nowhere is a nicer-looking dead end, so the row's own button is pressed. */
  await rows.first().getByTestId("inbox-open-review").click();
  const opened = page.getByTestId("v10-inbox-draft");
  await expect(
    opened,
    node(NODE, "「打开核对」 did not open a review panel -- the queue is a list with no entrance behind it"),
  ).toBeVisible({ timeout: 10_000 });
  const openedText = (await opened.innerText()).replace(/\s+/g, " ");
  evidence.openedPanel = openedText.slice(0, 240);
  expect(
    reference ? openedText.includes(reference) : true,
    node(
      NODE,
      `the panel opened on something else: it shows "${openedText.slice(0, 160)}" for the row whose reference is ${reference}`,
    ),
  ).toBe(true);

  writeFileSync(
    evidencePath("ui-overnight/ord0146-review-entry.json"),
    JSON.stringify({ generatedBy: "e345b9d6", head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", viewport: [VIEWPORT.width, VIEWPORT.height], ...evidence }, null, 2) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0146-review-entry.png"), fullPage: true });
});

test("ord0146-passing-a-review-is-the-servers-answer — 过没过是服务器说的，不是前端自己盖的章", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0146-passing-a-review-is-the-servers-answer";
  const seeded = await readScene(page);
  const journeyId = String(seeded.journey_id ?? "");
  const evidence: Record<string, unknown> = { node: NODE, order: "ORD-0146 ① 前端不得自判" };

  await openBookings(page, journeyId);
  const reference = `ORD146OK-${Math.floor(Math.random() * 1e6)
    .toString()
    .padStart(6, "0")}`;
  /* The manual tab posts a complete reservation, which the server accepts as READY_FOR_CONFIRMATION.
     It is the one draft in this app that a traveller is allowed to pass. */
  await page.getByTestId("inbox-tab-manual").click();
  await page.getByTestId("manual-title").fill("天津西 → 北京南 高铁");
  await page.getByTestId("manual-ref").fill(reference);
  await page.getByTestId("manual-from").fill("天津西");
  await page.getByTestId("manual-to").fill("北京南");
  await page.getByTestId("manual-depart").fill("2026-10-01T08:30");
  await page.getByTestId("manual-arrive").fill("2026-10-01T09:10");
  await page.getByTestId("manual-amount").fill("54.5");
  await page.getByTestId("manual-submit").click();

  const confirm = page.getByTestId("v13-2-confirm");
  await expect(
    confirm,
    node(NODE, "a complete draft produced no 「确认并保护」 control, so there is no pass action to judge"),
  ).toBeVisible({ timeout: 20_000 });
  await expect(confirm, node(NODE, "the pass button is greyed on a draft the server called READY_FOR_CONFIRMATION")).toBeEnabled();

  /* The gate's invention, labelled as such: the server is made to answer, when the panel reads back
     after a successful pass, that this draft is still `NEEDS_REVIEW`.  A panel that shows the
     server's answer keeps saying 需要补充; a panel that stamps its own conclusion says 已确认 / 受保护
     over a read that contradicts it -- which is what `setDraft({status:"CONFIRMED", confirmed_at:
     new Date().toISOString()})` does today. */
  let heldReads = 0;
  await page.route(`**/api/v1/journeys/${journeyId}/inbox`, async (route) => {
    const real = await route.fetch();
    const body = (await real.json()) as { drafts?: Draft[] };
    const held = (body.drafts ?? []).map((item) =>
      item.status === "CONFIRMED"
        ? { ...item, status: "NEEDS_REVIEW", unresolved_fields: ["scheduled_arrival_at"] }
        : item,
    );
    heldReads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ journey_id: journeyId, drafts: held }),
    });
  });
  await confirm.click();
  await page.waitForTimeout(1_500);
  /* An unfired stub reads green.  A build that never asks the server after a pass is exactly the
     defect N2 is about, so it has to be reported as that, not as a passing assertion. */
  expect(
    heldReads,
    node(
      NODE,
      `the panel never re-read GET /inbox after 「确认并保护」 (stub fired ${heldReads} times) — its verdict is coming from its own state, so there was nothing to contradict`,
    ),
  ).toBeGreaterThan(0);

  const panel = page.getByTestId("v10-inbox-draft");
  /* The whole panel cannot be grepped for 已确认/受保护: the anchor-role select legitimately carries
     the option 「途中 · 受保护活动」, so a panel that was honestly reporting 需审核 still matched.
     A pass can only be claimed by the two elements that state the outcome. */
  const statusLine = ((await panel.locator("> div:first-child b").first().innerText()) ?? "").replace(/\s+/g, " ").trim();
  const messageLine = ((await page.locator(".v10-inbox-message").first().innerText().catch(() => "")) ?? "").replace(/\s+/g, " ").trim();
  evidence.statusLine = statusLine;
  evidence.messageLine = messageLine;
  evidence.panelText = ((await panel.innerText().catch(() => "")) ?? "").replace(/\s+/g, " ").slice(0, 400);
  evidence.buttonsAfterPass = await visibleButtons(page);
  const claimed = `${statusLine} | ${messageLine}`;
  expect(
    /已确认|受保护于|Confirmed/i.test(claimed),
    node(
      NODE,
      `the server's own read still holds this draft as NEEDS_REVIEW, yet the panel states "${claimed}" — a pass minted in the browser instead of read back`,
    ),
  ).toBe(false);
  expect(
    /需审核|NEEDS_REVIEW|需要你补充/i.test(claimed),
    node(
      NODE,
      `the panel neither claims a pass nor carries the server's holding status; it reads "${claimed}" while GET /inbox says NEEDS_REVIEW — the state on screen is not the state the server reported`,
    ),
  ).toBe(true);

  writeFileSync(
    evidencePath("ui-overnight/ord0146-server-verdict.json"),
    JSON.stringify({ generatedBy: "e345b9d6", head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", ...evidence }, null, 2) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0146-server-verdict.png"), fullPage: true });
});

test("ord0146-a-protected-badge-needs-the-servers-commitment — 说「受保护」的每一张卡都得有服务器那条承诺链接", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0146-a-protected-badge-needs-the-servers-commitment";
  const evidence: Record<string, unknown> = { node: NODE, order: "ORD-0146 ③" };
  const seeded = await readScene(page);
  const journeyId = String(seeded.journey_id ?? "");
  await openBookings(page, journeyId);

  /* An assertion over "every card" is vacuous when the page holds no cards, and this fixture starts
     with none.  So the node makes its own protected fact first: a complete structured import is the
     one path the front end does auto-confirm (server says READY_FOR_CONFIRMATION), which yields a
     reservation, a commitment and a memory item for the same object. */
  const reference = `ORD146P-${Math.floor(Math.random() * 1e6).toString().padStart(6, "0")}`;
  await importThroughAside(page, {
    reservation_type: "RAIL",
    title: "天津西 → 北京南 高铁",
    confirmation_ref: reference,
    origin: "天津西",
    destination: "北京南",
    scheduled_departure_at: "2026-10-01T08:30:00+08:00",
    scheduled_arrival_at: "2026-10-01T09:10:00+08:00",
    amount_minor: 5450,
    currency: "CNY",
  });
  await expect
    .poll(
      async () => {
        const body = (await (
          await page.request.get(`/api/v1/journeys/${journeyId}/wallet`)
        ).json()) as { reservations?: Array<{ confirmation_ref: string | null }> };
        return (body.reservations ?? []).some((item) => item.confirmation_ref === reference);
      },
      {
        timeout: 20_000,
        message: node(NODE, "the complete import never produced a reservation, so there is nothing on screen to compare projections of"),
      },
    )
    .toBe(true);
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("v13-2-memory")).toBeVisible({ timeout: 30_000 });

  const wallet = (await (await page.request.get(`/api/v1/journeys/${journeyId}/wallet`)).json()) as {
    reservations?: Array<{ confirmation_ref: string | null; commitment_id: string | null }>;
  };
  const memory = (await (await page.request.get(`/api/v1/journeys/${journeyId}/memory`)).json()) as {
    items?: Array<{ title?: string; label?: string; protected?: boolean }>;
  };
  const cards = page.getByTestId("wallet-reservation");
  /* The wallet is a second query that starts after the reload; counting immediately measured zero
     cards on a page that did have one.  Wait for the row this node created by its own reference. */
  await expect(
    cards.filter({ hasText: reference }),
    node(NODE, `no wallet card for ${reference} appeared after the reload -- either the reservations are not rendered on this surface, or the badge this node is about to judge never reaches the screen`),
  ).toBeVisible({ timeout: 20_000 });
  const count = await cards.count();
  const readings: Array<{ text: string; ref: string | null; commitmentId: string | null | string }> = [];
  for (let index = 0; index < count; index += 1) {
    const text = (await cards.nth(index).innerText()).replace(/\s+/g, " ");
    const row =
      (wallet.reservations ?? []).find((item) =>
        item.confirmation_ref ? text.includes(item.confirmation_ref) : false,
      ) ?? null;
    readings.push({
      text: text.slice(0, 160),
      ref: row?.confirmation_ref ?? null,
      commitmentId: row ? row.commitment_id : "no-matching-wallet-row",
    });
  }
  const claiming = readings.filter((item) => /受保护|protected/i.test(item.text));
  const unbacked = claiming.filter((item) => !item.commitmentId || item.commitmentId === "no-matching-wallet-row");
  const mine = readings.find((item) => item.ref === reference) ?? null;
  const memoryProtected = (memory.items ?? []).filter((item) => item.protected).length;
  /* P4-8's shape is one screen stating two different things about the same read: a headline counter
     and the list under it.  It is not "cards == memory items" -- a reservation is one member of that
     set, not the set -- so the counter is judged against the projection it claims to count. */
  const counterText = (
    await page.getByTestId("v13-2-memory").locator("small").first().innerText()
  ).replace(/\s+/g, " ");
  const counter = Number((counterText.match(/\d+/) ?? ["NaN"])[0]);
  evidence.counter = { printed: counterText, parsed: counter, protectedItems: memoryProtected };
  evidence.mine = mine;

  expect(
    claiming.length,
    node(NODE, `the screen shows ${count} reservation card(s) and none of them claims 受保护, so this node measured nothing — check the card wording before believing the assertions below`),
  ).toBeGreaterThan(0);
  expect(
    unbacked,
    node(
      NODE,
      `${unbacked.length} card(s) read 受保护 while the server links them to no commitment: ${JSON.stringify(unbacked.slice(0, 2))}`,
    ),
  ).toHaveLength(0);
  expect(
    mine && /受保护/.test(mine.text),
    node(NODE, `the reservation the server confirmed (commitment ${JSON.stringify(mine?.commitmentId)}) is not shown as 受保护 on its own card: ${JSON.stringify(mine)}`),
  ).toBe(true);
  expect(
    counter,
    node(
      NODE,
      `the memory panel prints "${counterText}" while the same read lists ${memoryProtected} protected item(s) (${JSON.stringify(
        (memory.items ?? []).slice(0, 3),
      )}) — one screen, two answers about one set`,
    ),
  ).toBe(memoryProtected);

  /* The assertions above can only go one way, because the server happened to link a commitment to
     the row it just made.  So the gate invents the other state -- a reservation with no commitment
     behind it, which is what a draft that never got confirmed looks like in this payload -- and asks
     the card what it says then.  Without this arm no mutant can break this node: an unconditional
     「已确认 · 受保护」 badge would pass everything above. */
  await page.route(`**/api/v1/journeys/${journeyId}/wallet`, async (route) => {
    const real = await route.fetch();
    const body = (await real.json()) as { reservations?: Array<Record<string, unknown>> };
    body.reservations = (body.reservations ?? []).map((item) => ({
      ...item,
      commitment_id: null,
    }));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
  await page.reload({ waitUntil: "load" });
  const heldCards = page.getByTestId("wallet-reservation").filter({ hasText: reference });
  await expect(
    heldCards,
    node(NODE, `the card for ${reference} never rendered against the held wallet read`),
  ).toBeVisible({ timeout: 20_000 });
  const heldText = (await heldCards.first().innerText()).replace(/\s+/g, " ");
  evidence.heldCard = heldText.slice(0, 200);
  expect(
    /受保护|protected/i.test(heldText),
    node(
      NODE,
      `the server links this reservation to no commitment, yet its card still reads "${heldText.slice(0, 120)}" — the badge is asserted by the screen, not earned from the read`,
    ),
  ).toBe(false);

  writeFileSync(
    evidencePath("ui-overnight/ord0146-projection.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        created: reference,
        reservations: wallet.reservations?.length ?? 0,
        cards: count,
        claimingProtected: claiming.length,
        memoryProtected,
        readings,
        notAsserted:
          "退回/reject has no route (inbox offers import|list|text-extract|vision|PATCH|smart-apply|confirm), so the queue ships with 打开核对 + 保存更正 + 确认并保护 and reject stays a contracts ask",
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});
