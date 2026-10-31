"use client";

import { useEffect, useRef } from "react";
import type { CompareRow } from "@/lib/compare";
import type { LinkQuote } from "@/lib/home";
import { BulletBar } from "../BulletBar";
import { VerdictLabel } from "../VerdictLabel";

type Gsap = typeof import("gsap").gsap;
let registered: Promise<Gsap> | null = null;

/** GSAP and ScrollTrigger load only here, once, after the page is interactive. */
function loadGsap(): Promise<Gsap> {
  registered ??= Promise.all([import("gsap"), import("gsap/ScrollTrigger")]).then(([g, st]) => {
    g.gsap.registerPlugin(st.ScrollTrigger);
    return g.gsap;
  });
  return registered;
}

// Pin only where the whole section fits in the viewport; elsewhere, reveal on entry.
const DESKTOP = "(min-width: 64rem) and (min-height: 800px) and (prefers-reduced-motion: no-preference)";
const SMALL =
  "(max-width: 63.99rem) and (prefers-reduced-motion: no-preference), (max-height: 799px) and (prefers-reduced-motion: no-preference)";

const TRANSCRIPT = [
  { speaker: "Moderator", text: "I now hand the call over to Mr. Arjun Mehta, Managing Director." },
  { speaker: "Arjun Mehta", text: "Thank you. Our Pune facility is being expanded to meet demand." },
  {
    speaker: "Arjun Mehta",
    text: "We will commission Line 4 in Q2 FY26, adding 40,000 tonnes of capacity.",
    promise: true,
  },
  { speaker: "Arjun Mehta", text: "We will launch our premium Aura range of products in H2 FY26." },
];

const STEPS = [
  {
    title: "Read",
    body: "We extract every forward-looking statement from management, keeping the exact words and page.",
  },
  {
    title: "Link",
    body: "The same promise is followed across quarters, even when it’s renamed, raised, lowered or delayed.",
  },
  {
    title: "Verify",
    body: "Numbers are compared in code against reported results. AI is used only to read text, and every quote is checked against the source.",
  },
];

/** Three ruled steps, each with a small live visual. On desktop the section pins and
 * scroll scrubs through the steps; on small screens each step plays once on entry.
 * Under reduced motion it stays as rendered: every step in its final state. */
export function HowItWorks({
  quotes,
  bullet,
  children,
}: {
  quotes: [LinkQuote, LinkQuote];
  bullet: CompareRow | null;
  /** The section header, rendered by the server. */
  children: React.ReactNode;
}) {
  const root = useRef<HTMLElement>(null);

  useEffect(() => {
    let cancelled = false;
    let revert: (() => void) | undefined;
    loadGsap().then((gsap) => {
      const el = root.current;
      if (cancelled || !el) return;
      const ctx = gsap.context(() => {
        const q = gsap.utils.selector(el);
        const css = getComputedStyle(document.documentElement);
        const ink = css.getPropertyValue("--color-ink").trim() || "#121418";
        const muted = css.getPropertyValue("--color-muted").trim() || "#5e5a53";
        const rows = q<HTMLElement>("[data-step]");
        const highlight = q("[data-part='highlight']");
        const speaker = q("[data-part='speaker']");
        const link = q("[data-part='link']");
        const linkLabel = q("[data-part='link-label']");
        const mark = q<HTMLElement>("[data-part='actual']");
        const verdict = q("[data-part='verdict']");
        const markFrom = () => -(mark[0]?.offsetLeft ?? 0);

        /** Each step's visual, from its start state to its end state. */
        const visuals = [
          (tl: gsap.core.Timeline) =>
            tl
              .fromTo(highlight, { scaleX: 0 }, { scaleX: 1, duration: 1, ease: "power2.out" })
              .fromTo(speaker, { opacity: 0 }, { opacity: 1, duration: 0.4 }, "<0.4"),
          (tl: gsap.core.Timeline) =>
            tl
              .fromTo(link, { scaleY: 0 }, { scaleY: 1, duration: 1, ease: "power2.inOut" })
              .fromTo(linkLabel, { opacity: 0 }, { opacity: 1, duration: 0.4 }),
          (tl: gsap.core.Timeline) =>
            tl
              .fromTo(mark, { x: markFrom }, { x: 0, duration: 1, ease: "power2.out" })
              .fromTo(verdict, { opacity: 0, scale: 1.3 }, { opacity: 1, scale: 1, duration: 0.3 }),
        ];

        const mm = gsap.matchMedia();
        mm.add(DESKTOP, () => {
          gsap.set(rows, { "--step-ink": muted });
          const tl = gsap.timeline({
            defaults: { ease: "none" },
            scrollTrigger: {
              trigger: el,
              start: "top top",
              end: "+=1800",
              pin: true,
              // The page column is a flex container, where ScrollTrigger turns spacing off.
              pinSpacing: true,
              scrub: 0.6,
              invalidateOnRefresh: true,
            },
          });
          rows.forEach((row, i) => {
            tl.to(row, { "--step-ink": ink, duration: 0.3 });
            if (i > 0) tl.to(rows[i - 1]!, { "--step-ink": muted, duration: 0.3 }, "<");
            visuals[i]?.(tl);
            tl.to({}, { duration: 0.5 });
          });
        });
        mm.add(SMALL, () => {
          rows.forEach((row, i) => {
            const tl = gsap.timeline({
              scrollTrigger: { trigger: row, start: "top 80%", once: true },
            });
            tl.fromTo(row, { opacity: 0, y: 16 }, { opacity: 1, y: 0, duration: 0.45, ease: "power2.out" });
            visuals[i]?.(tl);
          });
        });
      }, el);
      revert = () => ctx.revert();
    });
    return () => {
      cancelled = true;
      revert?.();
    };
  }, []);

  return (
    <section ref={root} aria-labelledby="how-title" id="how-it-works" className="scroll-mt-6 bg-paper">
      {children}
      <ol className="border-b border-rule">
        {STEPS.map((step, i) => (
          <li
            key={step.title}
            data-step={i}
            className="grid gap-x-8 gap-y-4 border-t border-rule py-6 lg:grid-cols-[56px_minmax(0,1fr)_minmax(0,1.35fr)] lg:items-center"
            style={{ color: "var(--step-ink, var(--color-ink))" }}
          >
            <span className="font-mono text-[15px]" aria-hidden="true">
              0{i + 1}
            </span>
            <div>
              <h3 className="text-[24px] leading-tight font-bold tracking-[-0.02em]">
                <span className="sr-only">Step {i + 1}: </span>
                {step.title}
              </h3>
              <p className="mt-2 max-w-[44ch] text-[17px] leading-[1.5]">{step.body}</p>
            </div>
            <div className="border border-rule bg-insert p-4 text-ink">
              {i === 0 ? <ReadVisual /> : i === 1 ? <LinkVisual quotes={quotes} /> : <VerifyVisual bullet={bullet} />}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

function ReadVisual() {
  return (
    <div className="font-mono text-[13px] leading-[1.55]">
      <p className="mb-2 text-[11px] tracking-[0.08em] text-muted uppercase">Q1FY26 call · transcript p. 2</p>
      {TRANSCRIPT.map((line) =>
        line.promise ? (
          <div key={line.text} className="my-1">
            <p
              data-part="speaker"
              className="font-sans text-[11px] font-semibold tracking-[0.08em] text-ink uppercase"
            >
              {line.speaker} · Managing Director · promise
            </p>
            <p className="relative">
              <span
                data-part="highlight"
                aria-hidden="true"
                className="absolute -inset-x-1 inset-y-0 origin-left bg-highlight"
              />
              <mark className="relative bg-transparent">{line.text}</mark>
            </p>
          </div>
        ) : (
          <p key={line.text} className="text-ink-2">
            <span className="text-muted">{line.speaker}:</span> {line.text}
          </p>
        ),
      )}
    </div>
  );
}

function LinkVisual({ quotes }: { quotes: [LinkQuote, LinkQuote] }) {
  const cite = (q: LinkQuote) =>
    [q.period, q.speaker, q.page ? `p. ${q.page}` : null].filter(Boolean).join(" · ");
  return (
    <div className="relative font-mono text-[13px] leading-[1.55]">
      <span
        data-part="link"
        aria-hidden="true"
        className="absolute top-1 bottom-1 left-0 w-0.5 origin-top bg-ink"
      />
      <blockquote className="pl-4">
        <p>&ldquo;{quotes[0].quote}&rdquo;</p>
        <footer className="mt-1 text-[11px] text-muted">{cite(quotes[0])}</footer>
      </blockquote>
      <p
        data-part="link-label"
        className="my-3 pl-4 font-sans text-[11px] font-semibold tracking-[0.08em] text-ink uppercase"
      >
        ↕ same promise
      </p>
      <blockquote className="pl-4">
        <p>&ldquo;{quotes[1].quote}&rdquo;</p>
        <footer className="mt-1 text-[11px] text-muted">{cite(quotes[1])}</footer>
      </blockquote>
    </div>
  );
}

function VerifyVisual({ bullet }: { bullet: CompareRow | null }) {
  if (!bullet) return null;
  return (
    <div>
      <p className="mb-3 flex flex-wrap justify-between gap-x-4 font-mono text-[13px]">
        <span className="font-semibold">{bullet.label}</span>
        <span className="text-ink-2">
          target {bullet.target} · actual {bullet.actual}
        </span>
      </p>
      <div className="overflow-hidden py-1">
        <BulletBar input={bullet.input} tone={bullet.tone} label={bullet.ariaLabel} />
      </div>
      <p className="mt-3 flex items-center justify-between gap-4 font-mono text-[13px]">
        <span className={bullet.tone === "missed" ? "text-missed" : "text-ink-2"}>gap {bullet.gap}</span>
        <span data-part="verdict" className="inline-block origin-right">
          <VerdictLabel status={bullet.tone === "missed" ? "missed" : "delivered"} />
        </span>
      </p>
    </div>
  );
}
