// Lighthouse on a running production build (npm run build && npm start), mobile and
// desktop presets. Prints the category scores; full HTML reports go to
// test-results/lighthouse/. Usage: npm run lighthouse [-- <url>]
import { mkdirSync, writeFileSync } from "node:fs";
import * as chromeLauncher from "chrome-launcher";
import lighthouse from "lighthouse";
import desktopConfig from "lighthouse/core/config/desktop-config.js";

const url = process.argv[2] ?? `${process.env.E2E_BASE_URL ?? "http://localhost:3000"}/`;
const categories = ["performance", "accessibility", "best-practices", "seo"];
const out = "test-results/lighthouse";
mkdirSync(out, { recursive: true });

const chrome = await chromeLauncher.launch({ chromeFlags: ["--headless=new"] });
try {
  for (const [name, config] of [["mobile", undefined], ["desktop", desktopConfig]]) {
    const result = await lighthouse(
      url,
      { port: chrome.port, onlyCategories: categories, output: "html", logLevel: "error" },
      config,
    );
    writeFileSync(`${out}/home-${name}.html`, result.report);
    const lhr = result.lhr;
    const scores = categories.map((c) => `${c} ${Math.round(lhr.categories[c].score * 100)}`).join(" · ");
    const metric = (id) => lhr.audits[id]?.displayValue ?? "—";
    console.log(`${name.padEnd(7)} ${scores}`);
    console.log(
      `        LCP ${metric("largest-contentful-paint")} · TBT ${metric("total-blocking-time")} · CLS ${metric("cumulative-layout-shift")} · FCP ${metric("first-contentful-paint")}`,
    );
    for (const c of ["performance", "accessibility"]) {
      const failing = lhr.categories[c].auditRefs
        .map((ref) => lhr.audits[ref.id])
        .filter((a) => a.score !== null && a.score < 0.9 && a.scoreDisplayMode !== "informative" && a.scoreDisplayMode !== "notApplicable");
      for (const a of failing) console.log(`        ${c}: ${a.id} (${a.score}) ${a.displayValue ?? ""}`);
    }
  }
} finally {
  chrome.kill();
}
