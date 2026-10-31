export interface TickerItem {
  /** Uppercase label, e.g. "SAY-DO". Omit for a bare value (the symbol). */
  label?: string;
  value: string;
  /** Numbers are set in the ticker's amber; text values stay light. */
  numeric?: boolean;
}

/** One figure in the strip: label, then the value in amber (numbers) or bold (symbol). */
export function TickerEntry({ item, divider }: { item: TickerItem; divider: boolean }) {
  return (
    <li className={`flex items-center gap-1.5 py-2.5 pr-3 whitespace-nowrap ${divider ? "border-l border-ticker-divider pl-3" : ""}`}>
      {item.label ? <span>{item.label}</span> : null}
      <span className={item.numeric ? "text-ticker-number" : item.label ? "" : "font-semibold"}>
        {item.value}
      </span>
    </li>
  );
}

export function Ticker({ items, demo = false }: { items: TickerItem[]; demo?: boolean }) {
  return (
    <div className="bg-ticker text-ticker-text">
      <div className="mx-auto flex max-w-[1280px] flex-wrap items-stretch justify-between gap-x-6 px-6 font-mono text-[12px] leading-none tracking-[0.04em]">
        <ul className="flex flex-wrap items-stretch" aria-label="Key figures">
          {items.map((item, index) => (
            <TickerEntry key={`${item.label ?? ""}-${index}`} item={item} divider={index > 0} />
          ))}
        </ul>
        {demo ? (
          <p className="flex items-center py-2.5 font-semibold text-demo">DEMO DATA</p>
        ) : null}
      </div>
    </div>
  );
}
