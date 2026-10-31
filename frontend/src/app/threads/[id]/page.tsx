import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { DeadlineGrid } from "@/components/DeadlineGrid";
import { ReportLinkButton } from "@/components/ReportLinkButton";
import { SectionHeader } from "@/components/SectionHeader";
import { Shell } from "@/components/Shell";
import { Ticker, type TickerItem } from "@/components/Ticker";
import { TranscriptLink } from "@/components/TranscriptLink";
import { VerdictLabel } from "@/components/VerdictLabel";
import { api, DEMO_SYMBOL, isNotFound, type Company, type ThreadDetail } from "@/lib/api";
import { plural } from "@/lib/format";
import {
  deadlines,
  linkedNames,
  mentionTarget,
  reasonSentence,
  threadDeck,
  threadHeadline,
} from "@/lib/narrative";
import { quartersBetween } from "@/lib/periods";
import { changeTag, TONE_TEXT, verdictOf } from "@/lib/status";

type Params = { params: Promise<{ id: string }> };

async function load(id: string) {
  try {
    const thread = await api.thread(id);
    const company = await api.company(thread.company_id);
    return { thread, company };
  } catch (error) {
    if (isNotFound(error)) notFound();
    throw error;
  }
}

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const { id } = await params;
  try {
    return { title: (await api.thread(id)).title };
  } catch {
    return { title: "Promise" };
  }
}

function tickerItems(company: Company, thread: ThreadDetail): TickerItem[] {
  const { first, current, delayed } = deadlines(thread);
  const slipped = delayed ? quartersBetween(first, current) : null;
  const items: TickerItem[] = [
    { value: company.nse_symbol ?? company.name.toUpperCase() },
    { value: thread.title.toUpperCase() },
    {
      label: "STATUS",
      value: verdictOf(thread.status, thread.latest_verification?.kept_after_lowering).label.toUpperCase(),
      numeric: true,
    },
  ];
  if (slipped && slipped > 0) {
    items.push({ label: "SLIPPED", value: `${slipped} ${plural(slipped, "QTR", "QTRS")}`, numeric: true });
  }
  items.push({ label: "MENTIONS", value: String(thread.mention_count), numeric: true });
  return items;
}

export default async function ThreadPage({ params }: Params) {
  const { id } = await params;
  const { thread, company } = await load(id);
  const hasDeadlines = thread.mentions.some((m) => m.target_period);
  return (
    <Shell
      active="companies"
      companyId={company.id}
      ticker={<Ticker items={tickerItems(company, thread)} demo={company.nse_symbol === DEMO_SYMBOL} />}
    >
      <section aria-labelledby="thread-title" className="max-w-[920px]">
        <p className="kicker">
          <Link href={`/companies/${company.id}`} className="text-muted hover:text-link">
            {company.name}
          </Link>{" "}
          &middot; Promise thread
        </p>
        <h1
          id="thread-title"
          className="mt-4 text-[36px] leading-[1.06] font-bold tracking-[-0.035em] sm:text-[52px]"
        >
          {threadHeadline(thread)}
        </h1>
        <p className="mt-5 max-w-[52ch] text-[20px] leading-[1.45] text-ink-2">{threadDeck(thread)}</p>
      </section>

      {hasDeadlines ? (
        <section aria-labelledby="deadline-title">
          <SectionHeader index="01" id="deadline-title" title="Where the deadline sat, call by call" />
          <DeadlineGrid thread={thread} />
          <p className="mt-3 max-w-3xl text-[13px] text-muted">
            Dot: the call where it was said. Line: from that call to the deadline it gave. Dark box:
            the current deadline. Outlined box: a deadline that was later moved.
          </p>
        </section>
      ) : null}

      <div className="grid gap-12 lg:grid-cols-[minmax(0,1fr)_340px]">
        <section aria-labelledby="words-title" className="min-w-0">
          <SectionHeader index={hasDeadlines ? "02" : "01"} id="words-title" title="In their own words" />
          <ol>
            {thread.mentions.map((m) => {
              const tag = changeTag(m.change_type);
              return (
                <li
                  key={m.claim_id}
                  className="grid gap-x-6 gap-y-2 border-b border-rule py-5 first:pt-1 sm:grid-cols-[150px_minmax(0,1fr)]"
                >
                  <div>
                    <p className="font-mono text-[15px] font-semibold">{m.period_label}</p>
                    <p className={`mt-1 text-[12px] font-bold tracking-[0.06em] uppercase ${TONE_TEXT[tag.tone]}`}>
                      {tag.label}
                    </p>
                    {m.speaker ? <p className="mt-1 text-[13px] text-muted">{m.speaker}</p> : null}
                  </div>
                  <div>
                    <blockquote className="text-[21px] leading-[1.4] text-ink">
                      &ldquo;{m.quote}&rdquo;
                    </blockquote>
                    <p className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[14px]">
                      <TranscriptLink documentId={m.document_id} page={m.page_number} />
                      {m.target_low !== null ? (
                        <span className="font-mono text-[12px] text-faint">
                          TARGET {mentionTarget(m)}
                          {m.target_period ? ` · ${m.target_period}` : ""}
                        </span>
                      ) : m.target_period ? (
                        <span className="font-mono text-[12px] text-faint">DUE {m.target_period}</span>
                      ) : null}
                    </p>
                  </div>
                </li>
              );
            })}
          </ol>
        </section>
        <VerdictAside thread={thread} />
      </div>
    </Shell>
  );
}

function VerdictAside({ thread }: { thread: ThreadDetail }) {
  const verification = thread.latest_verification;
  const { delayed } = deadlines(thread);
  const wantsReason = thread.status === "missed" || thread.status === "dropped" || delayed;
  const reason = wantsReason
    ? thread.explanation_chunks
        .map((chunk) => ({ chunk, sentence: reasonSentence(chunk.text) }))
        .find((r) => r.sentence !== null)
    : undefined;
  const names = linkedNames(thread);
  const method =
    verification?.method === "llm"
      ? "AI judge · quote verified"
      : verification
        ? "Computed from reported results"
        : null;
  return (
    <aside aria-label="Verdict" className="flex flex-col gap-10">
      <div className="border-t-2 border-ink pt-4">
        <p className="kicker">Verdict</p>
        <p className="mt-3">
          <VerdictLabel status={thread.status} keptAfterLowering={verification?.kept_after_lowering} size="lg" />
        </p>
        <p className="mt-3 text-[16px] leading-[1.5] text-ink-2">
          {verification?.reasoning ?? "This promise has not been verified yet."}
        </p>
        {method ? (
          <p className="mt-3 font-mono text-[12px] text-faint">
            {method}
            {verification?.as_of_period ? ` · as of ${verification.as_of_period}` : ""}
          </p>
        ) : null}
      </div>

      {reason && reason.sentence ? (
        <figure>
          <p className="kicker">Their reason for the {thread.status === "missed" ? "miss" : "delay"}</p>
          <blockquote className="mt-3 border-y border-ink py-4 text-[22px] leading-[1.35] font-medium">
            &ldquo;{reason.sentence}&rdquo;
          </blockquote>
          <figcaption className="mt-2 flex flex-wrap gap-x-4 text-[14px]">
            <span className="font-mono text-muted">
              {reason.chunk.period_label}
              {reason.chunk.speaker ? ` · ${reason.chunk.speaker}` : ""}
            </span>
            <TranscriptLink documentId={reason.chunk.document_id} page={reason.chunk.page_start} />
          </figcaption>
        </figure>
      ) : null}

      {names.length > 1 ? (
        <div className="border-t border-rule pt-4">
          <p className="kicker">Linked automatically</p>
          <p className="mt-2 text-[15px] leading-[1.5] text-ink-2">
            These mentions use different names ({names.map((n) => `“${n}”`).join(", ")}) and
            were linked into one promise automatically.
          </p>
          <ReportLinkButton threadId={thread.id} names={names} />
        </div>
      ) : null}
    </aside>
  );
}
