import type { Metadata } from "next";
import { SectionHeader } from "@/components/SectionHeader";
import { Shell } from "@/components/Shell";
import { Ticker } from "@/components/Ticker";

export const metadata: Metadata = { title: "Method" };

const STEPS = [
  {
    index: "01",
    title: "Extract",
    body: [
      "Earnings-call transcripts are split by speaker. Only management’s words are read for promises: a target (“EBITDA margin of 18–19% in FY26”), a direction (“utilisation will improve”) or an event (“we will commission Line 4 in Q2”).",
      "A language model proposes each promise with a verbatim quote. The quote must be found in the transcript, with its numbers unchanged, or the promise is thrown away. Timeframes such as “next year” are resolved to fiscal periods by rules, not by the model.",
    ],
  },
  {
    index: "02",
    title: "Link",
    body: [
      "The same promise repeated on later calls becomes one thread. Same metric and same target period link in code; only ambiguous cases, such as a plant that changes name between calls, go to the model.",
      "Each mention is compared with the one before: reiterated, raised, lowered, or deadline moved. A promise that stops being mentioned before its deadline is marked dropped.",
    ],
  },
  {
    index: "03",
    title: "Verify",
    body: [
      "Numbers are compared in code, never by the model: the latest target against the reported actual, after converting lakh, crore and basis points. Each result also counts against the first target that was stated, so a target lowered and then met shows as “kept · lowered”.",
      "Promises without a number, such as a product launch, are judged by the model from later management commentary. Its supporting quote is checked against the transcript; if it cannot be found, the promise stays unverifiable.",
    ],
  },
];

export default function MethodPage() {
  return (
    <Shell
      active="method"
      ticker={
        <Ticker
          items={[
            { value: "METHOD" },
            { label: "STEPS", value: "EXTRACT → LINK → VERIFY", numeric: true },
            { label: "NUMBERS", value: "COMPARED IN CODE", numeric: true },
            { label: "QUOTES", value: "VERIFIED", numeric: true },
          ]}
        />
      }
    >
      <section aria-labelledby="method-title" className="max-w-[920px]">
        <p className="kicker">How SayDo works</p>
        <h1 id="method-title" className="mt-4 text-[40px] leading-[1.05] font-bold tracking-[-0.035em] sm:text-[52px]">
          What was promised, checked against what was reported.
        </h1>
        <p className="mt-5 max-w-[52ch] text-[20px] leading-[1.45] text-ink-2">
          SayDo reads what management says on earnings calls, follows each promise from call to
          call, and checks it against the results the company later reports.
        </p>
      </section>
      {STEPS.map((step) => (
        <section key={step.index} aria-labelledby={`step-${step.index}`} className="max-w-[920px]">
          <SectionHeader index={step.index} id={`step-${step.index}`} title={step.title} />
          {step.body.map((paragraph) => (
            <p key={paragraph.slice(0, 24)} className="mb-4 max-w-[68ch] text-[18px] leading-[1.55] text-ink-2">
              {paragraph}
            </p>
          ))}
        </section>
      ))}
      <section aria-labelledby="ratio-title" className="max-w-[920px]">
        <SectionHeader index="04" id="ratio-title" title="The Say-Do ratio" />
        <p className="mb-4 max-w-[68ch] text-[18px] leading-[1.55] text-ink-2">
          Kept promises divided by every promise that has been decided: kept, missed or dropped.
          Open and unverifiable promises are left out, the denominator is always shown, and fewer
          than five decided promises is flagged as a low sample.
        </p>
        <p className="max-w-[68ch] text-[18px] leading-[1.55] text-ink-2">
          Reading a page never calls a language model. Results are computed once, stored, and
          shown with the transcript page they came from. Only search sends your question to an
          embedding service.
        </p>
      </section>
      <section className="max-w-[920px] border-l-[3px] border-ink pl-[18px]">
        <p className="kicker">Not investment advice</p>
        <p className="mt-2 max-w-[68ch] text-[17px] leading-[1.55] text-ink-2">
          SayDo is an educational research tool. It shows how management&rsquo;s statements
          compare with reported results; it does not recommend buying or selling any security.
          Extraction and linking can be wrong: every claim links to its source so you can check.
        </p>
      </section>
    </Shell>
  );
}
