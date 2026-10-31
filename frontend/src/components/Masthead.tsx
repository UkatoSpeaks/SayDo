import Link from "next/link";
import { Suspense } from "react";
import { api } from "@/lib/api";
import { CompanyFinderInput } from "./CompanyFinderInput";

export type Section = "companies" | "search" | "method";

async function CompanyFinder() {
  let companies: { id: string; name: string; nse_symbol: string | null }[] = [];
  try {
    companies = (await api.companies()).map(({ id, name, nse_symbol }) => ({
      id,
      name,
      nse_symbol,
    }));
  } catch {
    // The finder still works as a search box over /companies without suggestions.
  }
  return <CompanyFinderInput companies={companies} />;
}

export function Masthead({ active, companyId }: { active?: Section; companyId?: string }) {
  const nav: { key: Section; label: string; href: string }[] = [
    { key: "companies", label: "Companies", href: "/companies" },
    { key: "search", label: "Search", href: companyId ? `/search?company=${companyId}` : "/search" },
    { key: "method", label: "Method", href: "/method" },
  ];
  return (
    <header className="border-b-2 border-ink">
      <div className="mx-auto flex max-w-[1280px] flex-wrap items-center justify-between gap-x-10 gap-y-3 px-6 py-4">
        <div className="flex flex-wrap items-center gap-x-10 gap-y-3">
          <Link href="/" className="text-[24px] leading-none tracking-[-0.04em] text-ink no-underline hover:text-ink">
            <span className="font-normal">Say</span>
            <span className="font-extrabold">Do</span>
          </Link>
          <nav aria-label="Main">
            <ul className="flex flex-wrap gap-x-6 gap-y-2 text-[15px]">
              {nav.map((item) => {
                const isActive = item.key === active;
                return (
                  <li key={item.key}>
                    <Link
                      href={item.href}
                      aria-current={isActive ? "page" : undefined}
                      className={`text-ink hover:text-ink ${
                        isActive
                          ? "font-semibold underline decoration-2 underline-offset-[6px]"
                          : "no-underline hover:underline hover:underline-offset-[6px]"
                      }`}
                    >
                      {item.label}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </nav>
        </div>
        <Suspense fallback={<CompanyFinderInput companies={[]} />}>
          <CompanyFinder />
        </Suspense>
      </div>
    </header>
  );
}
