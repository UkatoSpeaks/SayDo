import { describe, expect, it } from "vitest";
import { row, scorecard } from "@/test/fixtures";
import type { CompanyListItem, Ledger, ThreadDetail } from "./api";
import {
  buildHomeData,
  FALLBACK_EXAMPLES,
  FALLBACK_LEDGER,
  FALLBACK_LINK_QUOTES,
  formatCount,
  HERO_FINAL,
  HERO_TIMING,
  heroExamples,
  heroShows,
  linkQuotes,
  nextHeroStep,
  orderCompanies,
  pickFeatured,
  type HeroStep,
} from "./home";

describe("hero cycle", () => {
  it("plays said → strike → did → stamp, then moves to the next example", () => {
    let step: HeroStep = { index: 0, phase: "said" };
    const seen: string[] = [];
    for (let i = 0; i < 9; i++) {
      seen.push(`${step.index}:${step.phase}`);
      step = nextHeroStep(step, 3);
    }
    expect(seen).toEqual([
      "0:said", "0:strike", "0:did", "0:stamp",
      "1:said", "1:strike", "1:did", "1:stamp",
      "2:said",
    ]);
  });

  it("wraps from the last example back to the first", () => {
    expect(nextHeroStep({ index: 2, phase: "stamp" }, 3)).toEqual({ index: 0, phase: "said" });
    expect(nextHeroStep({ index: 0, phase: "stamp" }, 1)).toEqual({ index: 0, phase: "said" });
  });

  it("holds each finished example for about four seconds", () => {
    expect(HERO_TIMING.stamp).toBe(4000);
    const cycle = Object.values(HERO_TIMING).reduce((a, b) => a + b, 0);
    expect(cycle).toBeGreaterThan(6000);
    expect(cycle).toBeLessThan(7500);
  });

  it("the final step shows every part of the first example", () => {
    expect(HERO_FINAL).toEqual({ index: 0, phase: "stamp" });
    expect(["strike", "did", "stamp"].every((p) => heroShows("stamp", p as "strike"))).toBe(true);
    expect(heroShows("said", "strike")).toBe(false);
    expect(heroShows("strike", "did")).toBe(false);
    expect(heroShows("did", "did")).toBe(true);
  });
});

describe("formatCount", () => {
  it("formats counter values with Indian grouping and fixed decimals", () => {
    expect(formatCount(4620)).toBe("4,620");
    expect(formatCount(0)).toBe("0");
    expect(formatCount(75)).toBe("75");
    expect(formatCount(62.5, 1)).toBe("62.5");
    expect(formatCount(123456)).toBe("1,23,456");
    expect(formatCount(1980.6)).toBe("1,981");
  });
});

const ledger = (rows: Ledger["rows"]): Ledger => ({
  company_id: "c1",
  as_of_period: "Q4FY26",
  periods: ["Q1FY26", "Q2FY26", "Q3FY26", "Q4FY26"],
  rows,
  long_term_hidden: 0,
});

const revenueMiss = row({
  title: "Revenue ₹5,000 crore in FY26",
  metric_normalized: "revenue",
  verdict: "missed",
  target_period: "FY26",
  comparison: {
    unit: "inr_crore",
    target_low: 5000,
    target_high: null,
    target_kind: "at_least",
    original_low: null,
    original_high: null,
    original_kind: null,
    actual: 4620,
    actual_period: "FY26",
    gap_pct: -7.6,
  },
});
const lineDelay = row({
  title: "Line 4 commissioning by Q1FY27",
  metric_normalized: "plant_commissioning",
  verdict: "in_progress",
  first_target_period: "Q2FY26",
  target_period: "Q1FY27",
  slipped_quarters: 3,
  comparison: null,
  cells: [
    { period: "Q1FY26", kind: "said", text: "Q2FY26" },
    { period: "Q2FY26", kind: "moved", text: "→ Q3FY26" },
    { period: "Q3FY26", kind: "moved", text: "→ Q1FY27" },
    { period: "Q4FY26", kind: "said", text: "Q1FY27" },
  ],
});
const patLowered = row({
  title: "PAT ₹400 crore in FY26",
  metric_normalized: "pat",
  verdict: "delivered",
  kept_after_lowering: true,
  target_period: "FY26",
  comparison: {
    unit: "inr_crore",
    target_low: 400,
    target_high: null,
    target_kind: "at_least",
    original_low: 450,
    original_high: null,
    original_kind: "at_least",
    actual: 410,
    actual_period: "FY26",
    gap_pct: null,
  },
});

describe("heroExamples", () => {
  it("builds the three examples from the ledger, matching the demo copy", () => {
    const [miss, delay, lowered] = heroExamples(ledger([lineDelay, revenueMiss, patLowered]));
    expect(miss).toEqual(FALLBACK_EXAMPLES[0]);
    expect(delay).toEqual(FALLBACK_EXAMPLES[1]);
    expect(lowered).toEqual(FALLBACK_EXAMPLES[2]);
  });

  it("falls back per example when the ledger lacks one", () => {
    const examples = heroExamples(ledger([revenueMiss]));
    expect(examples[0]!.did.count).toEqual({ value: 4620, decimals: 0 });
    expect(examples[1]).toBe(FALLBACK_EXAMPLES[1]);
    expect(examples[2]).toBe(FALLBACK_EXAMPLES[2]);
    expect(heroExamples(null)).toEqual(FALLBACK_EXAMPLES);
  });
});

describe("buildHomeData", () => {
  it("uses the static figures when the API is down", () => {
    const data = buildHomeData({ companies: null, featuredId: null, card: null, ledger: null, thread: null });
    expect(data.live).toBe(false);
    expect(data.ratio.percent).toBe(75);
    expect(data.ledger).toBe(FALLBACK_LEDGER);
    expect(data.ledger.rows).toHaveLength(5);
    expect(data.scorecardHref).toBe("/companies");
    expect(data.ticker.map((t) => `${t.label ?? ""} ${t.value}`.trim())).toEqual(
      expect.arrayContaining(["SAY-DO 75%", "KEPT 6", "MISSED 1", "DROPPED 1", "DEADLINES MOVED 2", "LINE 4: Q2FY26 → Q1FY27"]),
    );
    expect(data.bullet?.actual).toBe("₹4,620 cr");
    expect(data.demoOnly).toBe(true);
  });

  it("uses the API figures when they are there", () => {
    const card = scorecard({
      counts: { delivered: 6, missed: 1, dropped: 1, in_progress: 2 },
      say_do_latest: { kept: 6, total: 8, ratio: 0.75 },
      say_do_original: { kept: 5, total: 8, ratio: 0.625 },
      guidance_behavior: { ...scorecard().guidance_behavior, counts: { delayed: 2 } },
    });
    const data = buildHomeData({
      companies: [],
      featuredId: "c1",
      card,
      ledger: ledger([lineDelay, revenueMiss, patLowered]),
      thread: null,
    });
    expect(data.live).toBe(true);
    expect(data.scorecardHref).toBe("/companies/c1");
    expect(data.ratio).toMatchObject({ percent: 75, decimals: 0, open: 2, lower: true });
    expect(data.ratio.original.text).toBe("62.5%");
    const ticker = data.ticker.map((t) => `${t.label ?? ""} ${t.value}`.trim());
    expect(ticker).toContain("LINE 4 COMMISSIONING: Q2FY26 → Q1FY27");
    expect(ticker).toContain("REVENUE FY26: ₹4,620 CR VS ₹5,000 CR");
    expect(ticker).toContain("PAT FY26: ₹410 CR · TARGET LOWERED ₹450 → ₹400 CR");
    expect(data.ledger.rows).toHaveLength(3);
  });

  it("gives a ratio with a decimal one decimal place", () => {
    const card = scorecard({ say_do_latest: { kept: 5, total: 8, ratio: 0.625 } });
    const data = buildHomeData({ companies: [], featuredId: "c1", card, ledger: null, thread: null });
    expect(data.ratio).toMatchObject({ percent: 62.5, decimals: 1 });
  });
});

describe("linkQuotes", () => {
  const mention = (period: string, metric: string, quote: string) => ({
    claim_id: period,
    document_id: "d",
    period_label: period,
    change_type: null,
    quote,
    speaker: "Arjun Mehta",
    page_number: 2,
    metric_raw: metric,
    claim_type: "qualitative",
    target_low: null,
    target_high: null,
    unit: null,
    direction: "none",
    timeframe_raw: null,
    target_period: "Q1FY27",
  });

  it("pairs the first mention with the first one under another name", () => {
    const thread = {
      mentions: [
        mention("Q1FY26", "Line 4 commissioning", "We will commission Line 4 in Q2 FY26."),
        mention("Q2FY26", "Line 4 commissioning", "Line 4 will now be commissioned in Q3 FY26."),
        mention("Q3FY26", "Pune expansion commissioning", "We now expect the Pune expansion to be commissioned in Q1 FY27, as clearance is pending."),
      ],
    } as unknown as ThreadDetail;
    const [a, b] = linkQuotes(thread);
    expect(a.period).toBe("Q1FY26");
    expect(b.period).toBe("Q3FY26");
    expect(b.quote).toBe("We now expect the Pune expansion to be commissioned in Q1 FY27, as clearance is pending.");
  });

  it("falls back without a thread", () => {
    expect(linkQuotes(null)).toBe(FALLBACK_LINK_QUOTES);
  });
});

const company = (id: string, symbol: string, total: number): CompanyListItem => ({
  id,
  name: `${symbol} Ltd`,
  nse_symbol: symbol,
  bse_code: null,
  sector: null,
  created_at: "2026-10-31T00:00:00Z",
  updated_at: "2026-10-31T00:00:00Z",
  say_do_latest: { kept: 0, total, ratio: total ? 0 : null },
  open_count: 0,
  latest_period: "Q2FY27",
});

const growthKept = row({
  title: "Revenue growth 2–3% in FY26",
  metric_normalized: "revenue_growth",
  verdict: "delivered",
  target_period: "FY26",
  comparison: {
    unit: "percent",
    target_low: 2,
    target_high: 3,
    target_kind: "range",
    original_low: null,
    original_high: null,
    original_kind: null,
    actual: 2.9,
    actual_period: "FY26",
    gap_pct: null,
  },
});

describe("featured company", () => {
  const demo = company("d", "DEMOIND", 8);

  it("features the real company with the most decided promises", () => {
    const a = company("a", "INFY", 3);
    const b = company("b", "HCLTECH", 5);
    expect(pickFeatured([demo, a, b])?.id).toBe("b");
    expect(pickFeatured([demo, a, company("c", "TCS", 3)])?.id).toBe("a");
  });

  it("falls back to the demo when no real company has a verdict", () => {
    expect(pickFeatured([company("a", "INFY", 0), demo])?.id).toBe("d");
    expect(pickFeatured([company("a", "INFY", 0)])?.id).toBe("a");
    expect(pickFeatured([])).toBeNull();
  });

  it("lists the demo last", () => {
    const a = company("a", "INFY", 3);
    const b = company("b", "HCLTECH", 0);
    expect(orderCompanies([demo, a, b]).map((c) => c.id)).toEqual(["a", "b", "d"]);
  });

  it("shows only a real company's own hero examples", () => {
    const examples = heroExamples(ledger([growthKept]), false);
    expect(examples).toHaveLength(1);
    expect(examples[0]).toMatchObject({
      key: "kept",
      caption: "Revenue growth · FY26",
      said: { struck: "2–3%" },
      did: { count: { value: 2.9, decimals: 1 } },
      stamp: { text: "KEPT", tone: "kept" },
    });
    expect(heroExamples(ledger([]), false)).toEqual([]);
  });

  it("builds the page around the real company, with the demo listed last", () => {
    const infy = company("a", "INFY", 2);
    const data = buildHomeData({
      companies: [demo, infy],
      featuredId: "a",
      card: scorecard({ say_do_latest: { kept: 1, total: 2, ratio: 0.5 } }),
      ledger: ledger([growthKept]),
      thread: null,
    });
    expect(data.featured).toEqual({ name: "INFY Ltd", symbol: "INFY", demo: false });
    expect(data.ticker[0]).toEqual({ value: "INFY" });
    expect(data.examples.map((e) => e.key)).toEqual(["kept"]);
    expect(data.companies.map((c) => c.id)).toEqual(["a", "d"]);
    expect(data.demoOnly).toBe(false);
    expect(data.scorecardHref).toBe("/companies/a");
    // No delayed thread: the linking example is the demo's.
    expect(data.howItWorksDemo).toBe("all");
    expect(data.bullet?.label).toContain("Revenue growth");
  });

  it("marks the demo as featured when it is", () => {
    const data = buildHomeData({
      companies: [demo],
      featuredId: "d",
      card: scorecard(),
      ledger: ledger([revenueMiss]),
      thread: null,
    });
    expect(data.featured.demo).toBe(true);
    expect(data.howItWorksDemo).toBe(false);
  });
});
