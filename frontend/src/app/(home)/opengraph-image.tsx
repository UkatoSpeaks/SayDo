import { ImageResponse } from "next/og";
import { api, DEMO_SYMBOL } from "@/lib/api";
import { formatRatio } from "@/lib/format";

export const alt = "SayDo — Management says a lot. SayDo checks what they did. Say-Do ratio 75%.";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

const PAPER = "#f3f0e9";
const INK = "#121418";
const MUTED = "#5e5a53";
const RULE = "#d6cfc2";
const AMBER = "#e8b44c";

/** Geist Bold and Geist Mono from Google Fonts (TTF, which next/og can read). If the
 * build has no network, the image falls back to next/og's built-in Geist Regular. */
async function loadFonts() {
  try {
    const css = await fetch(
      "https://fonts.googleapis.com/css2?family=Geist:wght@700&family=Geist+Mono:wght@500&display=swap",
      { signal: AbortSignal.timeout(3000) },
    ).then((r) => r.text());
    const urls = [...css.matchAll(/src: url\((.+?)\) format\('(?:truetype|opentype)'\)/g)].map((m) => m[1]!);
    if (urls.length < 2) return undefined;
    const [sans, mono] = await Promise.all(
      urls.slice(0, 2).map((u) => fetch(u, { signal: AbortSignal.timeout(3000) }).then((r) => r.arrayBuffer())),
    );
    return [
      { name: "Geist", data: sans!, weight: 700 as const, style: "normal" as const },
      { name: "Geist Mono", data: mono!, weight: 500 as const, style: "normal" as const },
    ];
  } catch {
    return undefined;
  }
}

async function demoFigures(): Promise<{ ratio: string; kept: number; total: number }> {
  try {
    const companies = await api.companies();
    const s = companies.find((c) => c.nse_symbol === DEMO_SYMBOL)?.say_do_latest;
    if (s?.ratio != null) return { ratio: formatRatio(s.ratio), kept: s.kept, total: s.total };
  } catch {
    // API down at build time: use the seeded demo's figures.
  }
  return { ratio: "75%", kept: 6, total: 8 };
}

export default async function OpenGraphImage() {
  const [fonts, { ratio, kept, total }] = await Promise.all([loadFonts(), demoFigures()]);
  const mono = fonts ? "Geist Mono" : undefined;
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", flexDirection: "column", background: PAPER, color: INK, fontFamily: "Geist" }}>
        <div style={{ display: "flex", height: 44, background: INK, color: PAPER, alignItems: "center", padding: "0 56px", fontFamily: mono, fontSize: 20, letterSpacing: 1 }}>
          <span>SAY-DO</span>
          <span style={{ color: AMBER, marginLeft: 10 }}>{ratio}</span>
          <span style={{ marginLeft: 28 }}>KEPT</span>
          <span style={{ color: AMBER, marginLeft: 10 }}>{kept}</span>
          <span style={{ marginLeft: 28 }}>OF</span>
          <span style={{ color: AMBER, marginLeft: 10 }}>{total}</span>
        </div>
        <div style={{ display: "flex", margin: "0 56px", padding: "26px 0 22px", borderBottom: `3px solid ${INK}`, fontSize: 40, letterSpacing: -1.5 }}>
          <span style={{ fontWeight: 400 }}>Say</span>
          <span style={{ fontWeight: 700 }}>Do</span>
        </div>
        <div style={{ display: "flex", flex: 1, margin: "0 56px", padding: "40px 0 48px" }}>
          <div style={{ display: "flex", flexDirection: "column", flex: 1, paddingRight: 48 }}>
            <div style={{ fontSize: 22, color: MUTED, letterSpacing: 2, textTransform: "uppercase" }}>
              For investors who read the fine print
            </div>
            <div style={{ fontSize: 70, fontWeight: 700, lineHeight: 1.02, letterSpacing: -2.8, marginTop: 20 }}>
              Management says a lot. SayDo checks what they did.
            </div>
          </div>
          <div style={{ display: "flex", flexDirection: "column", justifyContent: "center", width: 330, paddingLeft: 36, borderLeft: `2px solid ${INK}` }}>
            <div style={{ fontSize: 20, color: MUTED, letterSpacing: 2, textTransform: "uppercase" }}>Say-Do ratio</div>
            <div style={{ fontFamily: mono, fontSize: 132, lineHeight: 1, letterSpacing: -5, marginTop: 8 }}>{ratio}</div>
            <div style={{ fontSize: 20, color: MUTED, marginTop: 14, paddingTop: 12, borderTop: `1px solid ${RULE}` }}>
              {`Demo company · ${kept} of ${total} promises kept`}
            </div>
          </div>
        </div>
      </div>
    ),
    { ...size, fonts },
  );
}
