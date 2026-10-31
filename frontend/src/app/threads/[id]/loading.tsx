import { Shell } from "@/components/Shell";
import { Bar, LoadingNote, SectionSkeleton, TickerSkeleton } from "@/components/Skeleton";

export default function Loading() {
  return (
    <Shell active="companies" ticker={<TickerSkeleton />}>
      <LoadingNote />
      <div aria-hidden="true" className="flex max-w-[920px] flex-col gap-4">
        <Bar className="h-3 w-56" />
        <Bar className="h-12 w-full" />
        <Bar className="h-12 w-3/4" />
        <Bar className="h-5 w-2/3" />
      </div>
      <SectionSkeleton rows={4} />
    </Shell>
  );
}
