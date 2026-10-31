import { expect, test } from "@playwright/test";
import { DEMO_NAME, watchErrors } from "./helpers";

// Against the seeded demo: companies list -> scorecard -> thread -> search, with no
// console errors. Does not run a search (that would embed a query).
test("companies, scorecard, thread and search render without console errors", async ({ page }) => {
  const errors = watchErrors(page);

  await page.goto("/companies");
  await expect(page.getByRole("heading", { level: 1, name: "Companies" })).toBeVisible();
  await page.getByRole("link", { name: DEMO_NAME, exact: true }).click();

  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "Management kept 6 of its 8 checkable promises.",
  );
  await expect(page.getByText("DEMO DATA")).toBeVisible();
  await expect(page.getByRole("heading", { name: /The ledger/ })).toBeVisible();
  await expect(page.getByText("Kept · lowered", { exact: true })).toBeVisible();
  await expect(page.getByRole("img", { name: /^PAT FY26: target/ })).toBeVisible();

  await page.getByRole("table").first().getByRole("link", { name: /^Line 4 commissioning/ }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "Line 4 commissioning was due in Q2FY26. It is now promised for Q1FY27.",
  );
  await expect(page.getByRole("heading", { name: /Where the deadline sat/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Report a wrong link" })).toBeVisible();
  const transcript = page.getByRole("link", { name: /Transcript, p\. \d/ }).first();
  await expect(transcript).toHaveAttribute("href", /\/documents\/[0-9a-f-]+\/file#page=\d+$/);

  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Search" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Ask the transcripts." })).toBeVisible();
  await expect(page.getByLabel("Company", { exact: true })).toHaveValue(/[0-9a-f-]{36}/);
  await expect(page.getByRole("button", { name: "Search", exact: true })).toBeVisible();

  expect(errors).toEqual([]);
});

test("the transcript link serves the PDF", async ({ page, request }) => {
  await page.goto("/companies");
  await page.getByRole("link", { name: DEMO_NAME, exact: true }).click();
  const href = await page.getByRole("link", { name: /^Q1FY26 call/ }).getAttribute("href");
  expect(href).toBeTruthy();
  const response = await request.get(href!);
  expect(response.status()).toBe(200);
  expect(response.headers()["content-type"]).toBe("application/pdf");
});
