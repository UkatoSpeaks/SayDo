/** The landing page's data: built from the featured company's API data (the real company
 * with the most decided promises, else the demo), with static fallback figures when the
 * API is down. Pure, so it can be tested without a server. */

import type { CompanyListItem, Ledger, LedgerRow, Scorecard, ThreadDetail } from "./api";
import { compareRows, type CompareRow } from "./compare";
import { formatNumber, formatRatio, formatSignedPct, formatValue } from "./format";
import { metricLabel, stripPeriod, type Tone } from "./status";
import type { TickerItem } from "@/components/Ticker";

// ---------------------------------------------------------------------------
// Hero examples and their cycle

/** A number in the hero's second line that counts up from 0. */
export interface CountSpec {
  value: number;
  decimals: number;
}

export interface HeroExample {
  key: string;
  /** Small mono caption above the two lines: "Revenue · FY26". */
  caption: string;
  /** Line 1: before + struck + after, e.g. "They said: " "₹5,000 crore" " revenue in FY26." */
  said: { before: string; struck: string; after: string };
  /** Line 2: before + counted number + after. */
  did: { before: string; count: CountSpec | null; after: string };
  stamp: { text: string; tone: Tone };
}

export type HeroPhase = "said" | "strike" | "did" | "stamp";
export interface HeroStep {
  index: number;
  phase: HeroPhase;
}

const PHASES: HeroPhase[] = ["said", "strike", "did", "stamp"];

/** How long each phase lasts before the next one starts, in ms. The strike draws in
 * ~600ms, the count runs ~800ms, and the finished example holds for ~4s. */
export const HERO_TIMING: Record<HeroPhase, number> = {
  said: 700,
  strike: 650,
  did: 1000,
  stamp: 4000,
};

/** The final state of the first example: what the server renders and what reduced
 * motion shows. */
export const HERO_FINAL: HeroStep = { index: 0, phase: "stamp" };

export function nextHeroStep(step: HeroStep, count: number): HeroStep {
  const at = PHASES.indexOf(step.phase);
  if (at < PHASES.length - 1) return { index: step.index, phase: PHASES[at + 1]! };
  return { index: count > 0 ? (step.index + 1) % count : 0, phase: "said" };
}

/** Whether an element of the example is shown in this phase. */
export function heroShows(phase: HeroPhase, part: "strike" | "did" | "stamp"): boolean {
  return PHASES.indexOf(phase) >= PHASES.indexOf(part);
}

/** en-IN grouping with a fixed number of decimals: (4620, 0) -> "4,620". */
export function formatCount(value: number, decimals = 0): string {
  return new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

function decimalsOf(value: number): number {
  const text = formatNumber(value);
  const dot = text.indexOf(".");
  return dot === -1 ? 0 : text.length - dot - 1;
}

/** Money is written out on the hero ("₹5,000 crore"); other units use the short form. */
function spoken(value: number, unit: string | null | undefined): string {
  if (unit === "inr_crore") return `₹${formatNumber(value)} crore`;
  return formatValue(value, unit);
}

/** Split a value's text around its number, for the counter: "₹410 crore" -> ["₹", " crore"]. */
function aroundNumber(value: number, unit: string | null | undefined): [string, string] {
  const text = spoken(value, unit);
  const number = formatNumber(value);
  const at = text.indexOf(number);
  if (at === -1) return ["", ""];
  return [text.slice(0, at), text.slice(at + number.length)];
}

const TIMES = ["", "once", "twice"];
const VERBS: Record<string, string> = {
  plant_commissioning: "commissioned",
  product_launch: "launched",
};

export const FALLBACK_EXAMPLES: HeroExample[] = [
  {
    key: "miss",
    caption: "Revenue · FY26",
    said: { before: "They said: ", struck: "₹5,000 crore", after: " revenue in FY26." },
    did: { before: "They did:  ₹", count: { value: 4620, decimals: 0 }, after: " crore." },
    stamp: { text: "−7.6% · MISSED", tone: "missed" },
  },
  {
    key: "delay",
    caption: "Line 4 commissioning",
    said: { before: "They said: commissioned in ", struck: "Q2FY26", after: "." },
    did: { before: "Now:       Q1FY27, moved twice.", count: null, after: "" },
    stamp: { text: "DEADLINE MOVED", tone: "moved" },
  },
  {
    key: "lowered",
    caption: "PAT · FY26",
    said: { before: "They said: ", struck: "₹450 crore", after: "." },
    did: {
      before: "Lowered to ₹400 crore, then did ₹",
      count: { value: 410, decimals: 0 },
      after: " crore.",
    },
    stamp: { text: "KEPT · LOWERED", tone: "kept" },
  },
];

function missExample(rows: LedgerRow[]): HeroExample | null {
  const row = rows.find((r) => r.verdict === "missed" && r.comparison && r.comparison.target_low !== null);
  const c = row?.comparison;
  if (!row || !c || c.target_low === null) return null;
  const [pre, post] = aroundNumber(c.actual, c.unit);
  const metric = metricLabel(row.metric_normalized);
  const period = row.target_period ?? c.actual_period;
  return {
    key: "miss",
    caption: `${metric} · ${period}`,
    said: {
      before: "They said: ",
      struck: spoken(c.target_low, c.unit),
      after: ` ${metric.toLowerCase()} in ${period}.`,
    },
    did: { before: `They did:  ${pre}`, count: { value: c.actual, decimals: decimalsOf(c.actual) }, after: `${post}.` },
    stamp: {
      text: `${c.gap_pct !== null ? `${formatSignedPct(c.gap_pct)} · ` : ""}MISSED`,
      tone: "missed",
    },
  };
}

function delayExample(rows: LedgerRow[]): HeroExample | null {
  const row = rows.find((r) => r.slipped_quarters > 0 && r.first_target_period && r.target_period);
  if (!row?.first_target_period || !row.target_period) return null;
  const moves = row.cells.filter((cell) => cell.kind === "moved").length;
  const times = TIMES[moves] ?? `${moves} times`;
  const verb = VERBS[row.metric_normalized ?? ""] ?? "due";
  return {
    key: "delay",
    caption: stripPeriod(row.title),
    said: { before: `They said: ${verb} in `, struck: row.first_target_period, after: "." },
    did: {
      before: `Now:       ${row.target_period}${moves ? `, moved ${times}` : ""}.`,
      count: null,
      after: "",
    },
    stamp: { text: "DEADLINE MOVED", tone: "moved" },
  };
}

function loweredExample(rows: LedgerRow[]): HeroExample | null {
  const row = rows.find((r) => r.kept_after_lowering && r.comparison?.original_low != null);
  const c = row?.comparison;
  if (!row || !c || c.original_low == null || c.target_low === null) return null;
  const [pre, post] = aroundNumber(c.actual, c.unit);
  return {
    key: "lowered",
    caption: `${metricLabel(row.metric_normalized)} · ${row.target_period ?? c.actual_period}`,
    said: { before: "They said: ", struck: spoken(c.original_low, c.unit), after: "." },
    did: {
      before: `Lowered to ${spoken(c.target_low, c.unit)}, then did ${pre}`,
      count: { value: c.actual, decimals: decimalsOf(c.actual) },
      after: `${post}.`,
    },
    stamp: { text: "KEPT · LOWERED", tone: "kept" },
  };
}

function keptExample(rows: LedgerRow[]): HeroExample | null {
  const row = rows.find(
    (r) => r.verdict === "delivered" && !r.kept_after_lowering && r.comparison && r.comparison.target_low !== null,
  );
  const c = row?.comparison;
  if (!row || !c || c.target_low === null) return null;
  const [pre, post] = aroundNumber(c.actual, c.unit);
  const metric = metricLabel(row.metric_normalized);
  const period = row.target_period ?? c.actual_period;
  const target =
    c.target_high !== null && c.target_high !== c.target_low
      ? `${formatNumber(c.target_low)}–${spoken(c.target_high, c.unit)}`
      : spoken(c.target_low, c.unit);
  return {
    key: "kept",
    caption: `${metric} · ${period}`,
    said: { before: "They said: ", struck: target, after: ` ${metric.toLowerCase()} in ${period}.` },
    did: { before: `They did:  ${pre}`, count: { value: c.actual, decimals: decimalsOf(c.actual) }, after: `${post}.` },
    stamp: { text: "KEPT", tone: "kept" },
  };
}

/** The hero examples from the ledger. For the demo, any that can't be built use the
 * fallback copy; a real company only shows its own examples (possibly none). */
export function heroExamples(ledger: Ledger | null, demo = true): HeroExample[] {
  const rows = ledger?.rows ?? [];
  if (!demo) {
    return [missExample(rows), delayExample(rows), loweredExample(rows), keptExample(rows)].filter(
      (e): e is HeroExample => e !== null,
    );
  }
  return [
    missExample(rows) ?? FALLBACK_EXAMPLES[0]!,
    delayExample(rows) ?? FALLBACK_EXAMPLES[1]!,
    loweredExample(rows) ?? FALLBACK_EXAMPLES[2]!,
  ];
}

// ---------------------------------------------------------------------------
// Which company the page features

export function isDemo(company: Pick<CompanyListItem, "nse_symbol">): boolean {
  return company.nse_symbol === DEMO_SYMBOL;
}

/** Real companies first (in API order), the demo last. */
export function orderCompanies<T extends Pick<CompanyListItem, "nse_symbol">>(companies: T[]): T[] {
  return [...companies.filter((c) => !isDemo(c)), ...companies.filter(isDemo)];
}

/** The real company with the most decided promises (ties: the first listed); the demo
 * when no real company has a verdict yet; else the first company. */
export function pickFeatured(companies: CompanyListItem[]): CompanyListItem | null {
  let best: CompanyListItem | null = null;
  for (const c of companies) {
    if (isDemo(c) || c.say_do_latest.total === 0) continue;
    if (!best || c.say_do_latest.total > best.say_do_latest.total) best = c;
  }
  return best ?? companies.find(isDemo) ?? companies[0] ?? null;
}

// ---------------------------------------------------------------------------
// The rest of the page

export interface LinkQuote {
  period: string;
  page: number | null;
  speaker: string | null;
  quote: string;
}

export interface HomeRatio {
  /** The ratio in percent (75 for 0.75), or null when nothing is decided yet. */
  percent: number | null;
  decimals: number;
  latest: { kept: number; total: number };
  original: { kept: number; total: number; text: string };
  open: number;
  /** The original-target ratio is lower than the latest one. */
  lower: boolean;
}

export interface HomeData {
  /** False when the API could not be reached and fallback figures are shown. */
  live: boolean;
  /** The company the hero, ticker and ledger preview show. */
  featured: { name: string; symbol: string | null; demo: boolean };
  scorecardHref: string;
  ratio: HomeRatio;
  ticker: TickerItem[];
  examples: HeroExample[];
  ledger: Ledger;
  ledgerHref: string;
  bullet: CompareRow | null;
  linkQuotes: [LinkQuote, LinkQuote];
  /** With a real company featured: which how-it-works visuals show demo examples. Step 1's
   * transcript is always the demo's; steps 2-3 when the real ledger has nothing to show. */
  howItWorksDemo: false | "step1" | "all";
  /** Real companies first, the demo last. */
  companies: CompanyListItem[];
  demoOnly: boolean;
}

export interface HomeInput {
  companies: CompanyListItem[] | null;
  /** The company whose scorecard, ledger and thread were loaded. */
  featuredId: string | null;
  card: Scorecard | null;
  ledger: Ledger | null;
  thread: ThreadDetail | null;
}

const DEMO_SYMBOL = "DEMOIND";
const FALLBACK_PERIODS = ["Q1FY26", "Q2FY26", "Q3FY26", "Q4FY26", "Q1FY27", "Q2FY27", "Q3FY27", "Q4FY27"];

function fallbackRow(
  title: string,
  metric: string,
  verdict: LedgerRow["verdict"],
  cells: [LedgerRow["cells"][number]["kind"], string][],
  extra: Partial<LedgerRow> = {},
): LedgerRow {
  return {
    thread_id: `fallback-${metric}`,
    title,
    metric_normalized: metric,
    unit: null,
    verdict,
    kept_after_lowering: false,
    first_seen_period: "Q1FY26",
    first_target_period: null,
    target_period: null,
    slipped_quarters: 0,
    mention_count: 2,
    comparison: null,
    cells: FALLBACK_PERIODS.map((period, i) => {
      const [kind, text] = cells[i] ?? ["none", ""];
      return { period, kind, text };
    }),
    ...extra,
  };
}

/** The demo company's first five ledger rows, as seeded by backend/scripts/seed_demo.py. */
export const FALLBACK_LEDGER: Ledger = {
  company_id: "fallback",
  as_of_period: "Q4FY26",
  periods: FALLBACK_PERIODS,
  long_term_hidden: 0,
  rows: [
    fallbackRow(
      "Line 4 commissioning by Q1FY27",
      "plant_commissioning",
      "in_progress",
      [["said", "Q2FY26"], ["moved", "→ Q3FY26"], ["moved", "→ Q1FY27"], ["said", "Q1FY27"], ["open", "Q1FY27"]],
      { first_target_period: "Q2FY26", target_period: "Q1FY27", slipped_quarters: 3, mention_count: 4 },
    ),
    fallbackRow("EBITDA margin 19–20% in FY27", "ebitda_margin", "in_progress", [
      ["none", ""], ["none", ""], ["said", "19–20%"], ["said", "19–20%"],
      ["none", ""], ["none", ""], ["none", ""], ["open", "19–20%"],
    ]),
    fallbackRow(
      "Revenue ₹5,000 crore in FY26",
      "revenue",
      "missed",
      [["said", "5,000"], ["said", "5,000"], ["silent", ""], ["missed", "4,620"]],
      {
        unit: "inr_crore",
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
      },
    ),
    fallbackRow("Net debt ₹200 crore by FY27", "net_debt", "dropped", [
      ["said", "200"], ["said", "200"], ["silent", ""], ["silent", ""],
    ]),
    fallbackRow("Aura premium range launch by Q4FY26", "product_launch", "delivered", [
      ["said", "Q4FY26"], ["silent", ""], ["kept", ""],
    ]),
  ],
};

export const FALLBACK_LINK_QUOTES: [LinkQuote, LinkQuote] = [
  {
    period: "Q1FY26",
    page: 2,
    speaker: "Arjun Mehta",
    quote: "We will commission Line 4 in Q2 FY26, adding 40,000 tonnes of capacity.",
  },
  {
    period: "Q3FY26",
    page: 5,
    speaker: "Arjun Mehta",
    quote: "We now expect the Pune expansion to be commissioned in Q1 FY27.",
  },
];

const FALLBACK_RATIO: HomeRatio = {
  percent: 75,
  decimals: 0,
  latest: { kept: 6, total: 8 },
  original: { kept: 5, total: 8, text: "62.5%" },
  open: 2,
  lower: true,
};

const FALLBACK_TICKER: TickerItem[] = [
  { value: DEMO_SYMBOL },
  { label: "SAY-DO", value: "75%", numeric: true },
  { label: "KEPT", value: "6", numeric: true },
  { label: "MISSED", value: "1", numeric: true },
  { label: "DROPPED", value: "1", numeric: true },
  { label: "OPEN", value: "2", numeric: true },
  { label: "DEADLINES MOVED", value: "2", numeric: true },
  { label: "LINE 4:", value: "Q2FY26 → Q1FY27", numeric: true },
  { label: "REVENUE FY26:", value: "₹4,620 CR VS ₹5,000 CR", numeric: true },
  { label: "PAT FY26:", value: "₹410 CR · TARGET LOWERED ₹450 → ₹400 CR", numeric: true },
  { label: "ORIGINAL TARGETS", value: "62.5%", numeric: true },
  { label: "AS OF", value: "Q4FY26", numeric: true },
];

const FALLBACK_COMPANY: CompanyListItem = {
  id: "fallback",
  name: "Demo Industries Ltd",
  nse_symbol: DEMO_SYMBOL,
  bse_code: null,
  sector: "Industrials (fictional)",
  created_at: "2026-10-31T00:00:00Z",
  updated_at: "2026-10-31T00:00:00Z",
  say_do_latest: { kept: 6, total: 8, ratio: 0.75 },
  open_count: 2,
  latest_period: "Q4FY26",
};

function ratioFrom(card: Scorecard): HomeRatio {
  const latest = card.say_do_latest;
  const original = card.say_do_original;
  const percent = latest.ratio === null ? null : Math.round(latest.ratio * 1000) / 10;
  return {
    percent,
    decimals: percent !== null && percent % 1 !== 0 ? 1 : 0,
    latest: { kept: latest.kept, total: latest.total },
    original: { kept: original.kept, total: original.total, text: formatRatio(original.ratio) },
    open: card.counts.in_progress ?? 0,
    lower: original.ratio !== null && latest.ratio !== null && original.ratio < latest.ratio,
  };
}

const upper = (text: string) => text.toUpperCase();

/** The marquee: the scorecard's counts, then a few promises in one line each. */
export function tickerFrom(card: Scorecard, ledger: Ledger | null, symbol: string | null): TickerItem[] {
  const n = card.counts;
  const items: TickerItem[] = [
    { value: symbol ?? "DEMO" },
    { label: "SAY-DO", value: formatRatio(card.say_do_latest.ratio), numeric: true },
    { label: "KEPT", value: String(n.delivered ?? 0), numeric: true },
    { label: "MISSED", value: String(n.missed ?? 0), numeric: true },
    { label: "DROPPED", value: String(n.dropped ?? 0), numeric: true },
    { label: "OPEN", value: String(n.in_progress ?? 0), numeric: true },
    { label: "DEADLINES MOVED", value: String(card.guidance_behavior.counts.delayed ?? 0), numeric: true },
  ];
  for (const row of ledger?.rows ?? []) {
    const c = row.comparison;
    if (row.slipped_quarters > 0 && row.first_target_period && row.target_period) {
      items.push({
        label: `${upper(stripPeriod(row.title))}:`,
        value: `${row.first_target_period} → ${row.target_period}`,
        numeric: true,
      });
    } else if (c && row.verdict === "missed" && c.target_low !== null) {
      items.push({
        label: `${upper(metricLabel(row.metric_normalized))} ${row.target_period ?? c.actual_period}:`,
        value: upper(`${formatValue(c.actual, c.unit)} vs ${formatValue(c.target_low, c.unit)}`),
        numeric: true,
      });
    } else if (c && row.kept_after_lowering && c.original_low != null && c.target_low !== null) {
      items.push({
        label: `${upper(metricLabel(row.metric_normalized))} ${row.target_period ?? c.actual_period}:`,
        value: upper(
          `${formatValue(c.actual, c.unit)} · target lowered ${formatValue(c.original_low, c.unit).replace(/ cr$/, "")} → ${formatValue(c.target_low, c.unit)}`,
        ),
        numeric: true,
      });
    }
  }
  items.push({ label: "ORIGINAL TARGETS", value: formatRatio(card.say_do_original.ratio), numeric: true });
  items.push({ label: "AS OF", value: card.as_of_period ?? "—", numeric: true });
  return items;
}

/** Two mentions of one thread under different names ("Line 4", "the Pune expansion"). */
export function linkQuotes(thread: ThreadDetail | null): [LinkQuote, LinkQuote] {
  const mentions = thread?.mentions ?? [];
  const first = mentions[0];
  if (!first) return FALLBACK_LINK_QUOTES;
  const later = mentions.slice(1);
  // Prefer the first later mention under another name ("Pune expansion" for "Line 4"),
  // else the one sharing the fewest words with the first.
  const name = (m: typeof first) => (m.metric_raw ?? "").trim().toLowerCase();
  const words = (m: typeof first) => m.quote.toLowerCase().match(/[a-z0-9]+/g) ?? [];
  const firstWords = new Set(words(first));
  const renamed =
    later.find((m) => name(m) && name(first) && name(m) !== name(first)) ??
    later
      .map((m) => ({ m, overlap: words(m).filter((w) => firstWords.has(w)).length }))
      .sort((a, b) => a.overlap - b.overlap)[0]?.m;
  if (!renamed) return FALLBACK_LINK_QUOTES;
  const quote = (m: typeof first): LinkQuote => ({
    period: m.period_label,
    page: m.page_number,
    speaker: m.speaker,
    quote: firstSentence(m.quote),
  });
  return [quote(first), quote(renamed)];
}

function firstSentence(text: string): string {
  const match = text.match(/^.*?[.!?](?=\s|$)/);
  return (match ? match[0] : text).trim();
}

export function buildHomeData(input: HomeInput): HomeData {
  const { card, ledger, thread, featuredId } = input;
  const companies = orderCompanies(input.companies ?? [FALLBACK_COMPANY]);
  const live = Boolean(card && ledger && input.companies);
  const shown = (live ? companies.find((c) => c.id === featuredId) : null) ?? FALLBACK_COMPANY;
  const demo = isDemo(shown);
  const scorecardHref = featuredId && live ? `/companies/${featuredId}` : "/companies";
  const preview = ledger && ledger.rows.length > 0 ? { ...ledger, rows: ledger.rows.slice(0, 5) } : FALLBACK_LEDGER;
  const own = compareRows(ledger?.rows ?? []);
  const bullet = own.find((row) => row.tone === "missed") ?? own[0] ?? compareRows(FALLBACK_LEDGER.rows)[0] ?? null;
  const quotes = linkQuotes(thread);
  return {
    live,
    featured: { name: shown.name, symbol: shown.nse_symbol, demo },
    scorecardHref,
    ratio: card ? ratioFrom(card) : FALLBACK_RATIO,
    ticker: card ? tickerFrom(card, ledger, shown.nse_symbol) : FALLBACK_TICKER,
    examples: demo || !live ? heroExamples(ledger) : heroExamples(ledger, false),
    ledger: preview,
    ledgerHref: live ? `${scorecardHref}#ledger-title` : "/companies",
    bullet,
    linkQuotes: quotes,
    howItWorksDemo: demo ? false : quotes === FALLBACK_LINK_QUOTES || own.length === 0 ? "all" : "step1",
    companies,
    demoOnly: companies.length > 0 && companies.every(isDemo),
  };
}
