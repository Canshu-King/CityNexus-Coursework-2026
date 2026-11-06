import { expect, type Page } from "@playwright/test";

/**
 * Open the stage the way a traveller does: press the floating ball.
 *
 * The widget used to be a `<details>` block inside Today, and the gates learned to open it instead
 * of assuming the panel was visible -- asserting against a collapsed disclosure just hung until the
 * timeout and read as a product failure. The shape changed (the traveller asked for a floating ball
 * that does not cover the screen); the discipline did not: every gate that presses a stage action
 * opens the window first, and proves it opened by reading the window rather than the ball.
 */
export async function openStageEntry(page: Page, reason: string, timeout = 30_000) {
  const ball = page.getByTestId("sim-ball");
  await expect(
    ball,
    `${reason}: there is no stage ball on this screen to press`,
  ).toBeVisible({ timeout });
  if (await page.getByTestId("sim-pop").count()) return;
  await ball.click();
  await expect(
    page.getByTestId("sim-pop"),
    `${reason}: the ball was pressed and no window appeared`,
  ).toBeVisible({ timeout });
}

/** Close it again, through the control the traveller would use. */
export async function closeStageEntry(page: Page, reason: string, timeout = 30_000) {
  await expect(
    page.getByTestId("sim-pop"),
    `${reason}: there is no open stage window to close`,
  ).toBeVisible({ timeout });
  await page.getByTestId("sim-ball").click();
  await expect
    .poll(() => page.getByTestId("sim-pop").count(), {
      timeout,
      intervals: [250],
      message: `${reason}: the ball was pressed again and the window never went away`,
    })
    .toBe(0);
}

/**
 * What the ball owes the reader without opening anything. It can no longer be a sentence -- a 48px
 * ball carrying prose would be the "blocks the screen" complaint again -- so the claim is carried by
 * the accessible name, and the dot only says whether a scenario is on stage.
 */
export async function stageDeclaration(page: Page) {
  const name = await page.getByTestId("sim-ball").getAttribute("aria-label");
  return String(name ?? "").replace(/\s+/g, " ").trim();
}

/** The same declaration as the window renders it in ink. */
export async function stageWindowDeclaration(page: Page) {
  const text = await page.getByTestId("sim-entry-decl").innerText();
  return text.replace(/\s+/g, " ").trim();
}
