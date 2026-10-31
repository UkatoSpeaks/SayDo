/** Flat grey bars in the ruled style; no shimmer. */
export function Bar({ className = "" }: { className?: string }) {
  return <span aria-hidden="true" className={`block bg-skeleton ${className}`} />;
}

export function TickerSkeleton() {
  return (
    <div className="bg-ticker">
      <div className="mx-auto flex max-w-[1280px] gap-3 px-6 py-2.5">
        {[56, 88, 64, 72, 64, 96].map((w, i) => (
          <span key={i} className="block h-3 bg-ticker-divider" style={{ width: w }} />
        ))}
      </div>
    </div>
  );
}

export function SectionSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <section aria-hidden="true">
      <div className="mb-5 flex items-baseline gap-4 border-t-2 border-ink pt-3">
        <Bar className="h-3 w-5" />
        <Bar className="h-5 w-56" />
      </div>
      <div className="border border-rule bg-insert">
        {Array.from({ length: rows }, (_, i) => (
          <div key={i} className="flex gap-6 border-b border-hairline px-4 py-4 last:border-b-0">
            <Bar className="h-4 w-48" />
            <Bar className="h-4 flex-1" />
            <Bar className="h-4 w-20" />
          </div>
        ))}
      </div>
    </section>
  );
}

export function HeroSkeleton() {
  return (
    <div aria-hidden="true" className="grid gap-10 md:grid-cols-[1fr_360px]">
      <div className="flex flex-col gap-4">
        <Bar className="h-3 w-72" />
        <Bar className="h-12 w-full max-w-2xl" />
        <Bar className="h-12 w-4/5 max-w-xl" />
        <Bar className="h-5 w-full max-w-2xl" />
      </div>
      <div className="flex flex-col gap-4 border-t-2 border-ink pt-6 md:border-t-0 md:border-l md:pl-7 md:pt-0">
        <Bar className="h-3 w-28" />
        <Bar className="h-24 w-48" />
        <Bar className="h-4 w-full" />
        <Bar className="h-4 w-full" />
      </div>
    </div>
  );
}

export function LoadingNote() {
  return (
    <p role="status" className="sr-only">
      Loading
    </p>
  );
}
