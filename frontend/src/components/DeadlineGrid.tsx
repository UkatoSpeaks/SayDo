import type { ThreadDetail } from "@/lib/api";
import { ordinalLabel, periodOrdinal } from "@/lib/periods";
import { changeTag, TONE_TEXT } from "@/lib/status";

interface GridRow {
  key: string;
  call: string;
  callIndex: number;
  dueIndex: number;
  due: string;
  current: boolean;
  tag: ReturnType<typeof changeTag>;
}

export function deadlineRows(thread: ThreadDetail): { periods: string[]; rows: GridRow[] } {
  const dated = thread.mentions.filter((m) => m.target_period && periodOrdinal(m.target_period) !== null);
  if (dated.length === 0) return { periods: [], rows: [] };
  const ordinals = dated.flatMap((m) => [periodOrdinal(m.period_label)!, periodOrdinal(m.target_period!)!]);
  const start = Math.min(...ordinals);
  const end = Math.max(...ordinals);
  const periods = Array.from({ length: end - start + 1 }, (_, i) => ordinalLabel(start + i));
  const current = thread.target_period;
  const rows = dated.map((m) => ({
    key: m.claim_id,
    call: m.period_label,
    callIndex: periodOrdinal(m.period_label)! - start,
    dueIndex: periodOrdinal(m.target_period!)! - start,
    due: m.target_period!,
    current: m.target_period === current,
    tag: changeTag(m.change_type),
  }));
  return { periods, rows };
}

/** One row per mention: a dot on the call, a line to the deadline it gave, a box there. */
export function DeadlineGrid({ thread }: { thread: ThreadDetail }) {
  const { periods, rows } = deadlineRows(thread);
  if (rows.length === 0) return null;
  const n = periods.length;
  const centre = (index: number) => `${((index + 0.5) / n) * 100}%`;
  const template = `150px repeat(${n}, minmax(96px, 1fr))`;
  return (
    <div className="relative overflow-x-auto border border-rule bg-insert" role="region" aria-label="Deadline by call" tabIndex={0}>
      <div className="grid" style={{ gridTemplateColumns: template, minWidth: 150 + n * 96 }}>
        <div className="border-b border-ink px-4 py-2.5 text-[13px] font-semibold text-muted">Said on</div>
        {periods.map((p) => (
          <div key={p} className="border-b border-l border-ink border-l-hairline px-2 py-2.5 text-center font-mono text-[12px] font-semibold text-muted">
            {p}
          </div>
        ))}
        {rows.map((row) => {
          const from = Math.min(row.callIndex, row.dueIndex);
          const to = Math.max(row.callIndex, row.dueIndex);
          return (
            <div key={row.key} className="contents">
              <div className="border-b border-hairline px-4 py-4">
                <p className="font-mono text-[14px] font-semibold">{row.call} call</p>
                <p className={`mt-0.5 text-[11px] font-bold tracking-[0.06em] uppercase ${TONE_TEXT[row.tag.tone]}`}>
                  {row.tag.label}
                </p>
              </div>
              <div className="relative border-b border-hairline" style={{ gridColumn: `span ${n}` }}>
                {periods.map((p, i) => (
                  <span
                    key={p}
                    aria-hidden="true"
                    className="absolute inset-y-0 border-l border-hairline"
                    style={{ left: `${(i / n) * 100}%` }}
                  />
                ))}
                <span
                  aria-hidden="true"
                  className="absolute top-1/2 h-0.5 -translate-y-1/2 bg-ink"
                  style={{ left: centre(from), width: `${((to - from) / n) * 100}%` }}
                />
                <span
                  aria-hidden="true"
                  className="dot absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 bg-ink"
                  style={{ left: centre(row.callIndex) }}
                />
                <span
                  className={`absolute top-1/2 -translate-x-1/2 -translate-y-1/2 border px-2 py-1 text-center font-mono text-[12px] leading-tight whitespace-nowrap ${
                    row.current ? "border-ink bg-ink text-white" : "border-moved bg-white text-moved"
                  }`}
                  style={{ left: centre(row.dueIndex) }}
                >
                  <span className="sr-only">On the {row.call} call, </span>due {row.due}
                  {row.current ? null : (
                    <span className="block">
                      <span aria-hidden="true">{"✕"}</span> moved
                    </span>
                  )}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
