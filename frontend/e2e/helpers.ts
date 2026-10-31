import type { Page } from "@playwright/test";

export const DEMO_NAME = "Demo Industries Ltd";

/** Collects console errors and uncaught page errors. */
export function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  return errors;
}

export async function demoUrls(page: Page): Promise<{ scorecard: string; thread: string }> {
  await page.goto("/companies");
  const scorecard = await page.getByRole("link", { name: DEMO_NAME, exact: true }).getAttribute("href");
  if (!scorecard) throw new Error("Demo company not listed; run backend/scripts/seed_demo.py");
  await page.goto(scorecard);
  const thread = await page
    .getByRole("table")
    .first()
    .getByRole("link", { name: /^Line 4 commissioning/ })
    .getAttribute("href");
  if (!thread) throw new Error("Delayed plant thread not in the ledger");
  return { scorecard, thread };
}
