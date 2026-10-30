# SayDo

**Track what management promised vs what they delivered.**

SayDo is an AI research tool for Indian listed companies (NSE/BSE). It reads earnings-call
transcripts, results and presentations, pulls out what management promised ("revenue growth of
15-18% in FY27"), and checks those promises against reported numbers in later quarters.

This repo currently contains the backend: companies, PDF upload and page-level text extraction,
Indian fiscal-year helpers, speaker-aware transcript chunking, Gemini embeddings in pgvector and a
semantic search endpoint. LLM claim extraction and the Next.js frontend come later.

## Stack

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 (async) · Alembic · PostgreSQL 16 + pgvector ·
PyMuPDF · pytest · uv

## Getting started

Prerequisites: [uv](https://docs.astral.sh/uv/) and Docker (with Compose).

```bash
# 1. Configure Postgres: copy .env.example to .env and set a password
cp .env.example .env              # then edit POSTGRES_PASSWORD

# 2. Start Postgres (pgvector/pgvector:pg16, bound to 127.0.0.1 only).
#    Also creates the <POSTGRES_DB>_test database.
docker compose up -d db

# 3. Configure the backend: copy .env.example to .env and put the same
#    password in DATABASE_URL and TEST_DATABASE_URL
cd backend
cp .env.example .env
uv sync

# 4. Run migrations
uv run alembic upgrade head

# 5. Run the API (docs at http://localhost:8000/docs)
uv run uvicorn app.main:app --reload

# 6. Run the tests (uses TEST_DATABASE_URL; the schema is rebuilt automatically)
uv run pytest
```

Both `.env` files are git-ignored; only the `.env.example` templates are committed. Docker Compose
refuses to start if `POSTGRES_PASSWORD` is missing.

Port 5432 already in use? Set `POSTGRES_PORT=5433` in the root `.env` and update the URLs in
`backend/.env`.

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | App status and database connectivity |
| POST / GET | `/companies` | Create / list companies |
| GET | `/companies/{id}` | Get a company |
| POST | `/companies/{id}/documents` | Upload a PDF (multipart: `file`, `doc_type`, `period_label`); parsed synchronously; `409` on duplicate. Transcripts are then chunked and embedded in the background |
| GET | `/companies/{id}/documents` | List documents (`?doc_type=`, `?order=asc\|desc`) sorted by fiscal period |
| GET | `/documents/{id}` | Get a document |
| GET | `/documents/{id}/pages` | Page text (`?start=&end=`, max 50 pages per request) |
| DELETE | `/documents/{id}` | Delete a document, its pages and its stored file |
| POST | `/documents/{id}/process` | Chunk + embed in the background (`202`). Keeps existing chunks and only embeds missing vectors; `?rechunk=true` rebuilds them |
| GET | `/documents/{id}/chunks` | Chunks (`?section=&speaker_role=&limit=&offset=`) |
| POST | `/companies/{id}/search` | Semantic search: `{query, top_k (≤30, default 8), section?, speaker_role?, from_period?, to_period?}` |
| POST | `/documents/{id}/extract` | Extract claims in the background (`202`, returns the run). Resumes a paused/failed run; `?reextract=true` deletes the claims and starts over; `409` if a job is active, the document isn't embedded, or claims exist already |
| GET | `/documents/{id}/extraction-runs` | Extraction runs, newest first, with chunk/claim/token counts |
| GET | `/companies/{id}/claims` | Claims (`?metric=&claim_type=&period_from=&period_to=&target_fy=&min_confidence=&limit=&offset=`), oldest call first, then by page |
| GET | `/claims/{id}` | A claim with its source chunk text, call period and page |

Errors share one shape: `{"error": {"code": "...", "message": "..."}}`.

Period labels follow the Indian fiscal year (April–March, named after the year it ends; FY27 =
1 Apr 2026 – 31 Mar 2027). Accepted forms include `Q2FY26`, `Q2 FY26`, `q2fy2026`, `FY26` and
`FY2025-26`.

## Schema

| Table | Purpose | Filled in |
| --- | --- | --- |
| `companies` | Listed company with NSE symbol / BSE code | Prompt 1 |
| `documents` | Uploaded PDF: type, fiscal period, SHA-256 (unique), processing status | Prompt 1 |
| `document_pages` | Extracted text per page | Prompt 1 |
| `chunks` | Speaker-aware text chunks with `vector(768)` embeddings (HNSW, cosine) | Prompt 2 |
| `claims` | Promises extracted from management commentary, with model, prompt version, validation notes and `mention_count` | Prompt 3 |
| `extraction_runs` | One extraction pass over a document: status, counters, tokens | Prompt 3 |
| `actuals` | Reported metric values per fiscal period | Later |
| `verifications` | Claim vs actual outcome (delivered, missed, …) | Later |

## Processing pipeline

`uploaded → parsed → chunked → embedded` (`failed` with `error_message` on any error).

1. **Clean** (`services/transcript_cleaner.py`): lines that repeat in the top/bottom 5 lines of at
   least half the pages (min. 3; digits ignored, so "Page 3 of 12" matches "Page 4 of 12") are
   peeled off each page's edges, plus standalone page numbers. Body text and line breaks stay.
   Lines ending in `:` or starting with "Moderator" are never treated as headers.
2. **Parse speakers** (`services/transcript_parser.py`):
   - Participants: a `Management:` / `Company Participants:` block on pages 1–2, split on
     honorifics (Mr./Ms./Dr./Shri/…) or, without them, on lines with a dash. Names may wrap lines.
   - A speaker label is `Name:` at the start of a line, at most 6 words, no digits, starting with a
     capital. It counts only if it is Moderator/Operator, a listed participant, an analyst the
     moderator introduced, or a label that appears more than once. So "Key highlights for the
     quarter: …" stays text.
   - Roles: moderator → management (fuzzy match: honorifics/case ignored, initials, ≥0.85
     similarity) → analyst ("the next question is from the line of X from Y") → unknown.
   - Section: `prepared_remarks` until a moderator turn says e.g. "now begin the
     question-and-answer session" or "the first question is from"; `qna` after that (an analyst
     speaking also switches it). Text before the first speaker becomes an `other` chunk.
3. **Chunk** (`services/chunker.py`): one chunk per turn up to 500 tokens (tiktoken
   `cl100k_base`); longer turns split at sentence/paragraph boundaries into balanced ≤500-token
   pieces with ~50 tokens of overlap. Moderator turns under 20 tokens are dropped. Other document
   types use the same splitter over all pages (speaker null, section `other`).
4. **Embed** (`services/embeddings.py`): `gemini-embedding-001`, 768 dims, `RETRIEVAL_DOCUMENT` /
   `RETRIEVAL_QUERY`, L2-normalised, 50 texts per request, `EMBEDDING_MIN_REQUEST_INTERVAL_S`
   between requests, retries on 429/5xx. The embedded text is prefixed with
   `[company | period | speaker (role) | section]`; the stored text is not. Batches are committed
   one at a time, so a failed run resumes where it stopped.

Embedding settings live in `backend/.env`: `GEMINI_API_KEY`, `EMBEDDING_PROVIDER=gemini|fake`,
`EMBEDDING_DIM=768` (must match the column), `EMBEDDING_MIN_REQUEST_INTERVAL_S=1.0`. Tests use the
offline fake provider; `uv run pytest -m live` runs one test against the real Gemini API.

tiktoken downloads its encoding once (~1.7 MB) into `backend/.cache/tiktoken`.

## Claim extraction

`embedded → extracted`, started by `POST /documents/{id}/extract` (not automatic: set
`AUTO_EXTRACT=true` to extract right after processing).

1. **Select** (`services/extraction.py`): only `management` chunks. A Q&A answer gets the analyst
   question before it as context only. A keyword pre-filter (expect, guidance, target, will,
   capex, H1/H2, …; `EXTRACTION_PREFILTER`) skips chunks with nothing forward-looking.
2. **Extract** (`services/llm.py`, `services/extraction_prompt.py`): Groq
   `openai/gpt-oss-120b` with strict JSON-schema output, one call per chunk. The prompt
   (`PROMPT_VERSION = "extract-v1"`) gives the call period and the fiscal rules, defines what
   is and isn't a claim, and has 5 few-shot examples. Invalid output gets one repair retry that
   shows the model the validation error.
3. **Validate** (`services/claim_validation.py`, `services/timeframe.py`): the quote must be in
   the management chunk (normalised, fuzzy ≥ 0.9, no changed numbers), else the claim is
   rejected. Targets must appear in the source, else they're nulled and the claim downgraded.
   Timeframes are resolved by rules ("next year" on a Q2FY26 call = FY27; "next two years" =
   FY28, flagged as a range), and the model's guess is kept only when consistent with what was
   said. Near-duplicates in a document merge into the highest-confidence one (`mention_count`).
4. **Store**: commits after every chunk and marks it `claims_extracted_at`, so a run paused by
   the daily quota (`paused_quota`) or a crash resumes at the next chunk.

Rate limits (Groq free tier, per organisation): a shared limiter keeps a rolling 60 s budget of
`LLM_TPM_LIMIT=8000` tokens and `LLM_RPM_LIMIT=30` requests. 429s are retried (retry-after,
exponential backoff, 5 attempts); a daily-quota 429 pauses the run instead. Each chunk costs
about 3,000 tokens, mostly the system prompt, so expect roughly 2 chunks a minute.

Check a real transcript end to end (dev database, real APIs; `--fake` for no API calls):

```bash
cd backend
uv run python -m scripts.check_transcript samples/SYMBOL_Q2FY26.pdf --name "Company Ltd"
```

`backend/samples/` is git-ignored. Tests use `FakeLLMProvider`; `uv run pytest -m live` also
runs one real Groq extraction.

## Layout

```
backend/
  app/
    api/routes/   health, companies, documents, claims
    core/         config, db, logging, errors
    models/       SQLAlchemy models
    schemas/      Pydantic request/response models
    services/     fiscal, storage, pdf_parser, documents, transcript_cleaner,
                  transcript_parser, chunker, tokens, embeddings, ingestion, search,
                  llm, claim_schema, extraction_prompt, claim_validation, timeframe,
                  extraction
  alembic/        migrations
  scripts/        check_transcript (manual end-to-end check)
  samples/        real transcripts for manual checks (git-ignored)
  tests/
docker-compose.yml
```
