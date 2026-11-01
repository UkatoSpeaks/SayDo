import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { BulletBar } from "@/components/BulletBar";
import { LedgerLegend } from "@/components/LedgerLegend";
import { LedgerTable } from "@/components/LedgerTable";
import { SectionHeader } from "@/components/SectionHeader";
import { Shell } from "@/components/Shell";
import { Ticker, type TickerItem } from "@/components/Ticker";
import { TranscriptLink } from "@/components/TranscriptLink";
import { api, DEMO_SYMBOL, isNotFound, type Company, type DocumentRead, type Ledger, type Scorecard } from "@/lib/api";
import { compareRows } from "@/lib/compare";
import { formatRatio, periodRange, plural, ratioNumber } from "@/lib/format";
import { readFirst, scorecardDeck, scorecardHeadline } from "@/lib/narrative";
import { TONE_BORDER, TONE_TEXT } from "@/lib/status";

type Params = { params: Promise<{ id: string }> };

async function load(id: string) {
  try {
    const [company, card, ledger, transcripts] = await Promise.all([
      api.company(id),
      api.scorecard(id),
      api.ledger(id),
      api.transcripts(id),
    ]);
    return { company, card, ledger, transcripts };
  } catch (error) {
    if (isNotFound(error)) notFound();
    throw error;
  }
}

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const { id } = await params;
  try {
    const company = await api.company(id);
    return { title: `${company.name} scorecard` };
  } catch {
    return { title: "Scorecard" };
  }
}

function tickerItems(company: Company, card: Scorecard): TickerItem[] {
  const n = card.counts;
  return [
    { value: company.nse_symbol ?? company.name.toUpperCase() },
    { label: "SAY-DO", value: formatRatio(card.say_do_latest.ratio), numeric: true },
    { label: "KEPT", value: String(n.delivered ?? 0), numeric: true },
    { label: "MISSED", value: String(n.missed ?? 0), numeric: true },
    { label: "DROPPED", value: String(n.dropped ?? 0), numeric: true },
    { label: "OPEN", value: String(n.in_progress ?? 0), numeric: true },
    {
      label: "DEADLINES MOVED",
      value: String(card.guidance_behavior.counts.delayed ?? 0),
      numeric: true,
    },
    { label: "AS OF", value: card.as_of_period ?? "—", numeric: true },
  ];
}

export default async function ScorecardPage({ params }: Params) {
  const { id } = await params;
  const { company, card, ledger, transcripts } = await load(id);
  const demo = company.nse_symbol === DEMO_SYMBOL;

  return (
    <Shell
      active="companies"
      companyId={company.id}
      ticker={<Ticker items={tickerItems(company, card)} demo={demo} />}
    >
      <Hero company={company} card={card} ledger={ledger} transcripts={transcripts} />
      {ledger.rows.length > 0 ? (
        <>
          <section aria-labelledby="ledger-title">
            <SectionHeader
              index="01"
              id="ledger-title"
              title="The ledger"
              caption="Every promise, call by call, and how it ended."
            />
            <LedgerLegend />
            <LedgerTable ledger={ledger} labelledBy="ledger-title" />
          </section>
          <ReadFirst card={card} ledger={ledger} />
          <TargetVsActual ledger={ledger} />
        </>
      ) : (
        <section>
          <SectionHeader index="01" title="The ledger" />
          <p className="max-w-2xl text-[17px] text-ink-2">
            No promises have been extracted for {company.name} yet. Upload its earnings-call
            transcripts, extract claims and run verification; the ledger fills in from there.
          </p>
        </section>
      )}
      <LongTerm card={card} />
      <Transcripts transcripts={transcripts} index={card.long_term.length > 0 ? "05" : "04"} />
    </Shell>
  );
}

function Hero({
  company,
  card,
  ledger,
  transcripts,
}: {
  company: Company;
  card: Scorecard;
  ledger: Ledger;
  transcripts: DocumentRead[];
}) {
  const latest = card.say_do_latest;
  const original = card.say_do_original;
  const lower = original.ratio !== null && latest.ratio !== null && original.ratio < latest.ratio;
  const lowSample = latest.total > 0 && card.say_do.low_sample;
  const periods = transcripts.map((t) => t.period_label);
  return (
    <section aria-labelledby="hero-title" className="grid gap-10 md:grid-cols-[minmax(0,1fr)_380px]">
      <div>
        <p className="kicker">
          Management credibility &middot; {company.name}
          {company.nse_symbol ? <> &middot; NSE: {company.nse_symbol}</> : null}
        </p>
        <h1
          id="hero-title"
          className="mt-4 max-w-[22ch] text-[40px] leading-[1.04] font-bold tracking-[-0.035em] sm:text-[52px]"
        >
          {scorecardHeadline(latest.kept, latest.total, card.threads_total)}
        </h1>
        <p className="mt-5 max-w-[46ch] text-[20px] leading-[1.45] text-ink-2">
          {scorecardDeck(ledger.rows, latest.total)}
        </p>
        <p className="mt-6 flex flex-wrap items-center gap-x-5 gap-y-2 text-[14px] text-muted">
          <span className="font-mono">
            {transcripts.length} earnings {plural(transcripts.length, "call")}
            {periods.length ? ` · ${periodRange(periods)}` : ""}
          </span>
          {transcripts.length ? <a href="#transcripts">Transcripts</a> : null}
          <Link href={`/search?company=${company.id}`}>Search</Link>
        </p>
      </div>
      <div className="border-t-2 border-ink pt-6 md:border-t-0 md:border-l md:border-ink md:pt-0 md:pl-7">
        <p className="kicker">Say-Do ratio</p>
        <p className="mt-2 flex items-baseline gap-3">
          <span className="font-mono text-[96px] leading-none font-medium tracking-[-0.04em] sm:text-[112px]">
            {ratioNumber(latest.ratio)}
            {latest.ratio !== null ? <span className="text-[56px]">%</span> : null}
          </span>
          {lowSample ? (
            <span className="border border-moved px-1.5 py-0.5 font-mono text-[12px] text-moved uppercase">
              low sample
            </span>
          ) : null}
        </p>
        {latest.ratio === null ? (
          <p className="mt-2 text-[14px] text-muted">Nothing decided yet.</p>
        ) : null}
        <dl className="mt-6 text-[15px]">
          <div className="flex justify-between gap-4 border-t border-rule py-2.5">
            <dt>Against latest targets</dt>
            <dd className="font-mono">
              {latest.kept}/{latest.total}
            </dd>
          </div>
          <div className={`flex justify-between gap-4 border-t border-rule py-2.5 ${lower ? "text-moved" : ""}`}>
            <dt>Against original targets</dt>
            <dd className="font-mono">
              {original.kept}/{original.total} &middot; {formatRatio(original.ratio)}
            </dd>
          </div>
          <div className="flex justify-between gap-4 border-y border-rule py-2.5">
            <dt>Still open</dt>
            <dd className="font-mono">{card.counts.in_progress ?? 0}</dd>
          </div>
        </dl>
      </div>
    </section>
  );
}

function ReadFirst({ card, ledger }: { card: Scorecard; ledger: Ledger }) {
  const items = readFirst(card, ledger.rows);
  if (items.length === 0) return null;
  return (
    <section aria-labelledby="read-first-title">
      <SectionHeader index="02" id="read-first-title" title="What to read first" />
      <div className="grid gap-8 md:grid-cols-3">
        {items.map((item) => (
          <article key={item.key} className={`border-l-[3px] pl-[18px] ${TONE_BORDER[item.tone]}`}>
            <p className={`text-[13px] font-semibold tracking-[0.08em] uppercase ${TONE_TEXT[item.tone]}`}>
              {item.kicker}
            </p>
            <h3 className="mt-2 text-[21px] leading-[1.3] font-bold tracking-[-0.015em]">{item.headline}</h3>
            <p className="mt-3 font-mono text-[12px] text-ink-2">{item.numbers}</p>
            <Link href={`/threads/${item.threadId}`} className="mt-3 inline-block text-[14px]">
              Read the thread &rarr;
            </Link>
          </article>
        ))}
      </div>
    </section>
  );
}

function TargetVsActual({ ledger }: { ledger: Ledger }) {
  const rows = compareRows(ledger.rows);
  if (rows.length === 0) return null;
  return (
    <section aria-labelledby="compare-title">
      <SectionHeader
        index="03"
        id="compare-title"
        title="Target vs actual"
        caption="Numeric promises with a reported result."
      />
      <div className="relative overflow-x-auto border border-rule bg-insert" role="region" aria-labelledby="compare-title" tabIndex={0}>
        <table className="w-full min-w-[720px] border-collapse text-left">
          <thead>
            <tr className="border-b border-ink text-[13px] text-muted">
              <th scope="col" className="px-4 py-2.5 font-semibold">Promise</th>
              <th scope="col" className="w-[40%] px-4 py-2.5 font-semibold">
                <span className="sr-only">Chart</span>
              </th>
              <th scope="col" className="px-4 py-2.5 font-semibold">Target</th>
              <th scope="col" className="px-4 py-2.5 font-semibold">Actual</th>
              <th scope="col" className="px-4 py-2.5 font-semibold">Gap</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.threadId} className="border-b border-hairline last:border-b-0">
                <th scope="row" className="px-4 py-3 text-[15px] font-semibold whitespace-nowrap">
                  <Link href={`/threads/${row.threadId}`} className="text-ink hover:text-link">
                    {row.label}
                  </Link>
                </th>
                <td className="px-4 py-3">
                  <BulletBar input={row.input} tone={row.tone} label={row.ariaLabel} />
                </td>
                <td className="px-4 py-3 font-mono text-[13px] whitespace-nowrap">
                  {row.target}
                  {row.original ? (
                    <span className="block text-[12px] text-faint">was {row.original}</span>
                  ) : null}
                </td>
                <td className="px-4 py-3 font-mono text-[13px] whitespace-nowrap">{row.actual}</td>
                <td className={`px-4 py-3 font-mono text-[13px] whitespace-nowrap ${TONE_TEXT[row.tone]}`}>
                  <span aria-hidden="true">{row.tone === "missed" ? "✕ " : row.tone === "kept" ? "✓ " : ""}</span>
                  {row.gap}
                  <span className="sr-only"> ({row.verdict})</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-3 max-w-3xl text-[13px] text-muted">
        Grey band: the promised range (a one-sided target runs to the end of the bar). Coloured
        mark: the reported actual. Dashed line: the original target, where it was lowered before
        results came in. Each bar has its own scale.
      </p>
    </section>
  );
}

/** Statements with no target period ("medium term"): kept and searchable, never scored. */
function LongTerm({ card }: { card: Scorecard }) {
  const items = card.long_term;
  if (items.length === 0) return null;
  return (
    <section aria-labelledby="long-term-title">
      <SectionHeader
        index="04"
        id="long-term-title"
        title="Long-term statements (not scored)"
        caption="No target period was given, so these cannot be checked and are left out of the ledger."
      />
      <details className="group border border-rule bg-insert">
        <summary className="cursor-pointer px-4 py-3 text-[15px] font-semibold text-ink hover:text-link">
          Show {items.length} {plural(items.length, "statement")}
        </summary>
        <ul className="border-t border-hairline">
          {items.map((t) => (
            <li key={t.thread_id} className="border-b border-hairline px-4 py-3 last:border-b-0">
              <Link href={`/threads/${t.thread_id}`} className="text-[15px] text-ink hover:text-link">
                {t.title}
              </Link>
              {t.last_seen_period ? (
                <span className="ml-3 font-mono text-[12px] text-faint">last said {t.last_seen_period}</span>
              ) : null}
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}

function Transcripts({ transcripts, index }: { transcripts: DocumentRead[]; index: string }) {
  if (transcripts.length === 0) return null;
  return (
    <section aria-labelledby="transcripts" className="scroll-mt-6">
      <SectionHeader index={index} id="transcripts" title="Transcripts" caption="The source of every claim above." />
      <ul className="flex flex-wrap gap-x-8 gap-y-2 font-mono text-[14px]">
        {transcripts.map((t) => (
          <li key={t.id}>
            <TranscriptLink documentId={t.id}>
              {t.period_label} call{t.page_count ? `, ${t.page_count} pp.` : ""} &rarr;
            </TranscriptLink>
          </li>
        ))}
      </ul>
    </section>
  );
}
