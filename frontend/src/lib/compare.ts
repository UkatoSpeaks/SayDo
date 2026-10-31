/** Rows for "Target vs actual": one per numeric thread with an actual. */

import type { LedgerRow } from "./api";
import type { BulletInput } from "./bullet";
import { formatSignedPct, formatTarget, formatValue } from "./format";
import { metricLabel, verdictOf, type Tone } from "./status";

export interface CompareRow {
  threadId: string;
  label: string;
  input: BulletInput;
  target: string;
  original: string | null;
  actual: string;
  gap: string;
  tone: Tone;
  verdict: string;
  ariaLabel: string;
}

/** Gap to the bound that decides the verdict, in %; 0 inside a range. */
export function gapToTarget(input: BulletInput): number | null {
  const { low, high, kind, actual } = input;
  const relative = (bound: number) => (bound === 0 ? null : ((actual - bound) / Math.abs(bound)) * 100);
  if (kind === "at_least" && low !== null) return relative(low);
  if (kind === "at_most" && high !== null) return relative(high);
  if (low !== null && high !== null) {
    if (actual < low) return relative(low);
    if (actual > high) return relative(high);
    return 0;
  }
  return null;
}

export function compareRows(rows: LedgerRow[]): CompareRow[] {
  const out: CompareRow[] = [];
  for (const row of rows) {
    const c = row.comparison;
    if (!c) continue;
    const lowered = row.cells.some((cell) => cell.kind === "lowered");
    const originalValue =
      lowered && c.original_kind
        ? c.original_kind === "at_most"
          ? c.original_high
          : c.original_low
        : null;
    const input: BulletInput = {
      low: c.target_low,
      high: c.target_high,
      kind: c.target_kind,
      actual: c.actual,
      original: originalValue,
    };
    const verdict = verdictOf(row.verdict, row.kept_after_lowering);
    const target = formatTarget(c.target_low, c.target_high, c.unit, c.target_kind);
    const original =
      originalValue !== null ? formatTarget(c.original_low, c.original_high, c.unit, c.original_kind) : null;
    const actual = formatValue(c.actual, c.unit);
    const gapPct = row.verdict === "missed" && c.gap_pct !== null ? c.gap_pct : gapToTarget(input);
    const gap = gapPct === null ? "—" : gapPct === 0 ? "in range" : formatSignedPct(gapPct);
    const label = `${metricLabel(row.metric_normalized)} ${row.target_period ?? c.actual_period}`;
    out.push({
      threadId: row.thread_id,
      label,
      input,
      target,
      original,
      actual,
      gap,
      tone: verdict.tone,
      verdict: verdict.label,
      ariaLabel:
        `${label}: target ${target}` +
        (original ? `, lowered from ${original}` : "") +
        `; actual ${actual}; ${verdict.label.toLowerCase()}.`,
    });
  }
  return out;
}
