"use client";

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import { apiGet, transcriptUrl, type DocumentRead, type SearchResult } from "@/lib/api";
import { pickSentence } from "@/lib/highlight";
import { describeSearchError, runSearch, searchBody, type SearchFilters } from "@/lib/search";

interface PickerCompany {
  id: string;
  name: string;
  nse_symbol: string | null;
}

const EXAMPLES = [
  "Why was revenue below guidance?",
  "When will the new capacity be commissioned?",
  "What is the margin outlook for next year?",
  "How is the company reducing debt?",
];

const SECTIONS: { key: SearchFilters["section"]; label: string }[] = [
  { key: "all", label: "All sections" },
  { key: "prepared_remarks", label: "Prepared remarks" },
  { key: "qna", label: "Q&A" },
];

const SECTION_LABEL: Record<string, string> = {
  prepared_remarks: "Prepared remarks",
  qna: "Q&A",
  other: "Other",
};

type State =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "done"; query: string; results: SearchResult[] }
  | { kind: "error"; message: string };

function TextButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`bg-transparent p-0 text-[14px] text-ink ${
        active ? "font-bold underline decoration-2 underline-offset-[5px]" : "hover:underline hover:underline-offset-[5px]"
      }`}
    >
      {children}
    </button>
  );
}

export function SearchClient({
  companies,
  initialCompanyId,
}: {
  companies: PickerCompany[];
  initialCompanyId: string;
}) {
  const [companyId, setCompanyId] = useState(initialCompanyId);
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<SearchFilters>({
    managementOnly: true,
    section: "all",
    from: null,
    to: null,
  });
  const [periods, setPeriods] = useState<string[]>([]);
  const [state, setState] = useState<State>({ kind: "idle" });
  const abort = useRef<AbortController | null>(null);
  const ids = useId();

  // Periods for the range filter come from the company's transcripts (a plain GET).
  useEffect(() => {
    let cancelled = false;
    apiGet<DocumentRead[]>(`/companies/${companyId}/documents?doc_type=transcript&order=asc`)
      .then((docs) => {
        if (!cancelled) setPeriods([...new Set(docs.map((d) => d.period_label))]);
      })
      .catch(() => {
        if (!cancelled) setPeriods([]);
      });
    return () => {
      cancelled = true;
    };
  }, [companyId]);

  async function search(text: string) {
    const trimmed = text.trim();
    if (!trimmed) return;
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setState({ kind: "loading" });
    try {
      const response = await runSearch(companyId, searchBody(trimmed, filters), controller.signal);
      setState({ kind: "done", query: trimmed, results: response.results });
    } catch (error) {
      if (controller.signal.aborted) return;
      setState({ kind: "error", message: describeSearchError(error) });
    }
  }

  const count = state.kind === "done" ? state.results.length : null;

  return (
    <section aria-label="Search the transcripts" className="flex flex-col gap-8">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void search(query);
        }}
        className="flex flex-col gap-5"
      >
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[14px]">
          <label htmlFor={`${ids}-company`} className="kicker">
            Company
          </label>
          <select
            id={`${ids}-company`}
            value={companyId}
            onChange={(event) => {
              setCompanyId(event.target.value);
              setFilters((f) => ({ ...f, from: null, to: null }));
              setState({ kind: "idle" });
            }}
            className="border-0 border-b border-ink bg-transparent py-1 pr-6 font-semibold"
          >
            {companies.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
                {c.nse_symbol ? ` (${c.nse_symbol})` : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-4 sm:flex-row sm:items-end">
          <label htmlFor={`${ids}-query`} className="sr-only">
            Question
          </label>
          <input
            id={`${ids}-query`}
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="What did management say about margins?"
            maxLength={2000}
            className="min-w-0 flex-1 border-0 border-b-2 border-ink bg-transparent py-2 text-[22px] placeholder:text-muted focus:outline-none focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-link sm:text-[26px]"
          />
          <button
            type="submit"
            disabled={state.kind === "loading" || !query.trim()}
            className="bg-ink px-7 py-3 text-[16px] font-semibold text-paper disabled:cursor-not-allowed disabled:bg-muted"
          >
            Search
          </button>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-x-8 gap-y-3 border-b border-rule pb-3">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-3" role="group" aria-label="Filters">
            <TextButton
              active={filters.managementOnly}
              onClick={() => setFilters((f) => ({ ...f, managementOnly: !f.managementOnly }))}
            >
              Management only
            </TextButton>
            <span className="h-4 border-l border-rule" aria-hidden="true" />
            {SECTIONS.map((s) => (
              <TextButton
                key={s.key}
                active={filters.section === s.key}
                onClick={() => setFilters((f) => ({ ...f, section: s.key }))}
              >
                {s.label}
              </TextButton>
            ))}
            <span className="h-4 border-l border-rule" aria-hidden="true" />
            <span className="flex items-center gap-2 font-mono text-[13px]">
              <label htmlFor={`${ids}-from`} className="text-muted">
                From
              </label>
              <select
                id={`${ids}-from`}
                value={filters.from ?? ""}
                onChange={(e) => setFilters((f) => ({ ...f, from: e.target.value || null }))}
                className="border-0 border-b border-ink bg-transparent py-0.5"
              >
                <option value="">first call</option>
                {periods.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
              <label htmlFor={`${ids}-to`} className="text-muted">
                to
              </label>
              <select
                id={`${ids}-to`}
                value={filters.to ?? ""}
                onChange={(e) => setFilters((f) => ({ ...f, to: e.target.value || null }))}
                className="border-0 border-b border-ink bg-transparent py-0.5"
              >
                <option value="">latest call</option>
                {periods.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </span>
          </div>
          <p className="font-mono text-[13px] text-muted" aria-live="polite">
            {count === null ? "" : `${count} ${count === 1 ? "passage" : "passages"}`}
          </p>
        </div>
      </form>

      <SearchState
        state={state}
        onExample={(example) => {
          setQuery(example);
          void search(example);
        }}
      />
    </section>
  );
}

function SearchState({ state, onExample }: { state: State; onExample: (q: string) => void }) {
  if (state.kind === "idle") {
    return (
      <div>
        <p className="kicker">Try asking</p>
        <ul className="mt-3 flex flex-col items-start gap-2.5">
          {EXAMPLES.map((example) => (
            <li key={example}>
              <button
                type="button"
                onClick={() => onExample(example)}
                className="bg-transparent p-0 text-left text-[19px] text-link hover:text-link-hover hover:underline hover:underline-offset-4"
              >
                {example}
              </button>
            </li>
          ))}
        </ul>
      </div>
    );
  }
  if (state.kind === "loading") {
    return (
      <div role="status" aria-label="Searching">
        {[0, 1, 2].map((i) => (
          <div key={i} aria-hidden="true" className="grid gap-6 border-b border-rule py-6 sm:grid-cols-[170px_minmax(0,1fr)]">
            <div className="flex flex-col gap-2">
              <span className="block h-4 w-20 bg-skeleton" />
              <span className="block h-3 w-28 bg-skeleton" />
            </div>
            <div className="flex flex-col gap-2.5">
              <span className="block h-4 w-full bg-skeleton" />
              <span className="block h-4 w-11/12 bg-skeleton" />
              <span className="block h-4 w-2/3 bg-skeleton" />
            </div>
          </div>
        ))}
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <p role="alert" className="border-l-[3px] border-missed pl-[18px] text-[17px] text-ink">
        {state.message}
      </p>
    );
  }
  if (state.results.length === 0) {
    return (
      <p className="text-[17px] text-ink-2">
        No passages match &ldquo;{state.query}&rdquo; with these filters. Try fewer filters or
        other words.
      </p>
    );
  }
  return (
    <ol>
      {state.results.map((result) => (
        <ResultRow key={result.chunk_id} result={result} query={state.query} />
      ))}
    </ol>
  );
}

function ResultRow({ result, query }: { result: SearchResult; query: string }) {
  const { sentences, index } = pickSentence(result.text, query);
  const pages =
    result.page_start === result.page_end ? `p. ${result.page_start}` : `pp. ${result.page_start}–${result.page_end}`;
  return (
    <li className="grid gap-x-6 gap-y-3 border-b border-rule py-6 sm:grid-cols-[170px_minmax(0,1fr)]">
      <div className="flex flex-col gap-1 text-[13px] text-muted">
        <p className="font-mono text-[18px] text-ink">{result.period_label}</p>
        {result.speaker ? <p className="text-ink-2">{result.speaker}</p> : null}
        <p>
          {SECTION_LABEL[result.section] ?? result.section} &middot; {pages}
        </p>
        <p className="font-mono">match {result.score.toFixed(2)}</p>
      </div>
      <div>
        <p className="text-[19px] leading-[1.5] text-ink">
          {sentences.map((sentence, i) =>
            i === index ? <mark key={i}>{sentence}</mark> : <span key={i}>{sentence}</span>,
          )}
        </p>
        <p className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-[14px]">
          <a href={transcriptUrl(result.document_id, result.page_start)} target="_blank" rel="noopener noreferrer">
            Open transcript &rarr;<span className="sr-only"> (PDF, opens in a new tab)</span>
          </a>
          {result.thread_id ? <Link href={`/threads/${result.thread_id}`}>In the ledger &rarr;</Link> : null}
        </p>
      </div>
    </li>
  );
}
