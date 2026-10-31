import { transcriptUrl } from "@/lib/api";

export function TranscriptLink({
  documentId,
  page,
  children,
  className = "",
}: {
  documentId: string;
  page?: number | null;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <a
      href={transcriptUrl(documentId, page)}
      target="_blank"
      rel="noopener noreferrer"
      className={className}
    >
      {children ?? (page ? `Transcript, p. ${page} →` : "Transcript →")}
      <span className="sr-only"> (PDF, opens in a new tab)</span>
    </a>
  );
}
