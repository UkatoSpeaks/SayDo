import { Shell } from "@/components/Shell";
import { HeroSkeleton, LoadingNote, SectionSkeleton, TickerSkeleton } from "@/components/Skeleton";

export default function Loading() {
  return (
    <Shell active="companies" ticker={<TickerSkeleton />}>
      <LoadingNote />
      <HeroSkeleton />
      <SectionSkeleton rows={6} />
    </Shell>
  );
}
