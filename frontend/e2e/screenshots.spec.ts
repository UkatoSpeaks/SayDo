import { test } from "@playwright/test";
import { demoUrls, watchErrors } from "./helpers";

// Full-page screenshots of the four main pages at desktop and phone widths, saved to
// screenshots/ (git-ignored). E2E_RUN_SEARCH=1 also runs one search for the search
// page shot; only do that against a backend with EMBEDDING_PROVIDER=fake, or it embeds
// the query with the real API.
const WIDTHS = [1440, 390];

test("screenshots", async ({ page }) => {
  test.setTimeout(120_000);
  const errors = watchErrors(page);
  const { scorecard, thread } = await demoUrls(page);
  const pages: [string, string][] = [
    ["companies", "/companies"],
    ["scorecard", scorecard],
    ["thread", thread],
    ["search", `/search?company=${scorecard.split("/").pop()}`],
  ];
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 900 });
    for (const [name, url] of pages) {
      await page.goto(url);
      await page.waitForLoadState("networkidle");
      if (name === "search" && process.env.E2E_RUN_SEARCH === "1") {
        await page.getByLabel("Question").fill("Why was revenue below our guidance?");
        await page.getByRole("button", { name: "Search", exact: true }).click();
        await page.locator("mark").first().waitFor();
      }
      await page.screenshot({ path: `screenshots/${name}-${width}.png`, fullPage: true });
    }
  }
  if (errors.length) throw new Error(errors.join("\n"));
});

// The home page with reduced motion, so every animation shows its final state.
test.describe("home", () => {
  test.use({ reducedMotion: "reduce" });

  test("home screenshots", async ({ page }) => {
    const errors = watchErrors(page);
    for (const width of WIDTHS) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/");
      await page.waitForLoadState("networkidle");
      await page.screenshot({ path: `screenshots/home-${width}.png`, fullPage: true });
    }
    if (errors.length) throw new Error(errors.join("\n"));
  });
});
