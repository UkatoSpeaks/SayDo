import { bulletGeometry, type BulletInput } from "@/lib/bullet";
import type { Tone } from "@/lib/status";
import { TONE_BG } from "@/lib/status";

/** Track, promised band, actual mark and (when lowered) the original target, dashed. */
export function BulletBar({
  input,
  tone,
  label,
}: {
  input: BulletInput;
  tone: Tone;
  label: string;
}) {
  const g = bulletGeometry(input);
  return (
    <div role="img" aria-label={label} className="relative h-[22px] w-full min-w-[160px] bg-track">
      <span
        data-part="band"
        className="absolute inset-y-0 bg-band"
        style={{ left: `${g.band.left}%`, width: `${g.band.width}%` }}
      />
      {g.original !== null ? (
        <span
          data-part="original"
          className="absolute -inset-y-1 border-l-2 border-dashed border-ink"
          style={{ left: `calc(${g.original}% - 1px)` }}
        />
      ) : null}
      <span
        data-part="actual"
        className={`absolute -inset-y-1 w-1 ${TONE_BG[tone]}`}
        style={{ left: `calc(${g.actual}% - 2px)` }}
      />
    </div>
  );
}
