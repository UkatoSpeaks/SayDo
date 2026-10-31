import type { VerificationStatus } from "@/lib/api";
import { TONE_BG, TONE_TEXT, verdictOf } from "@/lib/status";

/** Uppercase status label with a coloured square; the label carries the meaning. */
export function VerdictLabel({
  status,
  keptAfterLowering = false,
  size = "sm",
}: {
  status: VerificationStatus | null | undefined;
  keptAfterLowering?: boolean;
  size?: "sm" | "lg";
}) {
  const verdict = verdictOf(status, keptAfterLowering);
  const big = size === "lg";
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap font-bold uppercase ${TONE_TEXT[verdict.tone]} ${
        big ? "gap-3 text-[28px] tracking-[0.02em]" : "gap-2 text-[13px] tracking-[0.06em]"
      }`}
    >
      <span
        aria-hidden="true"
        className={`inline-block shrink-0 ${TONE_BG[verdict.tone]} ${big ? "size-4" : "size-[9px]"}`}
      />
      {verdict.label}
    </span>
  );
}
