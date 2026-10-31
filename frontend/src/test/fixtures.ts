import type { LedgerCell, LedgerRow, Scorecard, ThreadDetail, ThreadMention } from "@/lib/api";

const PERIODS = ["Q1FY26", "Q2FY26", "Q3FY26", "Q4FY26"];

export function cells(kinds: (LedgerCell["kind"] | [LedgerCell["kind"], string])[]): LedgerCell[] {
  return kinds.map((k, i) => {
    const [kind, text] = Array.isArray(k) ? k : [k, ""];
    return { period: PERIODS[i] ?? `Q${i}`, kind, text };
  });
}

let next = 0;
export function row(overrides: Partial<LedgerRow> = {}): LedgerRow {
  next += 1;
  return {
    thread_id: `thread-${next}`,
    title: "EBITDA margin 18–19% in FY26",
    metric_normalized: "ebitda_margin",
    unit: "percent",
    verdict: "delivered",
    kept_after_lowering: false,
    first_seen_period: "Q1FY26",
    first_target_period: "FY26",
    target_period: "FY26",
    slipped_quarters: 0,
    mention_count: 2,
    comparison: null,
    cells: cells(["said", "said", "silent", ["kept", "18.5%"]]),
    ...overrides,
  };
}

export function scorecard(overrides: Partial<Scorecard> = {}): Scorecard {
  return {
    company_id: "c1",
    as_of_period: "Q4FY26",
    threads_total: 0,
    not_verified: 0,
    counts: {},
    say_do: { delivered: 0, denominator: 0, ratio: null, low_sample: true },
    say_do_latest: { kept: 0, total: 0, ratio: null },
    say_do_original: { kept: 0, total: 0, ratio: null },
    by_metric: [],
    guidance_behavior: {
      counts: {},
      revisions: 0,
      raised_share: null,
      lowered_share: null,
      delayed_share: null,
      threads_revised: 0,
    },
    top_misses: [],
    dropped: [],
    notes: [],
    ...overrides,
  };
}

export function mention(overrides: Partial<ThreadMention> = {}): ThreadMention {
  next += 1;
  return {
    claim_id: `claim-${next}`,
    document_id: "doc-1",
    period_label: "Q1FY26",
    change_type: "new",
    quote: "We expect this.",
    speaker: "Kavita Rao",
    page_number: 2,
    metric_raw: "PAT",
    claim_type: "numeric",
    target_low: null,
    target_high: null,
    unit: null,
    direction: "none",
    timeframe_raw: null,
    target_period: "FY26",
    ...overrides,
  };
}

export function thread(overrides: Partial<ThreadDetail> = {}): ThreadDetail {
  return {
    id: "t1",
    company_id: "c1",
    title: "PAT ₹400 crore in FY26",
    metric_normalized: "pat",
    target_fiscal_year: 2026,
    target_fiscal_quarter: null,
    target_period: "FY26",
    first_seen_period: "Q1FY26",
    last_seen_period: "Q3FY26",
    mention_count: 2,
    is_dropped: false,
    latest_change_type: null,
    status: "delivered",
    verification: null,
    created_at: "2026-10-31T00:00:00Z",
    updated_at: "2026-10-31T00:00:00Z",
    mentions: [],
    latest_verification: null,
    actual_used: null,
    baseline_used: null,
    evidence_chunks: [],
    explanation_chunks: [],
    ...overrides,
  };
}
