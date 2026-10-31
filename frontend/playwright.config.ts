import { defineConfig } from "@playwright/test";

// Runs against an already running frontend (npm run build && npm start) backed by the
// API with the demo seeded. Uses the installed Google Chrome, so no browser download.
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    channel: process.env.E2E_CHANNEL ?? "chrome",
    trace: "off",
  },
});
