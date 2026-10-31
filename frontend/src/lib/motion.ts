/** Shared rules for the landing page's animation.
 *
 * Server HTML always holds the final state (numbers at their value, every row shown),
 * so the page reads correctly without JavaScript and in screenshots. On the client, a
 * component resets its elements to their start state in a layout effect (before paint)
 * and marks its root `data-ready`. Until then, the inline script below plus the
 * `[data-anim]` rules in globals.css hide the parts that will animate, so there is no
 * flash of the final state before hydration. */

export const REDUCED_QUERY = "(prefers-reduced-motion: reduce)";

/** False on the server, without matchMedia, and when the reader asks for reduced motion. */
export function motionAllowed(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return !window.matchMedia(REDUCED_QUERY).matches;
}

/** Runs during HTML parsing, before first paint: turns on the pre-hydration rules. */
export const MOTION_FLAG_SCRIPT = `try{if(!matchMedia("${REDUCED_QUERY}").matches)document.documentElement.dataset.motion="on"}catch(e){}`;
