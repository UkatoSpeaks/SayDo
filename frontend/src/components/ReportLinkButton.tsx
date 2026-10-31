"use client";

import { useState } from "react";

/** Placeholder feedback hook: records the report in the browser console for now. */
export function ReportLinkButton({ threadId, names }: { threadId: string; names: string[] }) {
  const [sent, setSent] = useState(false);
  return (
    <div className="mt-3">
      <button
        type="button"
        disabled={sent}
        onClick={() => {
          console.info("[SayDo] wrong-link report", { threadId, names, at: new Date().toISOString() });
          setSent(true);
        }}
        className="border border-ink bg-transparent px-3.5 py-1.5 text-[14px] font-semibold text-ink hover:bg-ink hover:text-paper disabled:cursor-default disabled:border-rule disabled:text-muted disabled:hover:bg-transparent"
      >
        {sent ? "Reported" : "Report a wrong link"}
      </button>
      <p role="status" className="mt-2 text-[13px] text-muted">
        {sent ? "Thanks. The report was noted for review." : ""}
      </p>
    </div>
  );
}
