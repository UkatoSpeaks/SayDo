import Link from "next/link";
import { Shell } from "@/components/Shell";
import { Ticker } from "@/components/Ticker";

export default function NotFound() {
  return (
    <Shell ticker={<Ticker items={[{ value: "NOT FOUND" }, { label: "STATUS", value: "404", numeric: true }]} />}>
      <section className="max-w-2xl">
        <p className="kicker">Not found</p>
        <h1 className="mt-4 text-[40px] leading-[1.05] font-bold tracking-[-0.035em] sm:text-[52px]">
          There is nothing on file at this address.
        </h1>
        <p className="mt-5 text-[20px] leading-[1.45] text-ink-2">
          The company or promise may have been removed, or the link is mistyped.
        </p>
        <p className="mt-6 text-[15px]">
          <Link href="/companies">Back to companies &rarr;</Link>
        </p>
      </section>
    </Shell>
  );
}
