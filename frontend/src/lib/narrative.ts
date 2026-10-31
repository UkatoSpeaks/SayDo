/** Sentences written from the data, in code: headlines, decks, "what to read first". */

import type { LedgerRow, Scorecard, ThreadDetail, ThreadMention } from "./api";
import {
  countWord,
  formatSignedPct,
  formatTarget,
  formatValue,
  plural,
  toNumber,
} from "./format";
import { promiseNoun, stripPeriod, type Tone } from "./status";

const LOW_SAMPLE = 5;

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Lower-case the first letter mid-sentence, but keep acronyms ("PAT") and names
 * followed by a number or another capital ("Line 4", "Aura Premium"). */
export function lowerFirst(text: string): string {
  const [word = "", next = ""] = text.split(/\s+/);
  const isAcronym = word.length > 1 && word === word.toUpperCase();
  const isName = /^[0-9A-Z]/.test(next);
  if (isAcronym || isName) return text;
  return text.charAt(0).toLowerCase() + text.slice(1);
}

/** "a", "a and b", "a, b and c". */
export function joinClauses(parts: string[]): string {
  if (parts.length <= 1) return parts[0] ?? "";
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

function times(n: number): string {
  if (n === 1) return "once";
  if (n === 2) return "twice";
  return `${countWord(n)} times`;
}

function quarters(n: number): string {
  return `${countWord(n)} ${plural(n, "quarter")}`;
}

// ---------------------------------------------------------------- scorecard

export function scorecardHeadline(kept: number, total: number, threadsTotal: number): string {
  if (total === 0) {
    if (threadsTotal === 0) return "No promises have been tracked yet.";
    return `Management has made ${threadsTotal} checkable ${plural(threadsTotal, "promise")}; none has been decided yet.`;
  }
  if (kept === total) {
    return total === 1
      ? "Management kept its only decided promise."
      : `Management kept all ${total} of its checkable promises.`;
  }
  if (kept === 0) return `Management kept none of its ${total} checkable promises.`;
  return `Management kept ${kept} of its ${total} checkable promises.`;
}

function hasCell(row: LedgerRow, kind: string): boolean {
  return row.cells.some((cell) => cell.kind === kind);
}

/** The thread whose deadline moved the most, if any moved. */
export function mostDelayed(rows: LedgerRow[]): LedgerRow | null {
  let best: LedgerRow | null = null;
  for (const row of rows) {
    if (row.slipped_quarters > 0 && (!best || row.slipped_quarters > best.slipped_quarters)) {
      best = row;
    }
  }
  return best;
}

function delayClause(row: LedgerRow): string {
  const noun = stripPeriod(row.title);
  const n = row.slipped_quarters;
  const span =
    row.first_target_period && row.target_period
      ? `, from ${row.first_target_period} to ${row.target_period}`
      : "";
  if (row.verdict === "delivered") return `${lowerFirst(noun)} arrived ${quarters(n)} late`;
  if (row.verdict === "in_progress") return `${lowerFirst(noun)} has slipped ${quarters(n)}${span}`;
  return `the deadline for ${lowerFirst(noun)} moved ${quarters(n)}${span}`;
}

export function scorecardDeck(rows: LedgerRow[], decided: number): string {
  const parts: string[] = [];

  const lowered = rows.filter((row) => hasCell(row, "lowered"));
  const keptAfter = lowered.filter((row) => row.kept_after_lowering).length;
  if (lowered.length === 1) {
    parts.push(keptAfter ? "one target was lowered before it was met" : "one target was lowered");
  } else if (lowered.length > 1) {
    const n = countWord(lowered.length);
    if (keptAfter === lowered.length) parts.push(`${n} targets were lowered before they were met`);
    else if (keptAfter > 0) parts.push(`${n} targets were lowered (${countWord(keptAfter)} then met)`);
    else parts.push(`${n} targets were lowered`);
  }

  const dropped = rows.filter((row) => row.verdict === "dropped").length;
  if (dropped === 1) parts.push("one promise was quietly dropped");
  else if (dropped > 1) parts.push(`${countWord(dropped)} promises were quietly dropped`);

  const delayed = mostDelayed(rows);
  if (delayed) parts.push(delayClause(delayed));

  let deck: string;
  if (parts.length > 0) deck = `${capitalize(joinClauses(parts))}.`;
  else if (decided === 0) deck = "No promise has reached its deadline yet.";
  else deck = "No target was lowered, nothing was dropped and no deadline moved.";

  if (decided > 0 && decided < LOW_SAMPLE) {
    deck += ` With only ${decided} ${plural(decided, "promise")} decided, read the ratio as indicative.`;
  }
  return deck;
}

// --------------------------------------------------------- what to read first

export interface ReadFirstItem {
  key: "miss" | "dropped" | "goalposts";
  kicker: string;
  tone: Tone;
  headline: string;
  numbers: string;
  threadId: string;
}

function targetOf(row: LedgerRow): string | null {
  const c = row.comparison;
  if (!c) return null;
  return formatTarget(c.target_low, c.target_high, c.unit);
}

function biggestMiss(card: Scorecard, rows: LedgerRow[]): ReadFirstItem | null {
  const misses = card.top_misses;
  if (misses.length === 0) return null;
  const negative = misses
    .filter((m) => (toNumber(m.gap_pct) ?? 0) < 0)
    .sort((a, b) => (toNumber(a.gap_pct) ?? 0) - (toNumber(b.gap_pct) ?? 0));
  const miss = negative[0] ?? misses[0]!;
  const row = rows.find((r) => r.thread_id === miss.thread_id);
  const noun = promiseNoun(miss.title, miss.metric);
  const gap = toNumber(miss.gap_pct);
  const c = row?.comparison;
  if (row && c && gap !== null) {
    const target = targetOf(row)!;
    const actual = formatValue(c.actual, c.unit);
    const period = row.target_period ?? c.actual_period;
    return {
      key: "miss",
      kicker: "Biggest miss",
      tone: "missed",
      headline: `${noun} missed its ${period} target by ${formatSignedPct(Math.abs(gap)).slice(1)}: ${actual} against ${target}.`,
      numbers: `TARGET ${target} · ACTUAL ${actual} · GAP ${formatSignedPct(gap)}`,
      threadId: miss.thread_id,
    };
  }
  return {
    key: "miss",
    kicker: "Biggest miss",
    tone: "missed",
    headline: `${noun} was promised for ${miss.target_period ?? "a stated period"} and not delivered.`,
    numbers: `DUE ${miss.target_period ?? "—"}`,
    threadId: miss.thread_id,
  };
}

function quietlyDropped(card: Scorecard, rows: LedgerRow[]): ReadFirstItem | null {
  const dropped = card.dropped[0];
  if (!dropped) return null;
  const row = rows.find((r) => r.thread_id === dropped.thread_id);
  const last = dropped.last_seen_period ?? row?.first_seen_period ?? "—";
  const said = row?.mention_count ?? 1;
  return {
    key: "dropped",
    kicker: "Quietly dropped",
    tone: "moved",
    headline: `The promise of ${lowerFirst(dropped.title)} was last mentioned on the ${last} call, then never again.`,
    numbers: `SAID ${said}× · LAST ${last} · DUE ${dropped.target_period ?? "—"}`,
    threadId: dropped.thread_id,
  };
}

function goalposts(rows: LedgerRow[]): ReadFirstItem | null {
  const lowered = rows.find((r) => r.kept_after_lowering && r.comparison);
  if (lowered && lowered.comparison) {
    const c = lowered.comparison;
    const noun = promiseNoun(lowered.title, lowered.metric_normalized);
    const original =
      c.original_low !== null || c.original_high !== null
        ? formatTarget(c.original_low, c.original_high, c.unit)
        : null;
    const target = targetOf(lowered)!;
    const actual = formatValue(c.actual, c.unit);
    return {
      key: "goalposts",
      kicker: "Goalposts moved",
      tone: "moved",
      headline: original
        ? `${noun} guidance was cut from ${original} to ${target} before results came in at ${actual}.`
        : `${noun} guidance was lowered to ${target} before results came in at ${actual}.`,
      numbers: original
        ? `ORIGINAL ${original} · LOWERED TO ${target} · ACTUAL ${actual}`
        : `LOWERED TO ${target} · ACTUAL ${actual}`,
      threadId: lowered.thread_id,
    };
  }
  const delayed = mostDelayed(rows);
  if (!delayed) return null;
  const noun = stripPeriod(delayed.title);
  const moves = delayed.cells.filter((cell) => cell.kind === "moved").length;
  return {
    key: "goalposts",
    kicker: "Goalposts moved",
    tone: "moved",
    headline: `${noun} was first due in ${delayed.first_target_period}; the deadline is now ${delayed.target_period}.`,
    numbers: `SLIPPED ${delayed.slipped_quarters} ${plural(delayed.slipped_quarters, "QTR", "QTRS")} · MOVED ${moves}×`,
    threadId: delayed.thread_id,
  };
}

/** Up to three items; one is left out when there is nothing to say about it. */
export function readFirst(card: Scorecard, rows: LedgerRow[]): ReadFirstItem[] {
  return [biggestMiss(card, rows), quietlyDropped(card, rows), goalposts(rows)].filter(
    (item): item is ReadFirstItem => item !== null,
  );
}

// ------------------------------------------------------------------- thread

export interface Deadlines {
  first: string | null;
  current: string | null;
  delayed: boolean;
}

export function deadlines(thread: ThreadDetail): Deadlines {
  const first = thread.mentions.find((m) => m.target_period)?.target_period ?? null;
  const current = thread.target_period ?? null;
  const delayed =
    thread.mentions.some((m) => m.change_type === "delayed") &&
    first !== null &&
    current !== null &&
    first !== current;
  return { first, current, delayed };
}

function latestTarget(thread: ThreadDetail): ThreadMention | undefined {
  return [...thread.mentions].reverse().find((m) => m.target_low !== null);
}

function originalTarget(thread: ThreadDetail): ThreadMention | undefined {
  return thread.mentions.find((m) => m.target_low !== null);
}

export function mentionTarget(mention: ThreadMention): string {
  return formatTarget(toNumber(mention.target_low), toNumber(mention.target_high), mention.unit);
}

function actualText(thread: ThreadDetail, unit: string | null | undefined): string | null {
  const actual = thread.actual_used as { value?: string; unit?: string } | null;
  const value = toNumber(actual?.value ?? null);
  if (value === null) return null;
  const actualUnit = actual?.unit ?? unit;
  if (unit && actualUnit !== unit) {
    const converted = convertUnit(value, actualUnit ?? null, unit);
    if (converted !== null) return formatValue(converted, unit);
  }
  return formatValue(value, actualUnit);
}

const MONEY: Record<string, number> = { inr_lakh: 0.01, inr_crore: 1, inr_bn: 100 };

/** Lakh/crore/bn and bps/percent, as the backend converts them. */
export function convertUnit(value: number, from: string | null, to: string | null): number | null {
  if (!from || !to) return null;
  if (from === to) return value;
  if (from in MONEY && to in MONEY) return (value * MONEY[from]!) / MONEY[to]!;
  if (from === "bps" && to === "percent") return value / 100;
  if (from === "percent" && to === "bps") return value * 100;
  return null;
}

export function threadHeadline(thread: ThreadDetail): string {
  const noun = promiseNoun(thread.title, thread.metric_normalized);
  const { first, current, delayed } = deadlines(thread);
  if (delayed) {
    const title = stripPeriod(thread.title);
    if (thread.status === "delivered") {
      return `${title} was due in ${first}. It was delivered against the later deadline of ${current}.`;
    }
    return `${title} was due in ${first}. It is now promised for ${current}.`;
  }
  const latest = latestTarget(thread);
  const target = latest ? mentionTarget(latest) : null;
  const actual = actualText(thread, latest?.unit);
  const period = thread.target_period;
  const keptAfterLowering = thread.latest_verification?.kept_after_lowering ?? false;

  switch (thread.status) {
    case "delivered": {
      if (target && actual) {
        const original = originalTarget(thread);
        if (keptAfterLowering && original) {
          return `${noun} met its lowered target of ${target}, not the original ${mentionTarget(original)}.`;
        }
        return `${noun} came in at ${actual} against a target of ${target}.`;
      }
      if (actual) {
        const direction = thread.mentions[thread.mentions.length - 1]?.direction;
        const verb =
          direction === "increase" ? "rose" : direction === "decrease" ? "fell" : "held";
        const to = direction === "maintain" ? "at" : "to";
        return `${noun} ${verb} as promised, ${to} ${actual}${period ? ` in ${period}` : ""}.`;
      }
      return `${stripPeriod(thread.title)} was delivered as promised.`;
    }
    case "missed":
      if (target && actual) {
        return `${noun} fell short: ${actual} against the ${target} promised${period ? ` for ${period}` : ""}.`;
      }
      return `${stripPeriod(thread.title)} was not delivered${period ? ` by ${period}` : ""}.`;
    case "dropped":
      return `${stripPeriod(thread.title)} was promised, then no longer mentioned.`;
    case "in_progress":
      return `${stripPeriod(thread.title)} is still open${period ? `, due in ${period}` : ""}.`;
    case "unverifiable":
      return `${stripPeriod(thread.title)} cannot be checked yet.`;
    default:
      return `${stripPeriod(thread.title)} has not been verified yet.`;
  }
}

export function threadDeck(thread: ThreadDetail): string {
  const calls = new Set(thread.mentions.map((m) => m.period_label));
  const n = calls.size;
  let deck =
    n <= 1
      ? `Said once, on the ${thread.first_seen_period} call.`
      : `First said on the ${thread.first_seen_period} call and repeated on ${countWord(n - 1)} later ${plural(n - 1, "call")}, most recently ${thread.last_seen_period}.`;
  const count = (kind: string) => thread.mentions.filter((m) => m.change_type === kind).length;
  const changes: string[] = [];
  if (count("raised")) changes.push(`the target was raised ${times(count("raised"))}`);
  if (count("lowered")) changes.push(`the target was lowered ${times(count("lowered"))}`);
  if (count("delayed")) changes.push(`the deadline moved ${times(count("delayed"))}`);
  if (changes.length) deck += ` ${capitalize(joinClauses(changes))}.`;
  return deck;
}

const REASON_RE =
  /\b(?:reasons?|because|due to|impact(?:ed)?|delay(?:ed)?|pending|slipped|weak|pressures?|shortfall)\b/i;

/** The sentence giving management's reason, from the explanation excerpts. */
export function reasonSentence(text: string): string | null {
  const sentences = text.match(/[^.!?]+[.!?]+/g) ?? [text];
  const found = sentences.find((sentence) => REASON_RE.test(sentence));
  return found ? found.trim() : null;
}

/** Distinct names the mentions used, when linking joined different wordings. */
export function linkedNames(thread: ThreadDetail): string[] {
  const seen = new Map<string, string>();
  for (const mention of thread.mentions) {
    const key = mention.metric_raw.trim().toLowerCase();
    if (!seen.has(key)) seen.set(key, mention.metric_raw.trim());
  }
  return seen.size > 1 ? [...seen.values()] : [];
}
