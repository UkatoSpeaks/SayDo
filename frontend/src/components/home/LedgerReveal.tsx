"use client";

import { animate, inView, stagger } from "motion/react";
import { useLayoutEffect, useRef } from "react";
import { motionAllowed } from "@/lib/motion";

const CELL_STEP = 0.06;

/** Wraps the ledger table: on first view, cells appear column by column from the first
 * call to the latest, then the verdicts stamp in. Runs once. */
export function LedgerReveal({ children }: { children: React.ReactNode }) {
  const root = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const el = root.current;
    if (!el) return;
    const rows = Array.from(el.querySelectorAll("tbody tr"));
    // Column-major order; empty cells don't take a turn.
    const columns = rows[0]?.querySelectorAll("td[data-kind]").length ?? 0;
    const cells: HTMLElement[] = [];
    for (let col = 0; col < columns; col++) {
      for (const row of rows) {
        const cell = row.querySelectorAll<HTMLElement>("td[data-kind]")[col];
        if (cell && cell.dataset.kind !== "none") cells.push(cell);
      }
    }
    const verdicts = Array.from(el.querySelectorAll<HTMLElement>("td[data-col='verdict'] > *"));
    const targets = [...cells, ...verdicts];
    const allowed = motionAllowed();
    if (allowed) for (const t of targets) t.style.opacity = "0";
    el.setAttribute("data-ready", "");
    if (!allowed || targets.length === 0) return;

    const running: { complete: () => void }[] = [];
    const unwatch = inView(
      el,
      () => {
        if (cells.length)
          running.push(animate(cells, { opacity: [0, 1] }, { duration: 0.25, delay: stagger(CELL_STEP) }));
        if (verdicts.length)
          running.push(
            animate(
              verdicts,
            { opacity: [0, 1], scale: [1.35, 1] },
              {
                duration: 0.22,
                ease: [0.2, 0.9, 0.3, 1.2],
                delay: stagger(CELL_STEP, { startDelay: cells.length * CELL_STEP + 0.15 }),
              },
            ),
          );
      },
      { amount: 0.35 },
    );
    return () => {
      unwatch();
      for (const r of running) r.complete();
      for (const t of targets) {
        t.style.opacity = "";
        t.style.transform = "";
      }
    };
  }, []);

  return (
    <div ref={root} data-anim="" data-reveal="">
      {children}
    </div>
  );
}
