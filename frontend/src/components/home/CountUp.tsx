"use client";

import { animate, inView } from "motion/react";
import { useLayoutEffect, useRef } from "react";
import { formatCount } from "@/lib/home";
import { motionAllowed } from "@/lib/motion";

/** A number that counts from 0 to `value` when it enters the viewport (or when
 * `play` turns true). The server renders the final value; an invisible copy of it
 * reserves the width so the count causes no layout shift. */
export function CountUp({
  value,
  decimals = 0,
  duration = 1.2,
  play,
  className,
}: {
  value: number;
  decimals?: number;
  duration?: number;
  /** Controlled mode: count when this turns true, reset to 0 when it turns false. */
  play?: boolean;
  className?: string;
}) {
  const root = useRef<HTMLSpanElement>(null);
  const number = useRef<HTMLSpanElement>(null);
  const final = formatCount(value, decimals);

  useLayoutEffect(() => {
    const el = number.current;
    root.current?.setAttribute("data-ready", "");
    if (!el || !motionAllowed()) return;
    const set = (v: number) => {
      el.textContent = formatCount(v, decimals);
    };
    if (play === false) {
      set(0);
      return;
    }
    const run = () => {
      const controls = animate(0, value, { duration, ease: "easeOut", onUpdate: set });
      return () => controls.complete();
    };
    if (play === true) return run();
    set(0);
    let stop: (() => void) | undefined;
    const unwatch = inView(
      el,
      () => {
        stop = run();
      },
      { amount: 0.6 },
    );
    return () => {
      unwatch();
      if (stop) stop();
      else el.textContent = final;
    };
  }, [value, decimals, duration, play, final]);

  return (
    <span ref={root} data-anim="" className={`inline-grid ${className ?? ""}`}>
      {/* Reserves the final width; a pseudo-element, so the number isn't in the text twice. */}
      <span
        aria-hidden="true"
        data-size={final}
        className="invisible col-start-1 row-start-1 before:content-[attr(data-size)]"
      />
      <span ref={number} data-await="" data-part="count" className="col-start-1 row-start-1 text-right">
        {final}
      </span>
    </span>
  );
}
