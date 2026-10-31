import { cells, mention, row, scorecard, thread } from "@/test/fixtures";
import {
  joinClauses,
  linkedNames,
  lowerFirst,
  readFirst,
  reasonSentence,
  scorecardDeck,
  scorecardHeadline,
  threadDeck,
  threadHeadline,
} from "./narrative";

describe("scorecardHeadline", () => {
  it("states kept out of checkable", () => {
    expect(scorecardHeadline(6, 8, 10)).toBe("Management kept 6 of its 8 checkable promises.");
  });

  it("handles no misses", () => {
    expect(scorecardHeadline(3, 3, 5)).toBe("Management kept all 3 of its checkable promises.");
    expect(scorecardHeadline(1, 1, 1)).toBe("Management kept its only decided promise.");
  });

  it("handles nothing kept and nothing decided", () => {
    expect(scorecardHeadline(0, 4, 4)).toBe("Management kept none of its 4 checkable promises.");
    expect(scorecardHeadline(0, 0, 3)).toBe(
      "Management has made 3 checkable promises; none has been decided yet.",
    );
    expect(scorecardHeadline(0, 0, 0)).toBe("No promises have been tracked yet.");
  });
});

const lowered = row({
  title: "PAT ₹400 crore in FY26",
  metric_normalized: "pat",
  kept_after_lowering: true,
  cells: cells([["said", "450"], "silent", ["lowered", "400"], ["kept", "410"]]),
  comparison: {
    unit: "inr_crore",
    target_low: 400,
    target_high: null,
    target_kind: "at_least",
    original_low: 450,
    original_high: null,
    original_kind: "at_least",
    actual: 410,
    actual_period: "FY26",
    gap_pct: null,
  },
});
const dropped = row({
  title: "Net debt ₹200 crore by FY27",
  metric_normalized: "net_debt",
  verdict: "dropped",
  target_period: "FY27",
  cells: cells([["said", "200"], ["said", "200"], "silent", "silent"]),
});
const delayed = row({
  title: "Line 4 commissioning by Q1FY27",
  metric_normalized: "plant_commissioning",
  verdict: "in_progress",
  first_target_period: "Q2FY26",
  target_period: "Q1FY27",
  slipped_quarters: 3,
  cells: cells([["said", "Q2FY26"], ["moved", "→ Q3FY26"], ["moved", "→ Q1FY27"], "said"]),
});
const plain = row();

describe("scorecardDeck", () => {
  it("mentions lowered targets, dropped promises and the most-delayed thread", () => {
    expect(scorecardDeck([lowered, dropped, delayed, plain], 8)).toBe(
      "One target was lowered before it was met, one promise was quietly dropped and " +
        "Line 4 commissioning has slipped three quarters, from Q2FY26 to Q1FY27.",
    );
  });

  it("says so when nothing was lowered, dropped or delayed", () => {
    expect(scorecardDeck([plain], 6)).toBe(
      "No target was lowered, nothing was dropped and no deadline moved.",
    );
  });

  it("flags a low sample", () => {
    expect(scorecardDeck([plain], 2)).toBe(
      "No target was lowered, nothing was dropped and no deadline moved. " +
        "With only 2 promises decided, read the ratio as indicative.",
    );
    expect(scorecardDeck([dropped], 1)).toBe(
      "One promise was quietly dropped. With only 1 promise decided, read the ratio as indicative.",
    );
  });

  it("handles nothing decided yet", () => {
    expect(scorecardDeck([], 0)).toBe("No promise has reached its deadline yet.");
  });

  it("counts several lowered targets", () => {
    const another = { ...lowered, thread_id: "x", kept_after_lowering: false };
    expect(scorecardDeck([lowered, another], 6)).toBe("Two targets were lowered (one then met).");
  });
});

describe("readFirst", () => {
  const miss = row({
    title: "Revenue ₹5,000 crore in FY26",
    metric_normalized: "revenue",
    verdict: "missed",
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
  });
  const card = scorecard({
    top_misses: [
      {
        thread_id: miss.thread_id,
        title: miss.title,
        metric: "revenue",
        status: "missed",
        gap_pct: "-7.60",
        computed_gap: "-380",
        near_miss: false,
        reasoning: null,
        target_period: "FY26",
        last_seen_period: "Q2FY26",
        kept_after_lowering: false,
      },
    ],
    dropped: [
      {
        thread_id: dropped.thread_id,
        title: dropped.title,
        metric: "net_debt",
        status: "dropped",
        gap_pct: null,
        computed_gap: null,
        near_miss: false,
        reasoning: null,
        target_period: "FY27",
        last_seen_period: "Q2FY26",
        kept_after_lowering: false,
      },
    ],
  });

  it("writes all three columns", () => {
    const items = readFirst(card, [miss, dropped, lowered, delayed]);
    expect(items.map((i) => i.headline)).toEqual([
      "Revenue missed its FY26 target by 7.6%: ₹4,620 cr against ₹5,000 cr.",
      "The promise of net debt ₹200 crore by FY27 was last mentioned on the Q2FY26 call, then never again.",
      "PAT guidance was cut from ₹450 cr to ₹400 cr before results came in at ₹410 cr.",
    ]);
    expect(items[0]!.numbers).toBe("TARGET ₹5,000 cr · ACTUAL ₹4,620 cr · GAP −7.6%");
  });

  it("falls back to the most-delayed thread for goalposts", () => {
    const items = readFirst(scorecard(), [delayed]);
    expect(items).toHaveLength(1);
    expect(items[0]!.headline).toBe(
      "Line 4 commissioning was first due in Q2FY26; the deadline is now Q1FY27.",
    );
    expect(items[0]!.numbers).toBe("SLIPPED 3 QTRS · MOVED 2×");
  });

  it("hides every column when there is nothing to say", () => {
    expect(readFirst(scorecard(), [plain])).toEqual([]);
  });
});

describe("threadHeadline and threadDeck", () => {
  it("describes a delayed promise by its deadlines", () => {
    const t = thread({
      title: "Line 4 commissioning by Q1FY27",
      metric_normalized: "plant_commissioning",
      status: "in_progress",
      target_period: "Q1FY27",
      last_seen_period: "Q4FY26",
      mentions: [
        mention({ period_label: "Q1FY26", target_period: "Q2FY26" }),
        mention({ period_label: "Q2FY26", target_period: "Q3FY26", change_type: "delayed" }),
        mention({ period_label: "Q3FY26", target_period: "Q1FY27", change_type: "delayed" }),
      ],
    });
    expect(threadHeadline(t)).toBe(
      "Line 4 commissioning was due in Q2FY26. It is now promised for Q1FY27.",
    );
    expect(threadDeck(t)).toBe(
      "First said on the Q1FY26 call and repeated on two later calls, most recently Q4FY26. " +
        "The deadline moved twice.",
    );
  });

  it("names the lowered target when it was met only after lowering", () => {
    const t = thread({
      latest_verification: { kept_after_lowering: true } as never,
      actual_used: { value: "410", unit: "inr_crore", period: "FY26" },
      mentions: [
        mention({ target_low: "450", target_high: "450", unit: "inr_crore" }),
        mention({
          period_label: "Q3FY26",
          change_type: "lowered",
          target_low: "400",
          target_high: "400",
          unit: "inr_crore",
        }),
      ],
    });
    expect(threadHeadline(t)).toBe("PAT met its lowered target of ₹400 cr, not the original ₹450 cr.");
    expect(threadDeck(t)).toContain("The target was lowered once.");
  });

  it("converts the actual into the target's unit", () => {
    const t = thread({
      title: "Capex ₹300–350 crore in FY26",
      metric_normalized: "capex",
      actual_used: { value: "32000", unit: "inr_lakh", period: "FY26" },
      mentions: [mention({ target_low: "300", target_high: "350", unit: "inr_crore" })],
    });
    expect(threadHeadline(t)).toBe("Capex came in at ₹320 cr against a target of ₹300–350 cr.");
    expect(threadDeck(t)).toBe("Said once, on the Q1FY26 call.");
  });

  it("writes a plain sentence for other verdicts", () => {
    expect(threadHeadline(thread({ title: "Net debt ₹200 crore by FY27", status: "dropped" }))).toBe(
      "Net debt ₹200 crore was promised, then no longer mentioned.",
    );
    expect(threadHeadline(thread({ status: null }))).toBe("PAT ₹400 crore has not been verified yet.");
  });
});

describe("helpers", () => {
  it("joins clauses", () => {
    expect(joinClauses(["a"])).toBe("a");
    expect(joinClauses(["a", "b", "c"])).toBe("a, b and c");
  });

  it("lower-cases only ordinary words", () => {
    expect(lowerFirst("Net debt ₹200 crore")).toBe("net debt ₹200 crore");
    expect(lowerFirst("PAT ₹400 crore")).toBe("PAT ₹400 crore");
    expect(lowerFirst("Line 4 commissioning")).toBe("Line 4 commissioning");
  });

  it("finds the reason sentence", () => {
    expect(
      reasonSentence("Demand was fine. The delay is because equipment delivery slipped. More text."),
    ).toBe("The delay is because equipment delivery slipped.");
    expect(reasonSentence("All on track.")).toBeNull();
  });

  it("lists linked names only when they differ", () => {
    const same = thread({ mentions: [mention(), mention({ metric_raw: "pat" })] });
    expect(linkedNames(same)).toEqual([]);
    const renamed = thread({
      mentions: [mention({ metric_raw: "Line 4 commissioning" }), mention({ metric_raw: "Pune expansion" })],
    });
    expect(linkedNames(renamed)).toEqual(["Line 4 commissioning", "Pune expansion"]);
  });
});
