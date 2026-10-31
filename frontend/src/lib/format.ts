/** Number formatting: Indian digit grouping, units from the backend's unit field. */

const grouped = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
const oneDecimal = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 1 });

export function toNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

/** 4620 -> "4,620"; 1234567 -> "12,34,567"; 18.528 -> "18.53". */
export function formatNumber(value: number): string {
  return grouped.format(value).replace("-", "−");
}

/** A value in its unit: "₹4,620 cr", "₹32,000 lakh", "18.5%", "40,000 tonnes". */
export function formatValue(value: number, unit: string | null | undefined): string {
  switch (unit) {
    case "percent":
      return `${oneDecimal.format(value).replace("-", "−")}%`;
    case "inr_crore":
      return `₹${formatNumber(value)} cr`;
    case "inr_lakh":
      return `₹${formatNumber(value)} lakh`;
    case "inr_bn":
      return `₹${formatNumber(value)} bn`;
    case "usd_mn":
      return `US$${formatNumber(value)} mn`;
    case "bps":
      return `${formatNumber(value)} bps`;
    case "x":
      return `${formatNumber(value)}x`;
    case "tonnes":
      return `${formatNumber(value)} tonnes`;
    case "mw":
      return `${formatNumber(value)} MW`;
    case "units":
      return `${formatNumber(value)} units`;
    default:
      return formatNumber(value);
  }
}

type Kind = "range" | "at_least" | "at_most" | "exact";

/** "₹300–350 cr", "at least ₹400 cr", "18–19%". */
export function formatTarget(
  low: number | null,
  high: number | null,
  unit: string | null | undefined,
  kind?: Kind | null,
): string {
  if (low !== null && high !== null && low !== high) {
    const lowText = formatValue(low, unit);
    const highText = formatValue(high, unit);
    // Share the unit: "₹300–350 cr" rather than "₹300 cr–₹350 cr".
    const number = formatNumber(low);
    const [prefix, suffix] = lowText.split(number);
    if (prefix !== undefined && suffix !== undefined && highText.startsWith(prefix)) {
      return `${prefix}${number}–${highText.slice(prefix.length)}`;
    }
    return `${lowText}–${highText}`;
  }
  const value = low ?? high;
  if (value === null) return "no stated target";
  const text = formatValue(value, unit);
  if (kind === "at_least") return `at least ${text}`;
  if (kind === "at_most") return `at most ${text}`;
  return text;
}

/** A ratio as a percentage with at most one decimal: 0.625 -> "62.5%", 0.75 -> "75%". */
export function formatRatio(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined) return "—";
  return `${oneDecimal.format(ratio * 100)}%`;
}

/** The number part only, for the giant ratio (the % sign is set separately). */
export function ratioNumber(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined) return "—";
  return oneDecimal.format(ratio * 100);
}

/** A signed percentage: -7.6 -> "−7.6%", 2.5 -> "+2.5%". */
export function formatSignedPct(value: number): string {
  const text = oneDecimal.format(Math.abs(value));
  if (text === "0") return "0%";
  return `${value < 0 ? "−" : "+"}${text}%`;
}

const WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"];

/** Small counts as words in prose ("three quarters"), larger ones as numerals. */
export function countWord(n: number): string {
  return WORDS[n] ?? String(n);
}

export function plural(n: number, singular: string, pluralForm = `${singular}s`): string {
  return n === 1 ? singular : pluralForm;
}

/** "Q1FY26–Q4FY26", or a single label. */
export function periodRange(labels: string[]): string {
  if (labels.length === 0) return "";
  const first = labels[0]!;
  const last = labels[labels.length - 1]!;
  return first === last ? first : `${first}–${last}`;
}
