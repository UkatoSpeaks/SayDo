"use client";

import { animate, inView, stagger } from "motion/react";
import { useLayoutEffect, useRef } from "react";
import { motionAllowed } from "@/lib/motion";

/** Fades its children in once, when they scroll into view. With `selector`, the
 * matching descendants fade in one after another. Opacity and transform only, so
 * nothing moves the layout; the server renders everything visible. */
export function Reveal({
  children,
  selector,
  step = 0.08,
  delay = 0,
  y = 8,
  amount = 0.2,
  className,
  as: Tag = "div",
}: {
  children: React.ReactNode;
  selector?: string;
  step?: number;
  delay?: number;
  y?: number;
  amount?: number;
  className?: string;
  as?: "div" | "section" | "dl";
}) {
  const root = useRef<HTMLElement>(null);

  useLayoutEffect(() => {
    const el = root.current;
    if (!el) return;
    const targets = selector ? Array.from(el.querySelectorAll<HTMLElement>(selector)) : [el];
    const allowed = motionAllowed();
    if (allowed) for (const t of targets) t.style.opacity = "0";
    el.setAttribute("data-ready", "");
    if (!allowed || targets.length === 0) return;
    let controls: { complete: () => void } | undefined;
    const unwatch = inView(
      el,
      () => {
        controls = animate(
          targets,
          { opacity: [0, 1], y: [y, 0] },
          { duration: 0.45, ease: "easeOut", delay: stagger(step, { startDelay: delay }) },
        );
      },
      { amount },
    );
    return () => {
      unwatch();
      controls?.complete();
      for (const t of targets) {
        t.style.opacity = "";
        t.style.transform = "";
      }
    };
  }, [selector, step, delay, y, amount]);

  return (
    <Tag ref={root as React.Ref<never>} data-anim="" data-reveal="" className={className}>
      {children}
    </Tag>
  );
}
