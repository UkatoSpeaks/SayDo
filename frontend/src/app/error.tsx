"use client";

import Link from "next/link";
import { useEffect } from "react";
import { Footer } from "@/components/Footer";
import { Ticker } from "@/components/Ticker";

// Error boundaries are client components, so this renders without the server-side
// masthead (its company finder fetches on the server).
export default function ErrorPage({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.warn("Page failed to render:", error.message);
  }, [error]);
  const unreachable = /fetch failed|ECONNREFUSED|Failed to fetch/i.test(error.message);
  return (
    <>
      <Ticker items={[{ value: "ERROR" }, { label: "STATUS", value: unreachable ? "BACKEND UNREACHABLE" : "FAILED", numeric: true }]} />
      <header className="border-b-2 border-ink">
        <div className="mx-auto max-w-[1280px] px-6 py-4">
          <Link href="/companies" className="text-[24px] leading-none tracking-[-0.04em] text-ink no-underline">
            <span className="font-normal">Say</span>
            <span className="font-extrabold">Do</span>
          </Link>
        </div>
      </header>
      <main id="content" className="mx-auto max-w-[1280px] px-6 pt-10">
        <section className="max-w-2xl">
          <p className="kicker">Something went wrong</p>
          <h1 className="mt-4 text-[40px] leading-[1.05] font-bold tracking-[-0.035em] sm:text-[52px]">
            {unreachable ? "The SayDo backend is not answering." : "This page could not be loaded."}
          </h1>
          <p className="mt-5 text-[20px] leading-[1.45] text-ink-2">
            {unreachable
              ? "Start the API (uv run uvicorn app.main:app in backend/) and try again."
              : "The data behind it could not be read. Trying again usually helps."}
          </p>
          <div className="mt-6 flex flex-wrap items-center gap-6">
            <button type="button" onClick={reset} className="bg-ink px-5 py-2.5 text-[15px] font-semibold text-paper">
              Try again
            </button>
            <Link href="/companies" className="text-[15px]">
              Back to companies &rarr;
            </Link>
          </div>
        </section>
      </main>
      <Footer />
    </>
  );
}
