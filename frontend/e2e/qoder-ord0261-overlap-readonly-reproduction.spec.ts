/**
 * ORD-0261 (read-only reproduction first): HJ-P3 measured that `0fd3cc7` fixed the *sizes* on the
 * wizard screen — 「看支持的目的地」91×30⇒91×44 and 「高级设置」349×16⇒349×44 — but the centre of the
 * 「高级设置」 SUMMARY lands on a different control with the same label (the `trial-show-advanced`
 * BUTTON that sits beside the details and force-opens it).  Size is not tappability.
 *
 * This file measures and reports only: it enumerates every rendered control by label on the wizard
 * screen at 390×844 and records, for each, what `elementFromPoint(centre)` actually hits.  The
 * red/green acceptance and its killer get written only after the commander rules on the reproduction;
 * per the single-writer protocol this candidate ships as a measurement, not a fix.
 */
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { evidencePath } from "./_qoderEvidence";

const FILE = "e2e/qoder-ord0261-overlap-readonly-reproduction.spec.ts";
const node = (id: string, detail: string) => `${FILE} :: ${id} — ${detail}`;
const VIEWPORT = { width: 390, height: 844 };
test.use({ viewport: VIEWPORT, hasTouch: true });

const LABELS = ["高级设置", "看支持的目的地"];

type Hit = {
  label: string;
  role: string;
  box: [number, number];
  centre: [number, number];
  hitTag: string;
  hitRole: string;
  hitText: string;
  hitsItself: boolean;
};

async function openWizard(page: Page) {
  const seeded = (await (await page.request.get("/api/v1/demo/scene")).json()) as {
    journey_id?: string;
  };
  const journeyId = String(seeded.journey_id ?? "");
  expect(journeyId.length, node("seed", "/api/v1/demo/scene returned no journey_id")).toBeGreaterThan(8);
  await page.addInitScript((id) => {
    localStorage.clear();
    localStorage.setItem("citynexus.locale", "zh-CN");
    // A stored journey id would drop the wizard straight to the hub; this screen needs the wizard.
    localStorage.removeItem("citynexus.v8.journey_id");
  }, journeyId);
  await page.goto("/v8", { waitUntil: "load" });
  await page.waitForTimeout(1_000);
}

/** Every rendered control whose text matches one of the labels, with the true centre hit. */
async function inventory(page: Page): Promise<Hit[]> {
  return await page.evaluate((labels: string[]) => {
    const out: unknown[] = [];
    for (const el of Array.from(document.querySelectorAll("button, summary, a"))) {
      const text = (el.textContent ?? "").replace(/\s+/g, " ").trim();
      if (!labels.some((label) => text.includes(label))) continue;
      const box = el.getBoundingClientRect();
      if (box.width <= 0 || box.height <= 0) continue;
      const cx = box.left + box.width / 2;
      const cy = box.top + box.height / 2;
      const hit = document.elementFromPoint(cx, cy);
      const hitText = hit ? (hit.textContent ?? "").replace(/\s+/g, " ").trim() : "";
      const role = el.tagName === "SUMMARY" ? "summary" : el.tagName.toLowerCase();
      const hitRole = hit ? (hit.tagName === "SUMMARY" ? "summary" : hit.tagName.toLowerCase()) : "";
      out.push({
        label: text.slice(0, 20),
        role,
        box: [Math.round(box.width), Math.round(box.height)],
        centre: [Math.round(cx), Math.round(cy)],
        hitTag: hit ? hit.tagName : "",
        hitRole,
        hitText: hitText.slice(0, 20),
        // "命中它自己（或它内部的 label）" — a control counts when the browser's own hit test lands
        // on it or on a descendant of it, not on a sibling that happens to share the words.
        hitsItself: hit === el || (!!hit && el.contains(hit)),
      });
    }
    return out;
  }, LABELS);
}

test("ord0261-readonly-1-the-reproduction-at-current-head — 复现：中心到底命中谁", async ({ page }) => {
  test.setTimeout(180_000);
  await openWizard(page);
  const rows = await inventory(page);

  // The sweep must have actually found both reported controls; an empty inventory measures nothing.
  for (const label of LABELS) {
    expect(
      rows.some((row) => row.label.includes(label)),
      node("ord0261-readonly-1", `"${label}" was not found on the wizard screen at all`),
    ).toBe(true);
  }

  const overlapping = rows.filter((row) => !row.hitsItself);
  const summary = {
    generatedBy: "e345b9d6",
    order: "ORD-0261 read-only reproduction (no fix shipped, per the single-writer protocol)",
    at: new Date().toISOString(),
    tree: "agent/d-e345b9d6-candidates @ dea8410 (the authoritative tree, read-only)",
    viewport: VIEWPORT,
    labelsSwept: LABELS,
    controls: rows,
    centreMisses: overlapping,
    verdict:
      overlapping.length === 0
        ? "NOT_REPRODUCED_AT_THIS_HEAD — every labelled control's centre hits itself"
        : `REPRODUCED: ${overlapping.map((row) => `${row.label}(${row.role}) ${row.box.join("x")} centre hits <${row.hitTag}> "${row.hitText}"`).join(" | ")}`,
  };
  writeFileSync(evidencePath("ord0261-overlap-readonly.json"), JSON.stringify(summary, null, 1));
  console.log("ORD0261_VERDICT:", summary.verdict);
});
