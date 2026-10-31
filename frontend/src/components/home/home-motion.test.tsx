import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FALLBACK_EXAMPLES, HERO_TIMING } from "@/lib/home";
import { CountUp } from "./CountUp";
import { HeroStrike } from "./HeroStrike";
import { Marquee } from "./Marquee";

// Motion is replaced by stubs that jump straight to the end state, so the tests check
// what each component asks for, not the browser's animation engine.
const motion = vi.hoisted(() => ({
  inViewNow: true,
  animate: vi.fn(),
}));
vi.mock("motion/react", () => ({
  animate: (...args: unknown[]) => {
    motion.animate(...args);
    const [from, to, options] = args as [unknown, unknown, { onUpdate?: (v: unknown) => void }];
    if (typeof from === "number") options?.onUpdate?.(to);
    return { complete: () => {} };
  },
  inView: (_el: Element, callback: () => void) => {
    if (motion.inViewNow) callback();
    return () => {};
  },
  stagger: () => 0,
}));

function prefersReducedMotion(reduce: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: reduce && query.includes("reduce"),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    onchange: null,
    dispatchEvent: () => false,
  }));
}

const count = (container: HTMLElement) => container.querySelector('[data-part="count"]')!.textContent;
const slot = (container: HTMLElement, i: number) => container.querySelector<HTMLElement>(`[data-slot="${i}"]`)!;
const part = (el: HTMLElement, name: string) => el.querySelector<HTMLElement>(`[data-part="${name}"]`)!;

beforeEach(() => {
  motion.inViewNow = true;
  motion.animate.mockClear();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("CountUp", () => {
  it("renders the final value and reserves its width", () => {
    prefersReducedMotion(true);
    const { container } = render(<CountUp value={4620} />);
    expect(count(container)).toBe("4,620");
    expect(container.querySelector("[data-size]")).toHaveAttribute("data-size", "4,620");
    expect(container.textContent).toBe("4,620");
    expect(motion.animate).not.toHaveBeenCalled();
  });

  it("starts at 0 off-screen and counts to the final value in view", () => {
    prefersReducedMotion(false);
    motion.inViewNow = false;
    const off = render(<CountUp value={75} />);
    expect(count(off.container)).toBe("0");
    off.unmount();

    motion.inViewNow = true;
    const { container } = render(<CountUp value={62.5} decimals={1} />);
    expect(motion.animate).toHaveBeenCalledWith(0, 62.5, expect.objectContaining({ duration: 1.2 }));
    expect(count(container)).toBe("62.5");
  });
});

describe("HeroStrike", () => {
  it("with reduced motion, shows the first example in its final state and never cycles", () => {
    prefersReducedMotion(true);
    vi.useFakeTimers();
    const { container } = render(<HeroStrike examples={FALLBACK_EXAMPLES} />);
    const first = slot(container, 0);
    expect(first).not.toHaveClass("invisible");
    expect(first).toHaveTextContent("They said: ₹5,000 crore (struck through) revenue in FY26.");
    expect(part(first, "did")).toHaveTextContent("They did: ₹4,620 crore.");
    expect(part(first, "stamp")).toHaveTextContent("−7.6% · MISSED");
    expect(part(first, "strike").style.transform).toBe("");
    expect(part(first, "did").style.opacity).toBe("");
    expect(part(first, "stamp").style.opacity).toBe("");
    expect(count(first)).toBe("4,620");

    act(() => vi.advanceTimersByTime(30_000));
    expect(slot(container, 0)).not.toHaveClass("invisible");
    expect(slot(container, 1)).toHaveClass("invisible");
    expect(motion.animate).not.toHaveBeenCalled();
  });

  it("with motion, starts from the opening line and cycles through the examples", () => {
    prefersReducedMotion(false);
    vi.useFakeTimers();
    const { container } = render(<HeroStrike examples={FALLBACK_EXAMPLES} />);
    const first = slot(container, 0);
    expect(part(first, "strike").style.transform).toBe("scaleX(0)");
    expect(part(first, "did").style.opacity).toBe("0");
    expect(part(first, "stamp").style.opacity).toBe("0");

    act(() => vi.advanceTimersByTime(HERO_TIMING.said));
    expect(motion.animate).toHaveBeenCalledWith(part(first, "strike"), { scaleX: [0, 1] }, expect.anything());

    act(() => vi.advanceTimersByTime(HERO_TIMING.strike));
    expect(motion.animate).toHaveBeenCalledWith(0, 4620, expect.anything());
    expect(count(first)).toBe("4,620");

    act(() => vi.advanceTimersByTime(HERO_TIMING.did));
    expect(motion.animate).toHaveBeenCalledWith(part(first, "stamp"), expect.anything(), expect.anything());
    act(() => vi.advanceTimersByTime(HERO_TIMING.stamp));
    expect(slot(container, 0)).toHaveClass("invisible");
    expect(slot(container, 1)).not.toHaveClass("invisible");
    expect(slot(container, 1)).toHaveTextContent("They said: commissioned in Q2FY26 (struck through).");
    expect(part(slot(container, 1), "did").style.opacity).toBe("0");
  });
});

describe("Marquee", () => {
  it("lists the figures once for assistive tech and can be paused", () => {
    render(<Marquee items={[{ label: "SAY-DO", value: "75%", numeric: true }]} demo />);
    expect(screen.getAllByRole("list", { name: "Key figures" })).toHaveLength(1);
    const button = screen.getByRole("button", { name: "Pause ticker" });
    act(() => button.click());
    expect(screen.getByRole("button", { name: "Play ticker" })).toHaveTextContent("Play");
  });
});
