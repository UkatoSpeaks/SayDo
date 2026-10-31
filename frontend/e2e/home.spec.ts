import { expect, test, type Page } from "@playwright/test";
import { watchErrors } from "./helpers";

// The landing page against the seeded demo.

/** Scroll to the bottom in steps so every reveal and the pinned section run. */
async function scrollThrough(page: Page) {
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  for (let y = 0; y < height; y += 400) {
    await page.mouse.wheel(0, 400);
    await page.waitForTimeout(60);
  }
  await page.waitForTimeout(500);
}

test("home page loads and animates with no console errors", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/");
  await expect(page).toHaveTitle("SayDo — did management do what they said?");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "Management says a lot. SayDo checks what they did.",
  );
  await expect(page.getByRole("list", { name: "Key figures" })).toContainText("SAY-DO75%");
  // The hero cycles: the second example (the delayed plant) comes up after ~6.4s.
  await expect(page.locator('[data-slot="1"]')).toBeVisible({ timeout: 10_000 });
  await scrollThrough(page);
  await expect(page.getByRole("heading", { name: /How it works/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: /What SayDo will not do/ })).toBeVisible();
  await expect(page.getByText("Real companies are being added.")).toBeVisible();
  const ogImage = await page.locator('meta[property="og:image"]').getAttribute("content");
  expect(ogImage).toBeTruthy();
  // Absolute via metadataBase; fetch the path from the server under test.
  const og = await page.request.get(new URL(ogImage!).pathname + new URL(ogImage!).search);
  expect(og.status()).toBe(200);
  expect(og.headers()["content-type"]).toBe("image/png");
  expect(errors).toEqual([]);
});

test("the wordmark links home, and the ticker pauses for keyboard users", async ({ page }) => {
  await page.goto("/companies");
  await page.getByRole("link", { name: "SayDo", exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  const pause = page.getByRole("button", { name: "Pause ticker" });
  await pause.focus();
  const track = page.locator(".marquee-track");
  await expect(track).toHaveCSS("animation-play-state", "paused");
  await pause.press("Enter");
  await expect(page.getByRole("button", { name: "Play ticker" })).toBeVisible();
});

test("“See a live scorecard” opens the demo scorecard", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("link", { name: "See a live scorecard" }).first().click();
  await expect(page).toHaveURL(/\/companies\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "Management kept 6 of its 8 checkable promises.",
  );
});

test.describe("with reduced motion", () => {
  test.use({ reducedMotion: "reduce" });

  test("the hero shows the final strike-through state immediately", async ({ page }) => {
    const errors = watchErrors(page);
    await page.goto("/");
    const first = page.locator('[data-slot="0"]');
    await expect(first).toBeVisible();
    const strike = first.locator('[data-part="strike"]');
    // Full width, no transform: the line is drawn.
    const box = await strike.boundingBox();
    const word = await first.getByText("₹5,000 crore", { exact: true }).boundingBox();
    expect(box!.width).toBeGreaterThan(word!.width - 2);
    await expect(strike).toHaveCSS("transform", "none");
    await expect(first.locator('[data-part="did"]')).toHaveCSS("opacity", "1");
    await expect(first.locator('[data-part="count"]')).toHaveText("4,620");
    await expect(first.locator('[data-part="stamp"]')).toHaveText("−7.6% · MISSED");
    await expect(first.locator('[data-part="stamp"]')).toHaveCSS("opacity", "1");
    // No cycling, and the ticker is a static row without the duplicate copy.
    await page.waitForTimeout(7000);
    await expect(page.locator('[data-slot="1"]')).toBeHidden();
    await expect(page.locator(".marquee-track")).toHaveCSS("animation-name", "none");
    await expect(page.getByRole("button", { name: "Pause ticker" })).toBeHidden();
    // The big ratio is at its value, not counting.
    await expect(page.locator('[data-part="count"]').filter({ hasText: /^75$/ })).toBeVisible();
    expect(errors).toEqual([]);
  });
});
