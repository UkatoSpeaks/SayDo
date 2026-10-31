import type { ChangeType, LedgerCellKind, VerificationStatus } from "./api";

/** Colour family per status. Every use also shows a text label or glyph. */
export type Tone = "kept" | "missed" | "moved" | "open" | "muted" | "ink";

export const TONE_TEXT: Record<Tone, string> = {
  kept: "text-kept",
  missed: "text-missed",
  moved: "text-moved",
  open: "text-open",
  muted: "text-muted",
  ink: "text-ink",
};

export const TONE_BG: Record<Tone, string> = {
  kept: "bg-kept",
  missed: "bg-missed",
  moved: "bg-moved",
  open: "bg-open",
  muted: "bg-muted",
  ink: "bg-ink",
};

export const TONE_TINT: Record<Tone, string> = {
  kept: "bg-kept-tint",
  missed: "bg-missed-tint",
  moved: "bg-moved-tint",
  open: "bg-open-tint",
  muted: "",
  ink: "",
};

export const TONE_BORDER: Record<Tone, string> = {
  kept: "border-kept",
  missed: "border-missed",
  moved: "border-moved",
  open: "border-open",
  muted: "border-muted",
  ink: "border-ink",
};

export interface Verdict {
  label: string;
  tone: Tone;
}

export function verdictOf(
  status: VerificationStatus | null | undefined,
  keptAfterLowering = false,
): Verdict {
  switch (status) {
    case "delivered":
      return keptAfterLowering
        ? { label: "Kept · lowered", tone: "kept" }
        : { label: "Kept", tone: "kept" };
    case "missed":
      return { label: "Missed", tone: "missed" };
    case "dropped":
      return { label: "Dropped", tone: "moved" };
    case "in_progress":
      return { label: "Open", tone: "open" };
    case "unverifiable":
      return { label: "Unverifiable", tone: "muted" };
    default:
      return { label: "Not verified", tone: "muted" };
  }
}

export interface CellStyle {
  glyph: string;
  label: string;
  tone: Tone;
  tinted: boolean;
}

/** Ledger cell glyphs (the legend uses the same table). */
export const CELL_STYLES: Record<LedgerCellKind, CellStyle> = {
  said: { glyph: "●", label: "said", tone: "ink", tinted: false },
  raised: { glyph: "▲", label: "raised", tone: "kept", tinted: false },
  lowered: { glyph: "▼", label: "lowered", tone: "missed", tinted: false },
  moved: { glyph: "→", label: "deadline moved", tone: "moved", tinted: true },
  kept: { glyph: "✓", label: "kept", tone: "kept", tinted: true },
  missed: { glyph: "✕", label: "missed", tone: "missed", tinted: true },
  silent: { glyph: "·", label: "not mentioned", tone: "muted", tinted: false },
  open: { glyph: "○", label: "open, due", tone: "open", tinted: true },
  none: { glyph: "", label: "", tone: "muted", tinted: false },
};

export const LEGEND: LedgerCellKind[] = [
  "said",
  "raised",
  "lowered",
  "moved",
  "kept",
  "missed",
  "open",
  "silent",
];

/** How a mention changed the promise, as a short uppercase tag. */
export function changeTag(change: ChangeType | null | undefined): Verdict {
  switch (change) {
    case "raised":
      return { label: "Raised", tone: "kept" };
    case "lowered":
      return { label: "Lowered", tone: "missed" };
    case "delayed":
      return { label: "Deadline moved", tone: "moved" };
    case "pulled_forward":
      return { label: "Brought forward", tone: "moved" };
    case "narrowed":
      return { label: "Narrowed", tone: "ink" };
    case "widened":
      return { label: "Widened", tone: "ink" };
    case "reiterated":
      return { label: "Reiterated", tone: "muted" };
    case "new":
      return { label: "First said", tone: "ink" };
    default:
      return { label: "Said", tone: "muted" };
  }
}

const METRIC_LABELS: Record<string, string> = {
  revenue: "Revenue",
  revenue_growth: "Revenue growth",
  ebitda: "EBITDA",
  ebitda_margin: "EBITDA margin",
  gross_margin: "Gross margin",
  pat: "PAT",
  pat_margin: "PAT margin",
  capex: "Capex",
  capacity: "Capacity",
  capacity_utilization: "Capacity utilization",
  volume_growth: "Volume growth",
  debt: "Debt",
  net_debt: "Net debt",
  order_book: "Order book",
  market_share: "Market share",
  store_count: "Store count",
  dividend_payout: "Dividend payout",
  cost_reduction: "Cost reduction",
};

/** Named promises (a plant, a launch) read better by their title. */
const NAMED_METRICS = new Set(["plant_commissioning", "product_launch", "other"]);

/** "Line 4 commissioning by Q1FY27" -> "Line 4 commissioning". */
export function stripPeriod(title: string): string {
  return title.replace(/\s+(?:by|in|for)\s+(?:H[12]\s?|Q[1-4]\s?)?FY\s?\d{2,4}$/i, "").trim();
}

/** What a sentence should call the promise: "PAT", "Line 4 commissioning". */
export function promiseNoun(title: string, metric: string | null | undefined): string {
  if (metric && !NAMED_METRICS.has(metric) && METRIC_LABELS[metric]) return METRIC_LABELS[metric];
  return stripPeriod(title);
}

export function metricLabel(metric: string | null | undefined): string {
  if (!metric) return "Claim";
  return METRIC_LABELS[metric] ?? metric.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}
