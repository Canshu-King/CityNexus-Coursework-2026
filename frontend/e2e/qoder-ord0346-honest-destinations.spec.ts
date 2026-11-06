import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0346 -- 「看支持的目的地」 must group by what the server answered, not by which spellings a
 * client-side table happens to recognise.
 *
 * The panel used to print `CITY_FACTS`' eleven Chinese names under the words "支持的目的地".  Asked
 * directly at this generation, the server gives no identity at all to three of them and calls none
 * of the eleven `READY`, so the heading was a promise the product does not make.  These arms read
 * the rendered screen -- not the component's internals -- because the claim being tested is the one
 * a traveller sees.
 *
 * Both directions are asserted, and a third arm checks the identity column structurally: a
 * placeholder that merely *looks* populated (the `unresolved:<hash>` shape this project already
 * retired) must not be able to pass as an identity here.
 */

const FILE = "e2e/qoder-ord0346-honest-destinations.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const ZH_OFFERED = 11;

type PanelRow = {
  spelling: string;
  group: string;
  answered: string;
  capability: string | null;
  reasonCode: string | null;
  action: string | null;
  identityAttr: string | null;
  identityText: string;
  verdictText: string;
  fieldsText: string;
};
type Panel = {
  summary: string;
  groups: { key: string; title: string; rule: string; empty: string | null }[];
  rows: PanelRow[];
};

async function openFreshWizard(page: Page, locale: string) {
  await page.addInitScript((value) => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", value);
  }, locale);
  await openScreen(page, "/v8", ["trial-setup"]);
}

function readPanel(page: Page): Promise<Panel> {
  return page.getByTestId("trial-supported-cities").evaluate((root) => {
    const groups = Array.from(
      root.querySelectorAll("section[data-testid^='trial-supported-group-']"),
    ).map((section) => ({
      key: (section.getAttribute("data-testid") ?? "").replace("trial-supported-group-", ""),
      title: section.querySelector(".trial-supported-group-title")?.textContent ?? "",
      rule: section.querySelector(".trial-supported-group-rule")?.textContent ?? "",
      empty: section.querySelector(".trial-supported-none")?.textContent ?? null,
    }));
    const rows: PanelRow[] = Array.from(
      root.querySelectorAll("li[data-testid^='trial-supported-row-']"),
    ).map((li) => ({
      spelling: li.querySelector(".trial-supported-name")?.textContent ?? "",
      group:
        li.closest("section")?.getAttribute("data-testid")?.replace("trial-supported-group-", "") ??
        "",
      answered: li.getAttribute("data-answered") ?? "",
      capability: li.getAttribute("data-capability"),
      reasonCode: li.getAttribute("data-reason-code"),
      action: li.getAttribute("data-recommended-action"),
      identityAttr: li.getAttribute("data-canonical-destination-id"),
      identityText: li.querySelector(".trial-supported-identity")?.textContent ?? "",
      verdictText: li.querySelector(".trial-supported-verdict")?.textContent ?? "",
      fieldsText: li.querySelector(".trial-supported-fields")?.textContent ?? "",
    }));
    return { summary: root.querySelector("summary")?.textContent ?? "", groups, rows };
  });
}

const rowOf = (panel: Panel, spelling: string) => {
  const found = panel.rows.find((row) => row.spelling === spelling);
  if (!found) throw new Error(`${spelling} is not on the panel at all`);
  return found;
};

async function settle(page: Page, until: (panel: Panel) => boolean) {
  await expect
    .poll(async () => until(await readPanel(page)), { timeout: 40_000 })
    .toBe(true);
}

test.describe("ORD-0346 the supported-destinations panel speaks from the server's answer", () => {
  test("zh-arms-supported-not-mislabelled-and-unnamed-not-claimed", async ({ page }) => {
    test.setTimeout(200_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    await openFreshWizard(page, "zh-CN");
    await page.getByTestId("trial-supported-toggle").click();
    await settle(page, (panel) => panel.rows.length === ZH_OFFERED
      && panel.rows.every((row) => row.answered === "yes"));

    const panel = await readPanel(page);
    const beijing = rowOf(panel, "北京");
    const tokyo = rowOf(panel, "东京");
    const xiamen = rowOf(panel, "厦门");

    /* The supported leg: the server named this place and holds material for it, and the panel has
       to put it where a traveller will find it. */
    expect(beijing.group, node("supported-not-mislabelled", `北京 landed in ${beijing.group}: ${JSON.stringify(beijing)}`))
      .toBe("supported");
    expect(beijing.identityAttr, node("supported-not-mislabelled", `北京's identity column reads ${beijing.identityAttr}`))
      .toBe("CN-BEIJING");
    expect(beijing.identityText, node("supported-not-mislabelled", `北京's identity line: ${beijing.identityText}`))
      .toContain("服务器给了身份");
    expect(beijing.fieldsText, node("supported-not-mislabelled", `北京's own fields: ${beijing.fieldsText}`))
      .toContain("HYDRATABLE");

    /* The unsupported leg, both faces: 东京 is never in the supported group, and its empty identity
       is named instead of filled with something that looks like an identifier. */
    expect(tokyo.group, node("unsupported-cannot-claim-supported", `东京 landed in ${tokyo.group}`)).toBe("needs_lookup");
    expect(panel.groups.find((group) => group.key === "supported")?.empty, node(
      "unsupported-cannot-claim-supported", "the supported group is not empty, so it cannot carry 东京 as a negative control")).toBe(null);
    expect(
      (await page.getByTestId("trial-supported-group-supported").innerText()).replace(/\s+/g, " "),
      node("unsupported-cannot-claim-supported", "东京 appears under the supported heading"),
    ).not.toContain("东京");
    expect(tokyo.identityAttr, node("unnamed-identity-is-named", `东京's identity column carries ${JSON.stringify(tokyo.identityAttr)}`))
      .toBe(null);
    expect(tokyo.identityText, node("unnamed-identity-is-named", `东京's identity line: ${tokyo.identityText}`))
      .toBe("还没有身份");
    expect(tokyo.reasonCode, node("unnamed-identity-is-named", `东京's reason: ${tokyo.reasonCode}`))
      .toBe("NO_CANONICAL_IDENTITY");

    /* The middle case the old list erased entirely: a name the server can identify but holds nothing
       for.  It has an identity and it is not supported -- both of those have to be on the screen. */
    expect(xiamen.group, node("named-but-empty-is-not-supported", `厦门 landed in ${xiamen.group}`)).toBe("unknown");
    expect(xiamen.identityText, node("named-but-empty-is-not-supported", `厦门's identity line: ${xiamen.identityText}`))
      .toContain("服务器给了身份");
    expect(xiamen.verdictText, node("named-but-empty-is-not-supported", `厦门's verdict line: ${xiamen.verdictText}`))
      .toContain("0 条");

    /* Structural read of the whole identity column: only a real identity may stand in it, in any
       group, and "looks filled" is not the test.
       (First version of this line also carried a bare `sha` alternative, which matched
       `CN-SHANGHAI` and reddened a valid identity -- the digest words are named in full here.) */
    const placeholderShaped = panel.rows
      .filter((row) => row.identityAttr !== null)
      .filter((row) => !/^CN-[A-Z][A-Z_]+$/.test(String(row.identityAttr))
        || /unresolved|pending:|pend-|digest|sha1|sha256|hash/i.test(String(row.identityAttr)));
    expect(placeholderShaped, node("identity-column-carries-only-identities", `identity column holds: ${JSON.stringify(placeholderShaped.map((r) => [r.spelling, r.identityAttr]))}`))
      .toEqual([]);
    expect(
      panel.rows.filter((row) => row.identityAttr === null && row.identityText !== "还没有身份").map((row) => row.spelling),
      node("identity-column-carries-only-identities", "a row without an identity spoke about something other than the missing identity"),
    ).toEqual([]);

    /* ORD-0060 #3 is still in force: the entry itself states no number. */
    expect(/\d/.test(panel.summary.trim()), node("entry-states-no-number", `the summary reads ${JSON.stringify(panel.summary)}`)).toBe(false);
    expect(panel.groups.map((group) => group.key), node("three-groups-are-always-said", `groups rendered: ${JSON.stringify(panel.groups.map((g) => g.key))}`))
      .toEqual(["supported", "needs_lookup", "unknown"]);

    /* The picture is of the panel, not of wherever the viewport happened to be: at 390x844 the
       sticky bar sits over the middle of this list, and a whole-page shot graded the scroll
       position instead of the control (the ORD-0261 lesson, from the other side). */
    const panelLocator = page.getByTestId("trial-supported-cities");
    await panelLocator.scrollIntoViewIfNeeded();
    await panelLocator.screenshot({ path: evidencePath("ord0346/panel-zh-light.png") });
    await page.emulateMedia({ colorScheme: "dark", media: "screen" });
    await page.waitForTimeout(250);
    await panelLocator.screenshot({ path: evidencePath("ord0346/panel-zh-dark.png") });
    writeFileSync(
      evidencePath("ord0346/panel-zh-read.json"),
      JSON.stringify({ generatedBy: "S4-ast-taint", order: "ORD-0346", arm: "zh real reads", panel }, null, 1) + "\n",
      "utf-8",
    );
  });

  test("en-spellings-group-the-same-way", async ({ page }) => {
    test.setTimeout(200_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    await openFreshWizard(page, "en");
    await page.getByTestId("trial-supported-toggle").click();
    await settle(page, (panel) => panel.rows.length === ZH_OFFERED
      && panel.rows.every((row) => row.answered === "yes"));

    const panel = await readPanel(page);
    expect(rowOf(panel, "Beijing").group, node("en-arms", `Beijing landed in ${rowOf(panel, "Beijing").group}`)).toBe("supported");
    expect(rowOf(panel, "Tokyo").group, node("en-arms", `Tokyo landed in ${rowOf(panel, "Tokyo").group}`)).toBe("needs_lookup");
    expect(rowOf(panel, "Tokyo").identityText, node("en-arms", `Tokyo's identity line: ${rowOf(panel, "Tokyo").identityText}`)).toBe("no identity yet");
    writeFileSync(
      evidencePath("ord0346/panel-en-read.json"),
      JSON.stringify({ generatedBy: "S4-ast-taint", order: "ORD-0346", arm: "en real reads", panel }, null, 1) + "\n",
      "utf-8",
    );
  });

  test("a-read-that-never-lands-is-not-reported-as-a-server-verdict", async ({ page }) => {
    test.setTimeout(200_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    /* The failure is caused here rather than read from a backend: the state under test is "this
       device got nothing", and no server fact is being claimed by it. */
    await page.route("**/api/v1/destinations/capability*", (route) => route.abort());
    await openFreshWizard(page, "zh-CN");
    await page.getByTestId("trial-supported-toggle").click();
    await settle(page, (panel) => panel.rows.length === ZH_OFFERED
      && panel.rows.every((row) => row.fieldsText !== "" && row.fieldsText !== "not-asked"));

    const panel = await readPanel(page);
    expect(
      panel.rows.filter((row) => row.answered === "yes").map((row) => row.spelling),
      node("failed-read-cannot-become-a-verdict", `rows claiming an answer: ${JSON.stringify(panel.rows.filter((r) => r.answered === "yes").map((r) => [r.spelling, r.fieldsText]))}`),
    ).toEqual([]);
    expect(
      panel.rows.filter((row) => row.group === "supported").map((row) => row.spelling),
      node("failed-read-cannot-become-a-verdict", "a name reached the supported group with no answer behind it"),
    ).toEqual([]);
    expect(
      panel.groups.find((group) => group.key === "supported")?.empty,
      node("failed-read-cannot-become-a-verdict", "the supported group did not say it holds nothing"),
    ).not.toBe(null);
    expect(
      [...new Set(panel.rows.map((row) => row.identityText))],
      node("failed-read-cannot-become-a-verdict", "a row spoke about identity before anything answered"),
    ).toEqual(["身份还没读到"]);
    /* The cause is this device's own enum, and the upstream text is not on the screen anywhere. */
    expect(
      [...new Set(panel.rows.map((row) => row.fieldsText))].sort(),
      node("failed-read-cannot-become-a-verdict", "the failure was not attributed to one named device cause"),
    ).toEqual(["NETWORK_UNAVAILABLE"]);
    const unread = page.getByTestId("trial-supported-cities");
    await unread.scrollIntoViewIfNeeded();
    await unread.screenshot({ path: evidencePath("ord0346/panel-unread.png") });
    writeFileSync(
      evidencePath("ord0346/panel-unread.json"),
      JSON.stringify({ generatedBy: "S4-ast-taint", order: "ORD-0346", arm: "reads aborted", panel }, null, 1) + "\n",
      "utf-8",
    );
  });
});
