import { expect, test, type Page } from "@playwright/test";
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { claimFromRead, type GrantRow } from "../src/features/inbox/shareGrantRead";

/**
 * ORD-0447 — the share sentence has to follow what the server's grant doors answer, in both languages,
 * and one revocation has to be enough to change the sentence.
 *
 * Why the first arm predicts instead of assuming an empty roster: the seed hands every spec the same
 * journey, so "no grant yet" is a fact about run order, not about this screen.  The arm therefore
 * asks the door first, derives what the sentence must be from that answer with the same function the
 * component runs, and then checks the screen against it.  A screen that says one thing while the door
 * says another still fails, which is the only failure mode this order is about.
 */
const LOG = process.env.QODER_0447_LOG ?? "reports/qoder-ord0447-screen.log";
mkdirSync(dirname(LOG), { recursive: true });

function note(line: string): void {
  appendFileSync(LOG, `${line}\n`, "utf8");
  console.log(`[ord0447] ${line}`);
}

const API =
  process.env.CITYNEXUS_API_BASE_URL ??
  `http://127.0.0.1:${Number(process.env.CITYNEXUS_BACKEND_PORT ?? 8000)}`;

/* The config's per-test timeout is 420_000 because other specs drive a whole generation.  These arms
   are five taps each, so a shorter ceiling turns an instrument mistake into a fast red instead of a
   seven-minute hang -- which is the shape the first version of `tap()` took. */
test.setTimeout(150_000);

type Grant = { id: string; token: string; visibility: string; expires_at: string | null; revoked_at: string | null };

async function api<T = any>(
  path: string,
  init?: { method?: string; body?: unknown },
): Promise<{ status: number; body: T }> {
  const response = await fetch(`${API}/api/v1${path}`, {
    method: init?.method ?? "GET",
    headers: { "Content-Type": "application/json" },
    body: init?.body === undefined ? undefined : JSON.stringify(init.body),
  });
  return { status: response.status, body: (await response.json()) as T };
}

async function tap(page: Page): Promise<string> {
  const button = page.getByTestId("journey-link-copy").first();
  await expect(button, "the copy entry must be on this screen").toBeVisible();
  await button.scrollIntoViewIfNeeded();
  const box = await button.boundingBox();
  expect(box, "an entry with no box cannot be reached by a thumb").not.toBeNull();
  if (box !== null) {
    const hit = await page.evaluate(
      ({ x, y }) => (document.elementFromPoint(x, y) as HTMLElement | null)?.closest("button")?.dataset.testid ?? null,
      { x: box.x + box.width / 2, y: box.y + box.height / 2 },
    );
    expect(hit, "the centre of the entry must hit the entry itself").toBe("journey-link-copy");
  }
  const slot = page.getByTestId("journey-link-message");
  await button.click();
  /* `disabled` is the tap's own completion signal: the handler clears it only after the clipboard
     write and the grant read have both come back, so reading the slot before it goes enabled measures
     the previous tap's sentence -- that is how the first run of this arm came out "unchanged" after a
     revocation the server had already answered.  Nothing here requires the words to change on every
     tap: two different server states can deserve one sentence, and the must-change judgement belongs
     to the counter-arm, where it is written as `sentence_changed`. */
  await expect(button, "the tap must finish both reads before its sentence is judged").toBeEnabled();
  await expect(slot, "one tap must leave exactly one sentence").toHaveCount(1);
  const text = (await slot.textContent()) ?? "";
  expect(text.trim().length, "a tap that leaves an empty sentence says nothing").toBeGreaterThan(0);
  return text;
}

/** Put the journey in a known authorization state before an arm speaks: every standing grant is
 *  revoked through the product's own door.  The seed hands one journey to every spec, so an arm that
 *  assumed "nothing is granted yet" would be measuring run order rather than the screen. */
async function clearStandingGrants(journeyId: string): Promise<number> {
  const rows = await api<Grant[]>(`/journeys/${journeyId}/share-grants`);
  let revoked = 0;
  for (const row of rows.body) {
    if (row.revoked_at !== null) continue;
    const answer = await api(`/journeys/${journeyId}/share-grants/${row.id}/revoke`, { method: "POST" });
    expect(answer.status, "the revoke door must answer for the arm to start clean").toBe(200);
    revoked += 1;
  }
  return revoked;
}

for (const arm of [
  {
    locale: "zh-CN" as const,
    joined: "行程链接已复制到本机剪贴板。",
    tier: "获得链接者可见",
    openConsequence: "有链接的人不用先进成员名单就能读",
    retired: "谁拿到都能打开这份行程",
    markers: {
      NO_GRANT: "没有分享授权",
      REVOKED: "已撤销这枚授权",
      NOT_ANSWERED: "没有答",
      TIER_CLOSED: "档位是",
      OPEN: "档位是",
      UNKNOWN_TIER: "没有句子的档位",
      READ_FAILED: "读取分享授权失败",
    },
    noExpiry: "没给它记失效时刻",
    expiryBlame: "由那扇门答",
    instant: /\d{1,2}月\d{1,2}日\s?\d{1,2}:\d{2}/,
  },
  {
    locale: "en" as const,
    joined: "Journey link copied to this device's clipboard.",
    tier: "Anyone with the link",
    openConsequence: "Anyone holding the link can read it",
    retired: "Anyone with this link can open this journey",
    markers: {
      NO_GRANT: "lists no share grant",
      REVOKED: "revoked this grant",
      NOT_ANSWERED: "did not answer",
      TIER_CLOSED: "grant the server lists",
      OPEN: "grant here is",
      UNKNOWN_TIER: "no sentence for",
      READ_FAILED: "Reading the share grant failed",
    },
    noExpiry: "records no expiry instant",
    expiryBlame: "the door's answer",
    instant: /\d{1,2} \w{3,9},? \d{1,2}:\d{2}/,
  },
]) {
  test(`ORD-0447 ${arm.locale} - the sentence reads the grant, and a revocation moves it`, async ({
    page,
    context,
  }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.addInitScript((value) => localStorage.setItem("citynexus.locale", value), arm.locale);
    await page.goto("/v8/hub");
    const journeyId = await page.evaluate(() => localStorage.getItem("citynexus.v8.journey_id"));
    expect(journeyId, "the seed must have left a journey on this device").toBeTruthy();
    await page.goto(`/v8/${journeyId}`);
    /* Two probes for the one thing that went wrong the first time this ran: the page's own view of each
       grant read (did a service worker answer it, and what did the server say about caching), and the
       server's view at the same instant.  A sentence that lags behind the door needs a named cause,
       not a retry. */
    page.on("response", (response) => {
      if (!response.url().includes("/share-grants")) return;
      const headers = response.headers();
      note(
        `${arm.locale} page_response http=${response.status()} answered_by_service_worker=${response.fromServiceWorker()} cache_control=${headers["cache-control"] ?? "-"} age=${headers["age"] ?? "-"} etag=${headers["etag"] ?? "-"}`,
      );
    });

    /* Arm 0: what the screen says must be what the door answered.  The seed hands every spec the same
       journey, so the arm first puts that journey into a known state -- it revokes whatever the
       previous arm left standing, through the product's own revoke door -- and then reads.  Without
       that, the English arm was measuring "whatever the Chinese arm left behind": it saw two standing
       grants, said the true sentence for them, and the next arm's "the words must have moved" tripped
       on a sentence that had every right to stay. */
    const pre = await api<Grant[]>(`/journeys/${journeyId}/share-grants`);
    for (const row of pre.body) {
      if (row.revoked_at !== null) continue;
      const clear = await api(`/journeys/${journeyId}/share-grants/${row.id}/revoke`, { method: "POST" });
      expect(clear.status, "the arm cannot start clean unless revoke answers").toBe(200);
    }
    const listed = await api<Grant[]>(`/journeys/${journeyId}/share-grants`);
    expect(listed.status, "ORD-0434's list door must answer, or this arm measures nothing").toBe(200);
    const rows = listed.body.map((row) => ({
      id: row.id,
      visibility: row.visibility,
      expires_at: row.expires_at,
      revoked_at: row.revoked_at,
    }));
    const predicted = claimFromRead({ state: "ANSWERED", http_status: 200, rows: rows as GrantRow[] });
    note(`${arm.locale} arm0 rows=${rows.length} predicted=${predicted.kind}`);
    const bare = await tap(page);
    note(`${arm.locale} arm0_sentence="${bare}"`);
    expect(bare, "the copy fact and the grant fact are two sentences in one slot").toContain(arm.joined);
    expect(bare, `the screen must say the ${predicted.kind} sentence the door's answer implies`).toContain(
      arm.markers[predicted.kind],
    );
    expect(bare, "the retired unconditional reach claim must not come back").not.toContain(arm.retired);

    /* Arm 1: one standing link-readable grant with no expiry recorded. */
    const created = await api<Grant>(`/journeys/${journeyId}/share-grants`, {
      method: "POST",
      body: { visibility: "LINK_READ", created_by: "ord0447-e2e" },
    });
    expect(created.status, "the create door answers 201 with the row it stored").toBe(201);
    note(`${arm.locale} create_http=${created.status} visibility=${created.body.visibility} expires_at=${created.body.expires_at}`);
    const open = await tap(page);
    note(`${arm.locale} standing_sentence="${open}"`);
    expect(open, "the tier reaches the screen as a sentence, not as a key or an enum token").toContain(arm.tier);
    expect(open).not.toContain("visLinkRead");
    expect(open, "an expiry the server never recorded may not be invented here").toContain(arm.noExpiry);

    /* Arm 2: the counter-arm.  The same tap on the same screen, once the server has revoked that grant. */
    const revoked = await api(`/journeys/${journeyId}/share-grants/${created.body.id}/revoke`, {
      method: "POST",
    });
    expect(revoked.status, "revocation is a server answer, not a local flag").toBe(200);
    const after = await tap(page);
    const serverNow = await api<Grant[]>(`/journeys/${journeyId}/share-grants`);
    note(
      `${arm.locale} arm2_server_rows_now=${serverNow.body
        .map((row) => (row.revoked_at === null ? "standing" : "revoked"))
        .join(",")}`,
    );
    note(`${arm.locale} revoke_http=${revoked.status} sentence_changed=${after !== open}`);
    note(`${arm.locale} after_revoke="${after}"`);
    expect(after, "the sentence must move when the grant stops standing").not.toBe(open);
    expect(after).toContain(arm.markers.REVOKED);
    expect(after, "a revoked grant may not keep the tier's reach sentence").not.toContain(arm.openConsequence);

    /* Arm 3: a grant whose expiry instant the server recorded.  The screen may show the instant it was
       given; deciding whether that instant has passed belongs to the door that answers 409. */
    const until = new Date(Date.now() + 2 * 3600 * 1000).toISOString().replace(/\.\d{3}Z$/, "+00:00");
    const dated = await api<Grant>(`/journeys/${journeyId}/share-grants`, {
      method: "POST",
      body: { visibility: "LINK_READ", created_by: "ord0447-e2e", expires_at: until },
    });
    expect(dated.status, "an instant with an offset is accepted").toBe(201);
    expect(dated.body.expires_at, "the row carries back the instant the server was given").toBe(until);
    const withExpiry = await tap(page);
    note(`${arm.locale} dated_http=${dated.status} instant_sent=${until}`);
    note(`${arm.locale} dated_sentence="${withExpiry}"`);
    expect(withExpiry).toContain(arm.tier);
    expect(withExpiry, "the instant the server recorded reaches the screen, formatted").toMatch(arm.instant);
    expect(withExpiry, "the screen must hand the passed-or-not question back to the door").toContain(
      arm.expiryBlame,
    );
    await page.screenshot({ path: `${LOG.replace(/\.log$/, "")}-${arm.locale}.png` });
  });
}

test("ORD-0447 - an unknown tier, a closed tier and an unanswered door claim nothing", async () => {
  /* The two shapes no door answers on today's line, run through the same derivation the screen uses:
     a fifth tier name this tree has no ladder row for, and a door that does not exist at all.  Both
     must answer without a reach claim, which is what keeps a future tier from silently reading as
     "open" the day somebody adds one. */
  const unknown = claimFromRead({
    state: "ANSWERED",
    http_status: 200,
    rows: [{ id: "g", visibility: "EVERYONE_READABLE", expires_at: null, revoked_at: null }],
  });
  expect(unknown, "a tier with no ladder row claims nothing").toEqual({ kind: "UNKNOWN_TIER" });
  const absent = claimFromRead({ state: "NOT_ANSWERED", http_status: 404 });
  expect(absent).toEqual({ kind: "NOT_ANSWERED", httpStatus: 404 });
  const closed = claimFromRead({
    state: "ANSWERED",
    http_status: 200,
    rows: [{ id: "g", visibility: "INVITEES_ONLY", expires_at: null, revoked_at: null }],
  });
  expect(closed.kind, "an invitees-only grant is not a link claim").toBe("TIER_CLOSED");
  /* A revocation instant beats an expiry instant, and a standing row beats a revoked one -- the order
     the component depends on, pinned without a browser. */
  const mixed = claimFromRead({
    state: "ANSWERED",
    http_status: 200,
    rows: [
      { id: "a", visibility: "LINK_READ", expires_at: null, revoked_at: "2026-09-29T01:00:00+00:00" },
      { id: "b", visibility: "LINK_READ", expires_at: "2026-09-29T09:00:00+00:00", revoked_at: null },
    ],
  });
  expect(mixed.kind, "one standing grant is enough to stop the revoked sentence").toBe("OPEN");
  note(`derivation unknown=${unknown.kind} absent=${absent.kind} closed=${closed.kind} mixed=${mixed.kind}`);
});
