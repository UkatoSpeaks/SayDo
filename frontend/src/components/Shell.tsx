import { Footer } from "./Footer";
import { Masthead, type Section } from "./Masthead";

/** Page frame: ticker strip, masthead, content column, footer. `band` is full-width
 * content between the column and the footer. */
export function Shell({
  ticker,
  active,
  companyId,
  band,
  children,
}: {
  ticker: React.ReactNode;
  active?: Section;
  companyId?: string;
  band?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <>
      <a
        href="#content"
        className="sr-only bg-ink px-3 py-2 text-paper focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-10"
      >
        Skip to content
      </a>
      {ticker}
      <Masthead active={active} companyId={companyId} />
      <main id="content">
        <div className="mx-auto flex max-w-[1280px] flex-col gap-12 px-6 pt-10">{children}</div>
        {band}
      </main>
      <Footer />
    </>
  );
}
