import type { components } from "./api-types";

type Schemas = components["schemas"];
export type CompanyListItem = Schemas["CompanyListItem"];
export type Company = Schemas["CompanyRead"];
export type Scorecard = Schemas["ScorecardRead"];
export type ScorecardThread = Schemas["ScorecardThread"];
export type Ledger = Schemas["LedgerRead"];
export type LedgerRow = Schemas["LedgerRow"];
export type LedgerCell = Schemas["LedgerCell"];
export type LedgerCellKind = LedgerCell["kind"];
export type LedgerComparison = Schemas["LedgerComparison"];
export type ThreadDetail = Schemas["ThreadDetail"];
export type ThreadMention = Schemas["ThreadMention"];
export type ChunkExcerpt = Schemas["ChunkExcerpt"];
export type DocumentRead = Schemas["DocumentRead"];
export type SearchRequest = Schemas["SearchRequest"];
export type SearchResult = Schemas["SearchResult"];
export type SearchResponse = Schemas["SearchResponse"];
export type VerificationStatus = Schemas["VerificationStatus"];
export type ChangeType = Schemas["ChangeType"];
export type ChunkSection = Schemas["ChunkSection"];

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(
  /\/$/,
  "",
);

/** The demo company seeded by backend/scripts/seed_demo.py. */
export const DEMO_SYMBOL = "DEMOIND";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function errorFrom(response: Response): Promise<ApiError> {
  let code = "http_error";
  let message = `${response.status} ${response.statusText}`;
  try {
    const body = (await response.json()) as { error?: { code?: string; message?: string } };
    if (body.error?.code) code = body.error.code;
    if (body.error?.message) message = body.error.message;
  } catch {
    // Not JSON: keep the status line.
  }
  return new ApiError(response.status, code, message);
}

/** GET from the backend. Read endpoints never call an LLM or embedding API. */
export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    cache: "no-store",
    headers: { accept: "application/json" },
  });
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as T;
}

export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** The stored PDF, opened at a page by the browser's viewer. */
export function transcriptUrl(documentId: string, page?: number | null): string {
  const url = `${API_URL}/documents/${documentId}/file`;
  return page ? `${url}#page=${page}` : url;
}

export const api = {
  companies: () => apiGet<CompanyListItem[]>("/companies"),
  company: (id: string) => apiGet<Company>(`/companies/${id}`),
  scorecard: (id: string) => apiGet<Scorecard>(`/companies/${id}/scorecard`),
  ledger: (id: string) => apiGet<Ledger>(`/companies/${id}/ledger`),
  transcripts: (id: string) =>
    apiGet<DocumentRead[]>(`/companies/${id}/documents?doc_type=transcript&order=asc`),
  thread: (id: string) => apiGet<ThreadDetail>(`/threads/${id}`),
};
