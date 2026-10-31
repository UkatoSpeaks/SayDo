import { bulletDomain, bulletGeometry } from "./bullet";

describe("bulletDomain", () => {
  it("pads 30% of the span around the values", () => {
    expect(bulletDomain([300, 350, 320])).toEqual([285, 365]);
  });

  it("does not go below zero for non-negative values", () => {
    expect(bulletDomain([1, 100])[0]).toBe(0);
  });

  it("gives a single value some room", () => {
    expect(bulletDomain([50])).toEqual([45, 55]);
    expect(bulletDomain([0])).toEqual([0, 1]);
  });
});

describe("bulletGeometry", () => {
  it("places a range band and the actual inside it", () => {
    const g = bulletGeometry({ low: 300, high: 350, kind: "range", actual: 320 });
    // domain 285..365 (80 wide)
    expect(g.band.left).toBeCloseTo(18.75);
    expect(g.band.width).toBeCloseTo(62.5);
    expect(g.actual).toBeCloseTo(43.75);
    expect(g.original).toBeNull();
  });

  it("runs an at-least band to the end of the track", () => {
    const g = bulletGeometry({ low: 5000, high: null, kind: "at_least", actual: 4620 });
    expect(g.band.left + g.band.width).toBeCloseTo(100);
    expect(g.actual).toBeLessThan(g.band.left); // a miss sits outside the band
  });

  it("starts an at-most band at the start of the track", () => {
    const g = bulletGeometry({ low: null, high: 200, kind: "at_most", actual: 260 });
    expect(g.band.left).toBe(0);
    expect(g.actual).toBeGreaterThan(g.band.width);
  });

  it("draws the original target when it was lowered", () => {
    const g = bulletGeometry({ low: 400, high: null, kind: "at_least", actual: 410, original: 450 });
    // domain 385..465: original at 81.25%, band from 18.75% to the end
    expect(g.original).toBeCloseTo(81.25);
    expect(g.band.left).toBeCloseTo(18.75);
    expect(g.actual).toBeCloseTo(31.25);
  });

  it("gives a single-figure target a thin band", () => {
    const g = bulletGeometry({ low: 20, high: 20, kind: "exact", actual: 20 });
    expect(g.band.width).toBeCloseTo(1);
    expect(g.actual).toBeCloseTo(g.band.left + 0.5);
  });

  it("keeps everything on the track", () => {
    const g = bulletGeometry({ low: 10, high: 12, kind: "range", actual: -50 });
    for (const x of [g.band.left, g.band.left + g.band.width, g.actual]) {
      expect(x).toBeGreaterThanOrEqual(0);
      expect(x).toBeLessThanOrEqual(100);
    }
  });
});
