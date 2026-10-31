import Link from "next/link";
import type { CompanyListItem } from "@/lib/api";
import { formatRatio } from "@/lib/format";

/** Ruled table of companies: name, symbol, Say-Do, open promises, latest period. */
export function CompaniesTable({
  companies,
  labelledBy,
  hrefFor = (c) => `/companies/${c.id}`,
}: {
  companies: CompanyListItem[];
  labelledBy?: string;
  hrefFor?: (company: CompanyListItem) => string;
}) {
  return (
    <div className="relative overflow-x-auto border border-rule bg-insert" role="region" aria-labelledby={labelledBy} tabIndex={0}>
      <table className="w-full min-w-[640px] border-collapse text-left">
        <thead>
          <tr className="border-b border-ink text-[13px] text-muted">
            <th scope="col" className="px-4 py-2.5 font-semibold">Company</th>
            <th scope="col" className="px-4 py-2.5 font-semibold">NSE</th>
            <th scope="col" className="px-4 py-2.5 text-right font-semibold">Say-Do</th>
            <th scope="col" className="px-4 py-2.5 text-right font-semibold">Open</th>
            <th scope="col" className="px-4 py-2.5 font-semibold">Latest period</th>
            <th scope="col" className="px-4 py-2.5 font-semibold">
              <span className="sr-only">Link</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {companies.map((c) => {
            const href = hrefFor(c);
            return (
              <tr key={c.id} className="border-b border-hairline last:border-b-0">
                <th scope="row" className="px-4 py-3.5 text-[16px] font-semibold">
                  <Link href={href} className="text-ink hover:text-link">
                    {c.name}
                  </Link>
                  {c.sector ? <span className="block text-[13px] font-normal text-muted">{c.sector}</span> : null}
                </th>
                <td className="px-4 py-3.5 font-mono text-[14px]">{c.nse_symbol ?? "—"}</td>
                <td className="px-4 py-3.5 text-right font-mono text-[14px]">
                  {formatRatio(c.say_do_latest.ratio)}
                  <span className="block text-[12px] text-faint">
                    {c.say_do_latest.kept}/{c.say_do_latest.total} kept
                  </span>
                </td>
                <td className="px-4 py-3.5 text-right font-mono text-[14px]">{c.open_count}</td>
                <td className="px-4 py-3.5 font-mono text-[14px]">{c.latest_period ?? "—"}</td>
                <td className="px-4 py-3.5 text-right text-[14px] whitespace-nowrap">
                  <Link href={href}>
                    Scorecard &rarr;<span className="sr-only"> for {c.name}</span>
                  </Link>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
