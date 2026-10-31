import { API_URL, ApiError, errorFrom, type SearchRequest, type SearchResponse } from "./api";

export interface SearchFilters {
  managementOnly: boolean;
  section: "all" | "prepared_remarks" | "qna";
  from: string | null;
  to: string | null;
}

export function searchBody(query: string, filters: SearchFilters, topK = 10): SearchRequest {
  const body: SearchRequest = { query, top_k: topK };
  if (filters.managementOnly) body.speaker_role = "management";
  if (filters.section !== "all") body.section = filters.section;
  if (filters.from) body.from_period = filters.from;
  if (filters.to) body.to_period = filters.to;
  return body;
}

/** POST /companies/{id}/search: the one endpoint that embeds text (the query). */
export async function runSearch(
  companyId: string,
  body: SearchRequest,
  signal?: AbortSignal,
): Promise<SearchResponse> {
  const response = await fetch(`${API_URL}/companies/${companyId}/search`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as SearchResponse;
}

/** An error as one plain sentence for the reader. */
export function describeSearchError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429 || /\b429\b|rate.?limit|quota|resource.?exhausted/i.test(error.message)) {
      return "The search service is rate-limited right now. Wait a minute and try again.";
    }
    if (error.status === 502) {
      return "The search service did not answer. Try again in a moment.";
    }
    if (error.status === 422) return `That search could not be run: ${error.message}`;
    if (error.status === 404) return "That company no longer exists.";
    return `Search failed: ${error.message}`;
  }
  return `Can’t reach the SayDo backend at ${API_URL}. Is it running?`;
}
