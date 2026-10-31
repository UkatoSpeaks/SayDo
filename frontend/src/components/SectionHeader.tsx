/** 2px ink top rule, then index, title and an optional caption on one baseline. */
export function SectionHeader({
  index,
  title,
  caption,
  id,
}: {
  index: string;
  title: string;
  caption?: React.ReactNode;
  id?: string;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-baseline gap-x-4 gap-y-1 border-t-2 border-ink pt-3">
      <span className="font-mono text-[13px] text-muted" aria-hidden="true">
        {index}
      </span>
      <h2 id={id} className="text-[22px] font-bold leading-tight tracking-[-0.02em]">
        <span className="sr-only">{index} </span>
        {title}
      </h2>
      {caption ? <p className="text-[14px] text-muted">{caption}</p> : null}
    </div>
  );
}
