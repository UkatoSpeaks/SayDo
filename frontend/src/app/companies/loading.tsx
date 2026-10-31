import { Shell } from "@/components/Shell";
import { Bar, LoadingNote, SectionSkeleton, TickerSkeleton } from "@/components/Skeleton";

export default function Loading() {
  return (
    <Shell active="companies" ticker={<TickerSkeleton />}>
      <LoadingNote />
      <div aria-hidden="true" className="flex flex-col gap-3">
        <Bar className="h-3 w-28" />
        <Bar className="h-12 w-72" />
      </div>
      <SectionSkeleton rows={4} />
    </Shell>
  );
}
