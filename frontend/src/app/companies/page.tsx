import type { Metadata } from "next";
import Link from "next/link";
import { CompaniesTable } from "@/components/CompaniesTable";
import { SectionHeader } from "@/components/SectionHeader";
import { Shell } from "@/components/Shell";
import { Ticker } from "@/components/Ticker";
import { api, DEMO_SYMBOL } from "@/lib/api";

export const metadata: Metadata = { title: "Companies" };

type Props = { searchParams: Promise<{ q?: string }> };

export default async function CompaniesPage({ searchParams }: Props) {
  const { q } = await searchParams;
  const all = await api.companies();
  const needle = q?.trim().toLowerCase() ?? "";
  const companies = needle
    ? all.filter(
        (c) =>
          c.name.toLowerCase().includes(needle) || c.nse_symbol?.toLowerCase().includes(needle),
      )
    : all;
  const open = all.reduce((sum, c) => sum + c.open_count, 0);
  const latest = all
    .map((c) => c.latest_period)
    .filter((p): p is string => Boolean(p))
    .sort((a, b) => periodOrdinal(b) - periodOrdinal(a))[0];
  const demoOnly = all.length > 0 && all.every((c) => c.nse_symbol === DEMO_SYMBOL);

  return (
    <Shell
      active="companies"
      ticker={
        <Ticker
          demo={demoOnly}
          items={[
            { value: "COMPANIES" },
            { label: "TRACKED", value: String(all.length), numeric: true },
            { label: "OPEN PROMISES", value: String(open), numeric: true },
            { label: "LATEST DATA", value: latest ?? "—", numeric: true },
          ]}
        />
      }
    >
      <section aria-labelledby="companies-title">
        <p className="kicker">Listed on NSE</p>
        <h1 id="companies-title" className="mt-3 mb-10 text-[40px] leading-tight font-bold tracking-[-0.035em] sm:text-[52px]">
          Companies
        </h1>
        <SectionHeader
          index="01"
          title={needle ? `Matching “${q}”` : "All companies"}
          caption={`${companies.length} ${companies.length === 1 ? "company" : "companies"}`}
        />
        {all.length === 0 ? (
          <EmptyState />
        ) : companies.length === 0 ? (
          <p className="text-[17px] text-ink-2">
            No company matches &ldquo;{q}&rdquo;. <Link href="/companies">Show all companies</Link>.
          </p>
        ) : (
          <CompaniesTable companies={companies} labelledBy="companies-title" />
        )}
      </section>
    </Shell>
  );
}

function periodOrdinal(label: string): number {
  const match = label.match(/^(?:Q([1-4]))?FY(\d{2})$/);
  if (!match) return 0;
  return Number(match[2]) * 4 + Number(match[1] ?? 4);
}

function EmptyState() {
  const step = "border-t border-rule py-3";
  return (
    <div className="max-w-3xl">
      <p className="text-[20px] leading-[1.45] text-ink-2">
        No companies yet. SayDo builds a scorecard from a company&rsquo;s earnings-call
        transcripts and reported results; add them through the backend API.
      </p>
      <ol className="mt-6 text-[15px]">
        <li className={step}>
          <span className="mr-3 font-mono text-muted">1</span>Create the company:{" "}
          <code className="font-mono text-[13px]">POST /companies</code> with its name and NSE symbol.
        </li>
        <li className={step}>
          <span className="mr-3 font-mono text-muted">2</span>Upload each transcript PDF:{" "}
          <code className="font-mono text-[13px]">POST /companies/&#123;id&#125;/documents</code>{" "}
          (<code className="font-mono text-[13px]">doc_type=transcript</code>, a period such as Q2FY26).
        </li>
        <li className={step}>
          <span className="mr-3 font-mono text-muted">3</span>Extract the promises:{" "}
          <code className="font-mono text-[13px]">POST /documents/&#123;id&#125;/extract</code>.
        </li>
        <li className={step}>
          <span className="mr-3 font-mono text-muted">4</span>Enter reported results from the CSV
          template: <code className="font-mono text-[13px]">POST /companies/&#123;id&#125;/actuals/import</code>.
        </li>
        <li className={`${step} border-b`}>
          <span className="mr-3 font-mono text-muted">5</span>Verify:{" "}
          <code className="font-mono text-[13px]">POST /companies/&#123;id&#125;/verify</code>.
        </li>
      </ol>
      <p className="mt-6 text-[15px] text-ink-2">
        To try SayDo with a fictional company, run{" "}
        <code className="font-mono text-[13px]">uv run python -m scripts.seed_demo</code> in{" "}
        <code className="font-mono text-[13px]">backend/</code>.
      </p>
    </div>
  );
}
