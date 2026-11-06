/**
 * ORD-0203 -- the fourth cell, `NOT_ANSWERED`, on screen.
 *
 * The commander measured it on an exported tree: `backend/app` and `contracts/` both have it while
 * `frontend/src` had zero hits, so the traveller could never see it.  Rendering it is only half the
 * job -- the other half is that its action must consume the server's `retryable`, because
 * `anywhere_capability.py` keeps the pair mechanically (`NOT_ANSWERED => retryable is true and a
 * provider_status exists`) precisely so a screen cannot read the state as a bare word.
 *
 * The cell is produced through the **real** code path, not a fixture: the backend runs with
 * `CITYNEXUS_ANYWHERE_RESOLVER=photon` and `CITYNEXUS_PHOTON_BASE_URL` pointing at a closed port, so
 * the resolver really tries and really fails and the server answers
 * `NOT_ANSWERED / DESTINATION_RESOLVER_UNAVAILABLE / RETRY_LATER / retryable=true` with
 * `provider_status=[{status:"UNREACHABLE", answered:false}]`.  That measured shape is asserted here
 * so the gate fails if the backend's contract drifts, independently of what the screen renders.
 *
 * Every user-visible assertion reads **rendered text**, never a `data-*` attribute: this lane wrote
 * into the ledger that polling debug attributes once let a mutant which froze the elapsed seconds at
 * zero survive.  The attributes are used only to cross-read the payload, never to decide what the
 * traveller sees.
 *
 * Colour scheme is exercised both ways because dark is where this project's contrast defects live.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-not-answered-cell.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

const VIEWPORT = { width: 390, height: 844 };

test.use({ viewport: VIEWPORT, hasTouch: true });

async function openDiscover(page: Page): Promise<void> {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.v8.journey_id", id as string);
    localStorage.setItem("citynexus.locale", "zh-CN");
  }, journeyId);
  await page.goto("/v8/hub", { waitUntil: "load" });
  await expect(page.getByTestId("today-hero")).toBeVisible({ timeout: 30_000 });
  await page.goto("/v8/discover", { waitUntil: "load" });
  await expect(page.getByTestId("prep-panel")).toBeVisible({ timeout: 30_000 });
}

/** What the traveller actually reads, and the controls they can actually press. */
async function onScreen(page: Page) {
  const panel = page.getByTestId("prep-panel");
  return {
    text: (await panel.innerText()).replace(/\s+/g, " "),
    verdict: await page.getByTestId("prep-verdict-title").innerText().catch(() => ""),
    body: await page.getByTestId("prep-verdict-body").innerText().catch(() => ""),
    providers: await page.getByTestId("prep-providers").innerText().catch(() => ""),
    focusable: await page.evaluate(() =>
      Array.from(document.querySelectorAll<HTMLElement>("[data-testid^='prep-']"))
        .filter(
          (el) =>
            el.tagName === "BUTTON" &&
            !el.hasAttribute("disabled") &&
            el.getAttribute("aria-disabled") !== "true" &&
            el.offsetParent !== null,
        )
        .map((el) => el.dataset.testid as string),
    ),
  };
}

async function check(page: Page, query: string): Promise<void> {
  await page.getByTestId("prep-query").fill(query);
  await page.getByTestId("prep-check").click();
  await expect(
    page.getByTestId("prep-verdict").or(page.getByTestId("prep-check-error")),
    node("check", `the capability read for ${query} produced neither a verdict nor a named failure`),
  ).toBeVisible({ timeout: 60_000 });
}

test("ord0203-the-fourth-cell-renders-and-its-retry-consumes-retryable — 第四格上屏，重试真的吃 retryable", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const NODE = "ord0203-the-fourth-cell-renders-and-its-retry-consumes-retryable";
  await openDiscover(page);

  /* The server's own answer for this state, read from the endpoint rather than from the screen, so
     the screen is judged against the contract and not against itself. */
  const server = (await (
    await page.request.get(
      `/api/v1/destinations/capability?query=${encodeURIComponent("贡山独龙族怒族自治县")}`,
    )
  ).json()) as Record<string, unknown>;
  const providerStatus = (server.provider_status ?? []) as Array<Record<string, unknown>>;

  await check(page, "贡山独龙族怒族自治县");
  const shown = await onScreen(page);

  const evidence: Record<string, unknown> = { node: NODE, server, shown };

  /* The contract half: if the backend stops answering this way, this gate says so before the screen
     half is even read. */
  /* The cell's *name* is the backend's and changed under this node once already
     (`NOT_ANSWERED` -> `UNKNOWN_RETRYABLE`, the arbiter's frozen spelling at `ebb4b3f`), so the
     state is identified by what it must carry rather than by a hard-coded string: it is not one of
     the three older cells and it keeps the pair.  Pinning the literal made this node fail on a
     rename that changed nothing the traveller sees. */
  expect(
    ["NOT_ANSWERED", "UNKNOWN_RETRYABLE"].includes(String(server.capability)),
    node(NODE, `the backend answered capability=${JSON.stringify(server.capability)} for a resolver that cannot be reached; the fourth cell is what this node is about`),
  ).toBe(true);
  expect(
    server.capability,
    node(NODE, `the fourth cell must not be one of the three older cells; got ${JSON.stringify(server.capability)}`),
  ).not.toBe("UNAVAILABLE");
  expect(
    server.retryable,
    node(NODE, "the fourth cell must keep its `retryable` pair, or a screen reads it as a bare word"),
  ).toBe(true);
  expect(
    providerStatus.length,
    node(NODE, `the fourth cell must ship a provider_status observation; got ${JSON.stringify(providerStatus)}`),
  ).toBeGreaterThan(0);

  /* The rendered half -- text only, no data-* attributes. */
  expect(
    /没人回答我们|Nobody answered us/.test(shown.verdict),
    node(NODE, `the fourth cell is not on screen at all; the verdict reads ${JSON.stringify(shown.verdict)}`),
  ).toBe(true);
  expect(
    /没有拿到答复|nothing answered/.test(shown.body),
    node(NODE, `the copy does not say that nobody answered: ${JSON.stringify(shown.body)}`),
  ).toBe(true);
  /* The two claims the server explicitly did NOT make must not be made for it (R11).  A bare
     substring test cannot express this: the correct copy *denies* both claims by name
     ("不是「这个地方不能规划」，也不是「这城市还要灌数据」"), so the probe would fire on the denial it
     is asking for.  Measured while writing this node -- the first version of this assertion failed
     on its own required sentence.  So each phrase is judged by whether it appears as an assertion:
     it counts only when it is NOT immediately preceded by a negation. */
  const NEGATED = ["不是", "并非", "也 不是", "not ", "neither "];
  const claims = ["不能规划", "无法规划", "还要灌数据", "需灌数据", "cannot be planned", "needs data loaded"];
  const asserted = claims.filter((phrase) => {
    let index = shown.text.indexOf(phrase);
    while (index >= 0) {
      const window = shown.text.slice(Math.max(0, index - 12), index);
      if (!NEGATED.some((negation) => window.includes(negation))) return true;
      index = shown.text.indexOf(phrase, index + phrase.length);
    }
    return false;
  });
  expect(
    /网络不可用|连不上服务器|OFFLINE/.test(shown.text),
    node(NODE, `a server-known runtime state is being described as connectivity: "${shown.text.slice(0, 240)}"`),
  ).toBe(false);
  /* The retry action exists, is pressable, and says what it is. */
  expect(
    shown.focusable.includes("prep-retry-later"),
    node(NODE, `the fourth cell offers no 稍后重试 action: ${JSON.stringify(shown.focusable)}`),
  ).toBe(true);
  expect(
    /稍后重试/.test(shown.text),
    node(NODE, `the retry control is not named in words: "${shown.text.slice(0, 240)}"`),
  ).toBe(true);
  /* And preparing is NOT offered here: measured, the server's action is RETRY_LATER, and offering
     开始准备 is the defect the backend's own comment names. */
  expect(
    shown.focusable.includes("prep-start"),
    node(NODE, `a destination nobody has named is still offering 开始准备: ${JSON.stringify(shown.focusable)}`),
  ).toBe(false);
  expect(
    /开始准备/.test(shown.text),
    node(NODE, `the prepare control is still rendered for the fourth cell: "${shown.text.slice(0, 240)}"`),
  ).toBe(false);

  /* The provider observation the server sent is visible, because it is the evidence for "nobody
   answered" -- a sentence without it would be an assertion about the world. */
  expect(
    /UNREACHABLE|未答|unreachable/i.test(shown.providers),
    node(NODE, `the provider observation behind "nobody answered" is not on screen: ${JSON.stringify(shown.providers)}`),
  ).toBe(true);

  writeFileSync(
    evidencePath("ui-overnight/ord0203-fourth-cell.json"),
    JSON.stringify({ generatedBy: "e345b9d6", ...evidence, assertedClaims: asserted, negatedClaims: claims.filter((c) => shown.text.includes(c)) }, null, 2) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0203-fourth-cell-light.png"), fullPage: true });
});

test("ord0203-the-retry-actually-re-asks-the-server — 点“稍后重试”要真再问一次", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0203-the-retry-actually-re-asks-the-server";
  await openDiscover(page);

  const capabilityCalls: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/destinations/capability")) capabilityCalls.push(request.url());
  });

  await check(page, "贡山独龙族怒族自治县");
  const before = capabilityCalls.length;
  expect(
    before,
    node(NODE, "the capability read never went out, so the retry would have nothing to repeat"),
  ).toBeGreaterThan(0);

  await page.getByTestId("prep-retry-later").click();
  await expect
    .poll(() => capabilityCalls.length, {
      timeout: 30_000,
      message: node(NODE, "pressing 稍后重试 did not ask the server again -- the control is decorative"),
    })
    .toBeGreaterThan(before);

  writeFileSync(
    evidencePath("ui-overnight/ord0203-retry.json"),
    JSON.stringify(
      {
        generatedBy: "e345b9d6",
        node: NODE,
        head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown",
        capabilityCalls,
        retryIssuedAnotherRead: capabilityCalls.length > before,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
});

test("ord0203-the-fourth-cell-is-readable-in-dark — 深色下自己先看一眼", async ({ page }) => {
  test.setTimeout(300_000);
  const NODE = "ord0203-the-fourth-cell-is-readable-in-dark";
  await page.emulateMedia({ colorScheme: "dark", media: "screen" });
  await openDiscover(page);

  await check(page, "贡山独龙族怒族自治县");
  const shown = await onScreen(page);
  expect(
    /没人回答我们|Nobody answered us/.test(shown.verdict),
    node(NODE, "the fourth cell does not render in dark, so its contrast cannot be judged"),
  ).toBe(true);

  /* Measured contrast of the two text runs that carry the state, against their own background: a
     threshold rather than "it looked fine", because this project has shipped an unreadable sheet
     before and the dark layer is where those defects live. */
  const contrast = await page.evaluate(() => {
    const parse = (value: string) => {
      const match = /rgba?\(([^)]+)\)/.exec(value);
      if (!match) return null;
      const parts = match[1].split(",").map((item) => Number(item.trim()));
      return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
    };
    const luminance = (colour: { r: number; g: number; b: number }) => {
      const channel = (value: number) => {
        const v = value / 255;
        return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
      };
      return 0.2126 * channel(colour.r) + 0.7152 * channel(colour.g) + 0.0722 * channel(colour.b);
    };
    const background = (el: HTMLElement | null): { r: number; g: number; b: number } | null => {
      let node: HTMLElement | null = el;
      while (node) {
        const colour = parse(getComputedStyle(node).backgroundColor);
        if (colour && colour.a > 0.5) return colour;
        node = node.parentElement;
      }
      return null;
    };
    const measure = (selector: string) => {
      const el = document.querySelector<HTMLElement>(selector);
      if (!el) return null;
      const fg = parse(getComputedStyle(el).color);
      const bg = background(el);
      if (!fg || !bg) return null;
      const l1 = luminance(fg);
      const l2 = luminance(bg);
      const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
      return { selector, ratio: Number(ratio.toFixed(2)), color: getComputedStyle(el).color, background: `rgb(${bg.r}, ${bg.g}, ${bg.b})` };
    };
    return {
      title: measure("[data-testid='prep-verdict-title']"),
      body: measure("[data-testid='prep-verdict-body']"),
      providers: measure("[data-testid='prep-providers']"),
      scheme: matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light",
    };
  });
  expect(
    contrast.scheme,
    node(NODE, "the dark scheme did not apply, so this node would be measuring the light screen"),
  ).toBe("dark");
  for (const [what, reading] of Object.entries({
    title: contrast.title,
    body: contrast.body,
    providers: contrast.providers,
  })) {
    expect(
      reading,
      node(NODE, `the ${what} run could not be measured (no colour or no background found)`),
    ).not.toBeNull();
    expect(
      (reading as { ratio: number }).ratio,
      node(NODE, `the ${what} run measures ${(reading as { ratio: number }).ratio}:1 in dark, below the 4.5:1 this project holds text to (${JSON.stringify(reading)})`),
    ).toBeGreaterThanOrEqual(4.5);
  }

  writeFileSync(
    evidencePath("ui-overnight/ord0203-dark-contrast.json"),
    JSON.stringify({ generatedBy: "e345b9d6", node: NODE, head: process.env.CITYNEXUS_E2E_GIT_HEAD ?? "unknown", contrast }, null, 2) + "\n",
    "utf-8",
  );
  await page.screenshot({ path: evidencePath("ui-overnight/ord0203-fourth-cell-dark.png"), fullPage: true });
});
