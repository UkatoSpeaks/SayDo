import type { Metadata } from "next";
import { SearchClient } from "@/components/SearchClient";
import { Shell } from "@/components/Shell";
import { Ticker } from "@/components/Ticker";
import { api, DEMO_SYMBOL } from "@/lib/api";

export const metadata: Metadata = { title: "Search" };

type Props = { searchParams: Promise<{ company?: string }> };

export default async function SearchPage({ searchParams }: Props) {
  const { company } = await searchParams;
  const companies = await api.companies();
  const selected = companies.find((c) => c.id === company) ?? companies[0];
  return (
    <Shell
      active="search"
      companyId={selected?.id}
      ticker={
        <Ticker
          demo={selected?.nse_symbol === DEMO_SYMBOL}
          items={[
            { value: "SEARCH" },
            { label: "COMPANY", value: selected?.nse_symbol ?? selected?.name.toUpperCase() ?? "—", numeric: true },
            { label: "SOURCE", value: "EARNINGS-CALL TRANSCRIPTS" },
            { label: "MATCHING", value: "BY MEANING, NOT KEYWORDS" },
          ]}
        />
      }
    >
      <section aria-labelledby="search-title">
        <p className="kicker">Search</p>
        <h1 id="search-title" className="mt-3 text-[40px] leading-tight font-bold tracking-[-0.035em] sm:text-[52px]">
          Ask the transcripts.
        </h1>
        <p className="mt-4 max-w-[52ch] text-[20px] leading-[1.45] text-ink-2">
          Find what management said about a topic, in their own words, with the page it is on.
        </p>
      </section>
      {companies.length === 0 ? (
        <p className="text-[17px] text-ink-2">
          No companies yet: add one and upload its transcripts to search them.
        </p>
      ) : (
        <SearchClient
          companies={companies.map((c) => ({ id: c.id, name: c.name, nse_symbol: c.nse_symbol }))}
          initialCompanyId={selected!.id}
        />
      )}
    </Shell>
  );
}
