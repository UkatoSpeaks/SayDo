import Link from "next/link";
import type { Ledger } from "@/lib/api";
import { LedgerCellView } from "./LedgerCellView";
import { VerdictLabel } from "./VerdictLabel";

/** The promise x call matrix. Scrolls inside its panel on small screens. */
export function LedgerTable({ ledger, labelledBy }: { ledger: Ledger; labelledBy?: string }) {
  return (
    <div className="relative overflow-x-auto border border-rule bg-insert" tabIndex={0} aria-labelledby={labelledBy} role="region">
      <table className="w-full border-collapse text-left">
        <caption className="sr-only">
          Each promise by earnings call: what management said, and the verdict.
        </caption>
        <thead>
          <tr className="border-b border-ink">
            <th scope="col" className="min-w-[180px] px-4 sm:min-w-[260px] py-2.5 text-[13px] font-semibold text-muted">
              Promise
            </th>
            {ledger.periods.map((period) => (
              <th
                key={period}
                scope="col"
                className="border-l border-hairline px-3 py-2.5 font-mono text-[12px] font-semibold whitespace-nowrap text-muted"
              >
                {period}
              </th>
            ))}
            <th scope="col" className="border-l border-ink px-4 py-2.5 text-[13px] font-semibold text-muted">
              Verdict
            </th>
          </tr>
        </thead>
        <tbody>
          {ledger.rows.map((row) => (
            <tr key={row.thread_id} className="border-b border-hairline last:border-b-0">
              <th scope="row" className="px-4 py-3 align-top font-normal">
                <Link href={`/threads/${row.thread_id}`} className="text-[15px] font-semibold text-ink hover:text-link">
                  {row.title}
                </Link>
                {row.metric_normalized ? (
                  <span className="mt-0.5 block font-mono text-[12px] text-faint">
                    {row.metric_normalized}
                  </span>
                ) : null}
              </th>
              {row.cells.map((cell) => (
                <LedgerCellView key={cell.period} cell={cell} />
              ))}
              <td data-col="verdict" className="border-l border-ink px-4 py-3 align-middle">
                <VerdictLabel status={row.verdict} keptAfterLowering={row.kept_after_lowering} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
