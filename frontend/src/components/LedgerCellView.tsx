import type { LedgerCell } from "@/lib/api";
import { CELL_STYLES, TONE_TEXT, TONE_TINT } from "@/lib/status";

/** One ledger cell: glyph + text, tinted for kept/missed/moved/open. */
export function LedgerCellView({ cell }: { cell: LedgerCell }) {
  const style = CELL_STYLES[cell.kind];
  const tint = style.tinted ? TONE_TINT[style.tone] : "";
  const label = style.label ? `${cell.period}: ${style.label}${cell.text ? ` ${cell.text}` : ""}` : undefined;
  return (
    <td
      data-kind={cell.kind}
      className={`border-l border-hairline px-3 py-3 font-mono text-[13px] whitespace-nowrap ${tint} ${TONE_TEXT[style.tone]}`}
    >
      {cell.kind === "none" ? (
        <span className="sr-only">{cell.period}: no entry</span>
      ) : (
        <>
          <span aria-hidden="true" className="inline-flex items-baseline gap-1.5">
            {/* Moved cells already start with the arrow ("→ Q3FY26"). */}
            {cell.text.startsWith(style.glyph) ? null : <span>{style.glyph}</span>}
            {cell.text ? <span>{cell.text}</span> : null}
          </span>
          <span className="sr-only">{label}</span>
        </>
      )}
    </td>
  );
}
