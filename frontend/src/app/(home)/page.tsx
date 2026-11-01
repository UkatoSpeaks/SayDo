import type { Metadata } from "next";
import Link from "next/link";
import { CompaniesTable } from "@/components/CompaniesTable";
import { CountUp } from "@/components/home/CountUp";
import { HeroStrike } from "@/components/home/HeroStrike";
import { HowItWorks } from "@/components/home/HowItWorks";
import { LedgerReveal } from "@/components/home/LedgerReveal";
import { Marquee } from "@/components/home/Marquee";
import { Reveal } from "@/components/home/Reveal";
import { LedgerTable } from "@/components/LedgerTable";
import { SectionHeader } from "@/components/SectionHeader";
import { Shell } from "@/components/Shell";
import { api, type CompanyListItem } from "@/lib/api";
import { buildHomeData, heroExamples, isDemo, pickFeatured, type HomeData, type HomeInput } from "@/lib/home";
import { MOTION_FLAG_SCRIPT } from "@/lib/motion";

const TITLE = "SayDo — did management do what they said?";
const DESCRIPTION =
  "SayDo reads every earnings call of an Indian listed company, tracks each promise management makes, and checks it against what was actually reported — with the exact quote and page.";

export const metadata: Metadata = {
  title: { absolute: TITLE },
  description: DESCRIPTION,
  openGraph: { title: TITLE, description: DESCRIPTION, type: "website", siteName: "SayDo" },
  twitter: { card: "summary_large_image", title: TITLE, description: DESCRIPTION },
};

/** The featured company's scorecard, ledger and delayed-promise thread: the real company
 * with the most decided promises, else the demo. A real company without a hero example
 * of its own gives way to the demo. Every call may fail; the page then renders with the
 * static fallback figures. */
async function loadCompany(companies: CompanyListItem[], company: CompanyListItem): Promise<HomeInput> {
  const [card, ledger] = await Promise.all([
    api.scorecard(company.id).catch(() => null),
    api.ledger(company.id).catch(() => null),
  ]);
  const delayed = ledger?.rows.find((row) => row.slipped_quarters > 0);
  const thread = delayed ? await api.thread(delayed.thread_id).catch(() => null) : null;
  return { companies, featuredId: company.id, card, ledger, thread };
}

async function load(): Promise<HomeInput> {
  const companies = await api.companies().catch(() => null);
  const featured = companies ? pickFeatured(companies) : null;
  if (!companies || !featured) return { companies, featuredId: null, card: null, ledger: null, thread: null };
  const input = await loadCompany(companies, featured);
  const demo = companies.find(isDemo);
  if (!isDemo(featured) && demo && (!input.card || heroExamples(input.ledger, false).length === 0)) {
    const fallback = await loadCompany(companies, demo);
    if (fallback.card && fallback.ledger) return fallback;
  }
  return input;
}

const primaryButton =
  "inline-flex items-center bg-ink px-5 py-3 text-[16px] font-semibold text-paper no-underline hover:bg-ink-2 hover:text-paper";

export default async function HomePage() {
  const data = buildHomeData(await load());
  return (
    <Shell ticker={<Marquee items={data.ticker} demo={data.featured.demo} />} band={<ClosingBand href={data.scorecardHref} />}>
      <script dangerouslySetInnerHTML={{ __html: MOTION_FLAG_SCRIPT }} />
      <Hero data={data} />
      <LedgerPreview data={data} />
      <HowItWorks quotes={data.linkQuotes} bullet={data.bullet} demoNote={data.howItWorksDemo}>
        <SectionHeader index="02" id="how-title" title="How it works" caption="From transcript to verdict, in three steps." />
      </HowItWorks>
      <WillNotDo />
      <Companies data={data} />
    </Shell>
  );
}

function Hero({ data }: { data: HomeData }) {
  const { ratio } = data;
  return (
    <section aria-labelledby="home-title" className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_340px] xl:grid-cols-[minmax(0,1fr)_380px]">
      <div className="min-w-0">
        <p className="kicker">For investors who read the fine print</p>
        <h1
          id="home-title"
          className="mt-4 max-w-[17ch] text-[40px] leading-[1.02] font-bold tracking-[-0.04em] sm:text-[64px]"
        >
          Management says a lot. SayDo checks what they did.
        </h1>
        <div className="mt-8">
          <HeroStrike examples={data.examples} />
        </div>
        <p className="mt-8 max-w-[52ch] text-[20px] leading-[1.45] text-ink-2">{DESCRIPTION}</p>
        <p className="mt-8 flex flex-wrap items-center gap-x-6 gap-y-4">
          <Link href={data.scorecardHref} className={primaryButton}>
            See a live scorecard
          </Link>
          <a href="#how-it-works" className="text-[16px]">
            How it works ↓
          </a>
        </p>
      </div>
      <div className="border-t-2 border-ink pt-6 lg:border-t-0 lg:border-l lg:border-ink lg:pt-0 lg:pl-7">
        <p className="kicker">Say-Do ratio &middot; {data.featured.demo ? "demo company" : data.featured.name}</p>
        <p className="mt-2 font-mono text-[96px] leading-none font-medium tracking-[-0.04em] sm:text-[112px]">
          {ratio.percent === null ? (
            "—"
          ) : (
            <>
              <CountUp value={ratio.percent} decimals={ratio.decimals} />
              <span className="text-[56px]">%</span>
            </>
          )}
        </p>
        <p className="mt-3 text-[14px] text-muted">
          Promises kept against the latest stated target, out of those with a verdict.
        </p>
        <Reveal as="dl" selector=":scope > div" step={0.15} delay={0.9} className="mt-6 text-[15px]">
          <div className="flex justify-between gap-4 border-t border-rule py-2.5">
            <dt>Against latest targets</dt>
            <dd className="font-mono">
              {ratio.latest.kept}/{ratio.latest.total}
            </dd>
          </div>
          <div className={`flex justify-between gap-4 border-t border-rule py-2.5 ${ratio.lower ? "text-moved" : ""}`}>
            <dt>Against original targets</dt>
            <dd className="font-mono">
              {ratio.original.kept}/{ratio.original.total} &middot; {ratio.original.text}
            </dd>
          </div>
          <div className="flex justify-between gap-4 border-y border-rule py-2.5">
            <dt>Still open</dt>
            <dd className="font-mono">{ratio.open}</dd>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function LedgerPreview({ data }: { data: HomeData }) {
  return (
    <section aria-labelledby="home-ledger-title">
      <SectionHeader index="01" id="home-ledger-title" title="The ledger" caption="Every promise, across every call." />
      <LedgerReveal>
        <LedgerTable ledger={data.ledger} labelledBy="home-ledger-title" />
      </LedgerReveal>
      <p className="mt-3 text-[15px]">
        <Link href={data.ledgerHref}>Open the full ledger &rarr;</Link>
      </p>
    </section>
  );
}

const WONT = [
  {
    title: "No buy or sell calls",
    body: "SayDo shows what management said and what was reported. It never tells you to buy, sell or hold a stock.",
  },
  {
    title: "No price predictions",
    body: "Promises are checked against reported results, not share prices, and SayDo makes no forecast of where a stock will go.",
  },
  {
    title: "No unsourced numbers",
    body: "Every figure links to the transcript page or reported result it came from. If it can’t be sourced, it isn’t shown.",
  },
];

function WillNotDo() {
  return (
    <section aria-labelledby="wont-title">
      <SectionHeader index="03" id="wont-title" title="What SayDo will not do" />
      <Reveal selector="article" step={0.12} className="grid gap-8 md:grid-cols-3">
        {WONT.map((item) => (
          <article key={item.title} className="border-l-[3px] border-ink pl-[18px]">
            <h3 className="text-[21px] leading-[1.3] font-bold tracking-[-0.015em]">{item.title}</h3>
            <p className="mt-2 text-[16px] leading-[1.5] text-ink-2">{item.body}</p>
          </article>
        ))}
      </Reveal>
      <p className="mt-8 text-[14px] text-muted">SayDo is an educational research tool, not investment advice.</p>
    </section>
  );
}

function Companies({ data }: { data: HomeData }) {
  const count = data.companies.length;
  return (
    <section aria-labelledby="home-companies-title">
      <SectionHeader
        index="04"
        id="home-companies-title"
        title="Companies"
        caption={
          <>
            {count} {count === 1 ? "company" : "companies"}
            {data.demoOnly ? (
              <span className="ml-3 border border-ink px-1.5 py-0.5 font-mono text-[12px] text-ink uppercase">
                Demo data
              </span>
            ) : null}
          </>
        }
      />
      <Reveal selector="tbody tr" step={0.08}>
        <CompaniesTable
          companies={data.companies}
          labelledBy="home-companies-title"
          hrefFor={data.live ? undefined : () => "/companies"}
        />
      </Reveal>
      {data.demoOnly ? <p className="mt-3 text-[15px] text-ink-2">Real companies are being added.</p> : null}
    </section>
  );
}

function ClosingBand({ href }: { href: string }) {
  return (
    <section aria-labelledby="closing-title" className="mt-16 bg-ink text-paper">
      <div className="mx-auto flex max-w-[1280px] flex-wrap items-center justify-between gap-x-10 gap-y-6 px-6 py-14">
        <h2 id="closing-title" className="max-w-[20ch] text-[34px] leading-[1.08] font-bold tracking-[-0.035em] sm:text-[48px]">
          Read what they said. See what they did.
        </h2>
        <Link
          href={href}
          className="inline-flex items-center bg-paper px-5 py-3 text-[16px] font-semibold text-ink no-underline hover:bg-hairline hover:text-ink focus-visible:outline-paper"
        >
          See a live scorecard
        </Link>
      </div>
    </section>
  );
}
