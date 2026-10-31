import { CELL_STYLES, LEGEND, TONE_TEXT } from "@/lib/status";

export function LedgerLegend() {
  return (
    <ul className="mb-3 flex flex-wrap gap-x-5 gap-y-1 font-mono text-[12px]" aria-label="Legend">
      {LEGEND.map((kind) => {
        const style = CELL_STYLES[kind];
        return (
          <li key={kind} className={TONE_TEXT[style.tone]}>
            <span aria-hidden="true">{style.glyph}</span> {style.label}
          </li>
        );
      })}
    </ul>
  );
}
