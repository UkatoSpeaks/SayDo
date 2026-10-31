"use client";

import { useState } from "react";
import { TickerEntry, type TickerItem } from "../Ticker";

/** The ticker strip as a slow continuous loop. Two identical copies scroll by half
 * their joint width, so the loop is seamless. Pauses on hover, on keyboard focus and
 * with the button; under reduced motion it is a static, wrapping row (globals.css). */
export function Marquee({ items, demo = false }: { items: TickerItem[]; demo?: boolean }) {
  const [paused, setPaused] = useState(false);
  const copy = (hidden: boolean) => (
    <ul
      className="marquee-copy flex shrink-0 items-stretch"
      aria-label={hidden ? undefined : "Key figures"}
      aria-hidden={hidden || undefined}
    >
      {items.map((item, index) => (
        <TickerEntry key={`${item.label ?? ""}-${index}`} item={item} divider />
      ))}
    </ul>
  );
  return (
    <div className="bg-ticker text-ticker-text">
      <div
        className="marquee mx-auto flex max-w-[1280px] items-stretch font-mono text-[12px] leading-none tracking-[0.04em]"
        data-paused={paused || undefined}
      >
        <div className="marquee-window min-w-0 flex-1 overflow-hidden">
          <div className="marquee-track flex w-max">
            {copy(false)}
            {copy(true)}
          </div>
        </div>
        <div className="flex shrink-0 items-stretch border-l border-ticker-divider">
          {demo ? <p className="flex items-center px-3 py-2.5 font-semibold text-demo">DEMO DATA</p> : null}
          <button
            type="button"
            onClick={() => setPaused((p) => !p)}
            aria-label={paused ? "Play ticker" : "Pause ticker"}
            className="marquee-toggle border-l border-ticker-divider px-3 py-2.5 font-semibold uppercase hover:text-ticker-number focus-visible:outline-ticker-number focus-visible:-outline-offset-2"
          >
            {paused ? "Play" : "Pause"}
          </button>
        </div>
      </div>
    </div>
  );
}
