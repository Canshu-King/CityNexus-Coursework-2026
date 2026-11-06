import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { openScreen } from "./_qoderShellSeed";
import { evidencePath } from "./_qoderEvidence";

/**
 * ORD-0060 -- the wizard's own words about the destination.
 *
 * Three defects, one user-visible shape: the screen told a story the traveller had to
 * correct. A person going to Tokyo was asked to "抵达厦门"; a person typing a city the
 * built-in table does not know got "Next" enabled and the *previous* city's country,
 * currency and timezone carried forward in hidden fields; and nobody could see which
 * destinations are recognised at all.
 *
 * Read together with the standing rule that a disabled control without an inline
 * explanation is still a silent blocker: the fix says the assumption out loud and leaves
 * the traveller able to proceed, instead of adding a second way to be stuck quietly.
 */

const FILE = "e2e/qoder-wizard-city-copy.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;

async function openFreshWizard(page: Page, locale: string) {
  await page.addInitScript((value) => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", value);
  }, locale);
  await openScreen(page, "/v8", ["trial-setup"]);
}

test.describe("ORD-0060 the wizard describes the destination it will actually plan", () => {
  test("city-label-follows-the-recognised-city", async ({ page }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    await openFreshWizard(page, "zh-CN");
    await page.getByTestId("trial-city").fill("东京");

    const arrival = (await page.getByTestId("trial-arrival").evaluate(
      (el) => el.closest("label")?.querySelector("span")?.textContent ?? "",
    )).trim();
    const departure = (await page.getByTestId("trial-departure").evaluate(
      (el) => el.closest("label")?.querySelector("span")?.textContent ?? "",
    )).trim();
    expect(
      arrival,
      node("city-label-follows-the-recognised-city",
        `the arrival field for a Tokyo trip is labelled ${JSON.stringify(arrival)}`),
    ).toContain("抵达东京");
    expect(
      /厦门/.test(arrival + departure),
      node("city-label-follows-the-recognised-city",
        `a Xiamen label is still on a Tokyo trip: ${JSON.stringify({ arrival, departure })}`),
    ).toBe(false);
    expect(
      departure,
      node("city-label-follows-the-recognised-city", `the departure label did not follow the city`),
    ).toContain("离开东京");

    // English never named a city, so it must still not start naming one from this table.
    await openFreshWizard(page, "en");
    await page.getByTestId("trial-city").fill("tokyo");
    const enLabel = await page.getByTestId("trial-arrival").evaluate(
      (el) => el.closest("label")?.querySelector("span")?.textContent ?? "",
    );
    expect(
      enLabel.trim(),
      node("city-label-follows-the-recognised-city", `the English label changed shape: ${JSON.stringify(enLabel)}`),
    ).toBe("Arrive at");
    writeFileSync(
      evidencePath("ui-overnight/wizard-city-labels.json"),
      JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0060", node: "city-label-follows-the-recognised-city", arrival, departure, enLabel: enLabel.trim() }, null, 2) + "\n",
      "utf-8",
    );
  });

  test("unrecognised-city-assumption-is-said-out-loud", async ({ page }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 360, height: 800 });
    await openFreshWizard(page, "zh-CN");
    await page.getByTestId("trial-city").fill("拉萨");

    const warning = page.getByTestId("trial-city-warning");
    await expect(
      warning,
      node("unrecognised-city-assumption-is-said-out-loud", "an unknown city is accepted with no word about what will be assumed"),
    ).toBeVisible();
    const text = (await warning.innerText()).replace(/\s+/g, " ").trim();
    expect(
      text,
      node("unrecognised-city-assumption-is-said-out-loud", `the caution does not name the assumption: ${JSON.stringify(text)}`),
    ).toMatch(/没认出|not recognised/);
    for (const carried of ["CN", "CNY", "Asia/Shanghai"]) {
      expect(
        text,
        node("unrecognised-city-assumption-is-said-out-loud",
          `the hidden field still carries ${carried} but the caution never says so`),
      ).toContain(carried);
    }
    expect(
      await page.getByTestId("trial-city").getAttribute("aria-describedby"),
      node("unrecognised-city-assumption-is-said-out-loud", "the caution is visible but not attached to the field it describes"),
    ).toContain("trial-city-warning");
    // No new silent blocker: the traveller may go on, informed.
    await page.getByTestId("trial-arrival").fill("拉萨贡嘎机场");
    await page.getByTestId("trial-departure").fill("拉萨火车站");
    await expect(
      page.getByTestId("trial-next"),
      node("unrecognised-city-assumption-is-said-out-loud", "the fix was only to disable the button"),
    ).toBeEnabled();
    await page.getByTestId("trial-next").click();
    await page.getByTestId("trial-next").click();
    await expect(page.getByTestId("trial-create")).toBeVisible({ timeout: 10_000 });
    expect(
      (await page.getByTestId("review-city-warning").innerText()).replace(/\s+/g, " ").trim(),
      node("unrecognised-city-assumption-is-said-out-loud", "the confirmation screen dropped the caution the traveller acted on"),
    ).toContain("未识别");
  });

  test("supported-destinations-are-reachable-without-an-invented-count", async ({ page }) => {
    test.setTimeout(150_000);
    await page.emulateMedia({ colorScheme: "light", media: "screen" });
    await page.setViewportSize({ width: 390, height: 844 });
    await openFreshWizard(page, "zh-CN");

    const toggle = page.getByTestId("trial-supported-toggle");
    await expect(
      toggle,
      node("supported-destinations-are-reachable-without-an-invented-count",
        "nothing on any wizard screen says which destinations are supported"),
    ).toBeVisible();
    const summary = (await toggle.innerText()).trim();
    expect(
      /\d/.test(summary),
      node("supported-destinations-are-reachable-without-an-invented-count",
        `the entry states a number this lane does not own: ${JSON.stringify(summary)}`),
    ).toBe(false);
    await toggle.click();
    const list = (await page.getByTestId("trial-supported-cities").innerText()).replace(/\s+/g, " ");
    for (const city of ["天津", "东京", "新加坡"]) {
      expect(
        list,
        node("supported-destinations-are-reachable-without-an-invented-count",
          `${city} is recognised by the wizard but never listed for the traveller`),
      ).toContain(city);
    }
    writeFileSync(
      evidencePath("ui-overnight/wizard-supported.json"),
      JSON.stringify({ generatedBy: "e345b9d6", order: "ORD-0060", node: "supported-destinations-are-reachable-without-an-invented-count", summary, list: list.trim().slice(0, 240) }, null, 2) + "\n",
      "utf-8",
    );
  });
});
