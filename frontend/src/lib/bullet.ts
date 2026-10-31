/** Bullet-bar geometry: the promised band, the actual mark and the original target line,
 * as percentages of the track. Pure, so it can be tested without a DOM. */

export type TargetKind = "range" | "at_least" | "at_most" | "exact";

export interface BulletInput {
  low: number | null;
  high: number | null;
  kind: TargetKind;
  actual: number;
  /** The original target, drawn as a dashed line (only when it was lowered). */
  original?: number | null;
}

export interface BulletGeometry {
  domain: [number, number];
  /** Left edge and width of the promised band, in % of the track. */
  band: { left: number; width: number };
  actual: number;
  original: number | null;
}

const PADDING = 0.3; // of the span between the smallest and largest value
const EXACT_WIDTH = 1; // % of the track for a single-figure target

function clamp(value: number): number {
  return Math.min(100, Math.max(0, value));
}

export function bulletDomain(values: number[]): [number, number] {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const pad = span > 0 ? span * PADDING : Math.max(Math.abs(max) * 0.1, 1);
  let low = min - pad;
  const high = max + pad;
  // Don't invent negative room for quantities that can't go below zero here.
  if (min >= 0 && low < 0) low = 0;
  return [low, high];
}

export function bulletGeometry(input: BulletInput): BulletGeometry {
  const { low, high, kind, actual } = input;
  const original = input.original ?? null;
  const values = [actual, low, high, original].filter((v): v is number => v !== null);
  const domain = bulletDomain(values);
  const position = (value: number) => clamp(((value - domain[0]) / (domain[1] - domain[0])) * 100);

  let start: number;
  let end: number;
  if (kind === "at_least" && low !== null) {
    start = position(low);
    end = 100; // single-sided targets run to the end of the track
  } else if (kind === "at_most" && high !== null) {
    start = 0;
    end = position(high);
  } else if (low !== null && high !== null && low !== high) {
    start = position(low);
    end = position(high);
  } else {
    const centre = position((low ?? high)!);
    start = clamp(centre - EXACT_WIDTH / 2);
    end = clamp(centre + EXACT_WIDTH / 2);
  }
  return {
    domain,
    band: { left: start, width: Math.max(0, end - start) },
    actual: position(actual),
    original: original === null ? null : position(original),
  };
}
