import { pickSentence, splitSentences, terms } from "./highlight";

const PASSAGE =
  "Revenue this quarter was impacted by weak rural demand. Mr. Rao will cover margins. " +
  "We expect PAT of Rs. 450 crore in FY26. EBITDA margin was 18.5% for the year!";

describe("splitSentences", () => {
  it("keeps abbreviations and decimals inside a sentence", () => {
    expect(splitSentences(PASSAGE)).toEqual([
      "Revenue this quarter was impacted by weak rural demand. ",
      "Mr. Rao will cover margins. ",
      "We expect PAT of Rs. 450 crore in FY26. ",
      "EBITDA margin was 18.5% for the year!",
    ]);
  });

  it("joins back to the original text", () => {
    expect(splitSentences(PASSAGE).join("")).toBe(PASSAGE);
  });

  it("handles text without a full stop", () => {
    expect(splitSentences("No ending here")).toEqual(["No ending here"]);
  });
});

describe("pickSentence", () => {
  it("highlights the sentence sharing the most words with the query", () => {
    expect(pickSentence(PASSAGE, "Why was rural demand weak?").index).toBe(0);
    expect(pickSentence(PASSAGE, "PAT guidance for FY26").index).toBe(2);
  });

  it("matches plurals to singulars", () => {
    expect(pickSentence(PASSAGE, "EBITDA margins").index).toBe(3);
  });

  it("prefers the earlier sentence on a tie", () => {
    expect(pickSentence("Margins rose. Margins fell.", "margin").index).toBe(0);
  });

  it("highlights nothing when no word overlaps", () => {
    expect(pickSentence(PASSAGE, "dividend payout").index).toBeNull();
  });

  it("ignores stop words", () => {
    expect([...terms("What did the management say about the margins?")]).toEqual(["margin"]);
    expect(pickSentence("It was a good year.", "what was the year like").index).toBe(0);
  });
});
