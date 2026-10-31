/** Pick the passage sentence most relevant to the query, by word overlap. Client-side,
 * so highlighting needs no extra API call. */

const STOPWORDS = new Set(
  (
    "a an and are as at be been by can could did do does for from had has have how i in is it " +
    "its of on or our over said say says should so than that the their them then there these " +
    "they this to was we were what when where which who why will with would you your about " +
    "any into out up down management company"
  ).split(" "),
);

// "Rs. 5,000" and "Mr. Rao" do not end a sentence.
const ABBREVIATIONS = new Set(["rs", "mr", "ms", "mrs", "dr", "no", "vs", "approx", "inr", "st"]);

function stem(word: string): string {
  if (word.length > 4 && word.endsWith("ies")) return `${word.slice(0, -3)}y`;
  if (word.length > 3 && word.endsWith("s") && !word.endsWith("ss")) return word.slice(0, -1);
  return word;
}

export function terms(text: string): Set<string> {
  const words = text.toLowerCase().match(/[a-z0-9]+/g) ?? [];
  return new Set(words.filter((w) => !STOPWORDS.has(w) && w.length > 1).map(stem));
}

/** Sentences with their trailing whitespace, so joining them gives back the text. */
export function splitSentences(text: string): string[] {
  const sentences: string[] = [];
  const boundary = /[.!?]+["'”’)]*(\s+|$)/g;
  let start = 0;
  for (const match of text.matchAll(boundary)) {
    const end = match.index + match[0].length;
    const before = text.slice(start, match.index);
    const lastWord = before.match(/([A-Za-z]+)$/)?.[1]?.toLowerCase();
    const isLast = end >= text.length;
    if (!isLast && lastWord && ABBREVIATIONS.has(lastWord)) continue;
    if (match[0].trim() === "" && !isLast) continue;
    sentences.push(text.slice(start, end));
    start = end;
  }
  if (start < text.length) sentences.push(text.slice(start));
  return sentences.filter((s) => s.length > 0);
}

export interface Highlighted {
  sentences: string[];
  /** Index of the sentence to highlight, or null when nothing overlaps the query. */
  index: number | null;
}

export function pickSentence(text: string, query: string): Highlighted {
  const sentences = splitSentences(text);
  const wanted = terms(query);
  let best: number | null = null;
  let bestScore = 0;
  sentences.forEach((sentence, index) => {
    const have = terms(sentence);
    let score = 0;
    for (const term of wanted) if (have.has(term)) score += 1;
    if (score > bestScore) {
      bestScore = score;
      best = index;
    }
  });
  return { sentences, index: best };
}
