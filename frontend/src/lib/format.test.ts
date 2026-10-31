import { formatRatio, formatSignedPct, formatTarget, formatValue } from "./format";

describe("formatting", () => {
  it("uses Indian grouping and the unit field", () => {
    expect(formatValue(4620, "inr_crore")).toBe("₹4,620 cr");
    expect(formatValue(1234567, "inr_lakh")).toBe("₹12,34,567 lakh");
    expect(formatValue(40000, "tonnes")).toBe("40,000 tonnes");
  });

  it("keeps percentages to one decimal", () => {
    expect(formatValue(18.528, "percent")).toBe("18.5%");
    expect(formatRatio(0.625)).toBe("62.5%");
    expect(formatRatio(0.75)).toBe("75%");
    expect(formatRatio(2 / 3)).toBe("66.7%");
    expect(formatRatio(null)).toBe("—");
    expect(formatSignedPct(-7.6)).toBe("−7.6%");
    expect(formatSignedPct(2.456)).toBe("+2.5%");
  });

  it("formats targets", () => {
    expect(formatTarget(300, 350, "inr_crore")).toBe("₹300–350 cr");
    expect(formatTarget(18, 19, "percent")).toBe("18–19%");
    expect(formatTarget(400, null, "inr_crore", "at_least")).toBe("at least ₹400 cr");
    expect(formatTarget(null, 200, "inr_crore", "at_most")).toBe("at most ₹200 cr");
  });
});
