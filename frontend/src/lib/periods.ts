/** Fiscal period labels as quarter ordinals (fiscal_year * 4 + quarter - 1), like the
 * backend. A full year ("FY27") ends at its Q4. */

export function periodOrdinal(label: string): number | null {
  const match = label.trim().toUpperCase().match(/^(?:Q([1-4]))?\s?FY\s?(\d{2}|\d{4})$/);
  if (!match) return null;
  const year = Number(match[2]);
  const fiscalYear = year < 100 ? 2000 + year : year;
  return fiscalYear * 4 + Number(match[1] ?? 4) - 1;
}

export function ordinalLabel(ordinal: number): string {
  const fiscalYear = Math.floor(ordinal / 4);
  return `Q${(ordinal % 4) + 1}FY${String(fiscalYear % 100).padStart(2, "0")}`;
}

/** Quarters between two labels (later minus earlier), or null if either is unparsable. */
export function quartersBetween(from: string | null, to: string | null): number | null {
  if (!from || !to) return null;
  const a = periodOrdinal(from);
  const b = periodOrdinal(to);
  return a === null || b === null ? null : b - a;
}
