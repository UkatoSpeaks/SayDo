"use client";

import { animate } from "motion/react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  HERO_FINAL,
  HERO_TIMING,
  heroShows,
  nextHeroStep,
  type HeroExample,
  type HeroStep,
} from "@/lib/home";
import { motionAllowed } from "@/lib/motion";
import { TONE_BG, TONE_TEXT } from "@/lib/status";
import { CountUp } from "./CountUp";

type Controls = { complete: () => void };

/** "They said … / They did …": the promised figure is struck through, the reported one
 * counts up and the verdict stamps in, cycling through the examples. All examples sit
 * in one grid cell so the box is as tall as the tallest and never changes height. */
export function HeroStrike({ examples }: { examples: HeroExample[] }) {
  const root = useRef<HTMLDivElement>(null);
  // null: not animating, so the first example stays in its final state (server render,
  // reduced motion). Otherwise the step being played.
  const [playing, setPlaying] = useState<HeroStep | null>(null);
  const running = playing !== null;
  const step = playing ?? HERO_FINAL;

  // Start from the first example's opening line, before the first paint.
  useLayoutEffect(() => {
    root.current?.setAttribute("data-ready", "");
    if (!motionAllowed() || examples.length === 0) return;
    // Deliberate: the server rendered the final state; switch to the start state
    // before the browser paints it.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPlaying({ index: 0, phase: "said" });
  }, [examples.length]);

  // Advance through the phases on a timer.
  useEffect(() => {
    if (!playing) return;
    const timer = window.setTimeout(
      () => setPlaying((s) => (s ? nextHeroStep(s, examples.length) : s)),
      HERO_TIMING[playing.phase],
    );
    return () => window.clearTimeout(timer);
  }, [playing, examples.length]);

  // Drive the active example's parts for the current phase.
  useLayoutEffect(() => {
    if (!running) return;
    const slot = root.current?.querySelector<HTMLElement>(`[data-slot="${step.index}"]`);
    if (!slot) return;
    const part = (name: string) => slot.querySelector<HTMLElement>(`[data-part="${name}"]`);
    const strike = part("strike");
    const did = part("did");
    const stamp = part("stamp");
    let controls: Controls | undefined;
    switch (step.phase) {
      case "said":
        if (strike) strike.style.transform = "scaleX(0)";
        if (did) did.style.opacity = "0";
        if (stamp) stamp.style.opacity = "0";
        break;
      case "strike":
        if (strike) controls = animate(strike, { scaleX: [0, 1] }, { duration: 0.6, ease: "easeOut" });
        break;
      case "did":
        if (did) controls = animate(did, { opacity: [0, 1], y: [6, 0] }, { duration: 0.3, ease: "easeOut" });
        break;
      case "stamp":
        if (stamp)
          controls = animate(
            stamp,
            { opacity: [0, 1], scale: [1.4, 1] },
            { duration: 0.22, ease: [0.2, 0.9, 0.3, 1.2] },
          );
        break;
    }
    return () => controls?.complete();
  }, [running, step.index, step.phase]);

  return (
    <div
      ref={root}
      data-anim=""
      data-hero-strike=""
      className="grid border border-rule bg-insert px-5 py-5 font-mono text-[17px] leading-[1.45] sm:text-[22px] xl:text-[28px]"
    >
      {examples.map((example, index) => {
        const active = index === step.index;
        const phase = active ? step.phase : "stamp";
        return (
          <div
            key={example.key}
            data-slot={index}
            aria-hidden={active ? undefined : true}
            className={`col-start-1 row-start-1 ${active ? "" : "invisible"}`}
          >
            <p className="mb-2 font-sans text-[12px] font-semibold tracking-[0.08em] text-muted uppercase">
              {example.caption}
            </p>
            <p className="whitespace-pre-wrap">
              {example.said.before}
              <span className="relative inline-block whitespace-nowrap">
                <span className={heroShows(phase, "strike") ? "text-muted" : ""}>{example.said.struck}</span>
                <span
                  data-part="strike"
                  data-await="strike"
                  aria-hidden="true"
                  className="absolute top-1/2 right-0 left-0 h-[2px] origin-left -translate-y-1/2 bg-missed"
                />
                <span className="sr-only"> (struck through)</span>
              </span>
              {example.said.after}
            </p>
            <p data-part="did" data-await="" className="mt-1 whitespace-pre-wrap">
              {example.did.before}
              {example.did.count ? (
                <CountUp
                  value={example.did.count.value}
                  decimals={example.did.count.decimals}
                  duration={0.8}
                  play={running ? active && heroShows(phase, "did") : undefined}
                />
              ) : null}
              {example.did.after}{" "}
              <span
                data-part="stamp"
                data-await=""
                className={`inline-flex items-center gap-2 font-sans text-[0.6em] font-bold tracking-[0.06em] whitespace-nowrap uppercase align-middle ${TONE_TEXT[example.stamp.tone]}`}
              >
                <span aria-hidden="true" className={`inline-block size-[0.6em] ${TONE_BG[example.stamp.tone]}`} />
                {example.stamp.text}
              </span>
            </p>
          </div>
        );
      })}
    </div>
  );
}
