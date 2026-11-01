# SayDo

**Track what management promised vs what they delivered.**

SayDo is an AI research tool for Indian listed companies (NSE/BSE). It reads earnings-call
transcripts, results and presentations, pulls out what management promised ("revenue growth of
15-18% in FY27"), and checks those promises against reported numbers in later quarters.

The backend covers companies, PDF upload and page-level text extraction, Indian fiscal-year
helpers, speaker-aware transcript chunking, Gemini embeddings in pgvector, semantic search, LLM
claim extraction, reported actuals (CSV), claim threads that follow one promise across calls, a
verification agent, a Say-Do scorecard and the promise × call ledger. The Next.js frontend in
`frontend/` shows the companies list, scorecard, promise threads, search and method pages.

## Stack

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 (async) · Alembic · PostgreSQL 16 + pgvector ·
PyMuPDF · LangGraph · pytest · uv

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
| POST / GET | `/companies` | Create / list companies. The list adds `say_do_latest {kept, total, ratio}`, `open_count` and `latest_period` (three grouped queries, not one per company) |
| GET | `/companies/{id}` | Get a company |
| POST | `/companies/{id}/documents` | Upload a PDF (multipart: `file`, `doc_type`, `period_label`); parsed synchronously; `409` on duplicate. Transcripts are then chunked and embedded in the background |
| GET | `/companies/{id}/documents` | List documents (`?doc_type=`, `?order=asc\|desc`) sorted by fiscal period |
| GET | `/documents/{id}` | Get a document |
| GET | `/documents/{id}/file` | The stored PDF, served inline (`404 file_not_found` if missing). Link to `…/file#page=N` to open it at a page |
| GET | `/documents/{id}/pages` | Page text (`?start=&end=`, max 50 pages per request) |
| DELETE | `/documents/{id}` | Delete a document, its pages and its stored file |
| POST | `/documents/{id}/process` | Chunk + embed in the background (`202`). Keeps existing chunks and only embeds missing vectors; `?rechunk=true` rebuilds them |
| GET | `/documents/{id}/chunks` | Chunks (`?section=&speaker_role=&limit=&offset=`) |
| POST | `/companies/{id}/search` | Semantic search: `{query, top_k (≤30, default 8), section?, speaker_role?, from_period?, to_period?}`. Each result carries `thread_id` when a claim was extracted from that chunk |
| POST | `/documents/{id}/extract` | Extract claims in the background (`202`, returns the run). Resumes a paused/failed run; `?reextract=true` deletes the claims and starts over; `409` if a job is active, the document isn't embedded, or claims exist already |
| GET | `/documents/{id}/extraction-runs` | Extraction runs, newest first, with chunk/claim/token counts |
| GET | `/companies/{id}/claims` | Claims (`?metric=&claim_type=&period_from=&period_to=&target_fy=&min_confidence=&limit=&offset=`), oldest call first, then by page |
| GET | `/claims/{id}` | A claim with its source chunk text, call period and page |
| POST | `/companies/{id}/actuals/import` | Upload actuals CSV (multipart `file`). All rows validated first; any error → `422` listing every `{row, field, message}` and nothing is imported. Upserts on (company, metric, year, quarter) |
| GET | `/companies/{id}/actuals` | Reported actuals plus derived values (`derived: true`, `derived_from`) (`?metric=&period_from=&period_to=`) |
| POST | `/companies/{id}/link` | Build/refresh claim threads in the background (`202`, returns the run) |
| POST | `/companies/{id}/verify` | Link + verify every thread in the background (`202`, returns the run); `409` if a link/verify run is active |
| GET | `/companies/{id}/verification-runs` | Link/verify runs, newest first: status, as-of period, thread/LLM/embedding/token counts |
| GET | `/companies/{id}/threads` | Threads with their latest verdict (`?status=&metric=&dropped=`) |
| GET | `/threads/{id}` | A thread: every mention (quote, period, change type, page), latest verification, the actual used, evidence and explanation chunks with text |
| GET | `/companies/{id}/scorecard` | Say-Do scorecard, with `say_do_latest` and `say_do_original` (`{kept, total, ratio}`) |
| GET | `/companies/{id}/ledger` | The promise × call matrix: `{periods, rows: [{thread_id, title, verdict, kept_after_lowering, slipped_quarters, comparison, cells: [{period, kind, text}]}]}` |

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
| `actuals` | Reported metric values per fiscal period (entered via CSV) | Prompt 4 |
| `claim_threads` | One promise tracked across calls: title, target period, first/last seen, mentions, `is_dropped`. Claims point at their thread and carry a `change_type` | Prompt 4 |
| `verifications` | A thread's outcome as of a period: status, method (deterministic/llm), gap, near miss, evidence and explanation chunk ids, a snapshot of what was compared. Old rows are kept; one `is_latest` per thread | Prompt 4 |
| `verification_runs` | One link or verify pass over a company: status, counts, LLM calls, tokens | Prompt 4 |

## Processing pipeline

`uploaded → parsed → chunked → embedded` (`failed` with `error_message` on any error).

1. **Clean** (`services/transcript_cleaner.py`): lines that repeat in the top/bottom 5 lines of at
   least half the pages (min. 3; digits ignored, so "Page 3 of 12" matches "Page 4 of 12") are
   peeled off each page's edges, plus standalone page numbers. Body text and line breaks stay.
   Lines ending in `:` or starting with "Moderator" are never treated as headers.
2. **Parse speakers** (`services/transcript_parser.py`):
   - Participants: a `Management:` / `Company Participants:` / `Corporate Participants` /
     `Conference Call Participants` block on pages 1–2, split on honorifics (Mr./Ms./Dr./Shri/…)
     or, without them, on lines with a dash. Names may wrap lines. Bare lines alternating name /
     job title (Infosys style) are paired line by line. An `Analysts` block ends the management
     list and seeds the analyst names (`Name – Firm`, or alternating name / firm lines).
   - A speaker label is either `Name:` at the start of a line (at most 6 words, no digits,
     starting with a capital; it counts only if it is Moderator/Operator, a listed participant,
     an analyst the moderator introduced, or a label that appears more than once, so "Key
     highlights for the quarter: …" stays text), or a name alone on its line with the speech on
     the next lines (Infosys style; it counts only if it is Moderator/Operator or a known
     participant/analyst, and never inside the participant lists).
   - A transcript with no management speaker is marked `failed` ("No management speakers
     recognised…"), never `extracted` with zero claims; `scripts/ingest_company.py` warns and
     carries on with the other calls.
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
   between requests, retries on 429/5xx. A per-minute quota 429 waits the time Gemini names
   (or a minute) and retries without using up an attempt; a daily-quota 429 stops at once. The embedded text is prefixed with
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
   capex, H1/H2, …; `EXTRACTION_PREFILTER`) skips chunks with nothing forward-looking. The strict
   pre-filter (`EXTRACTION_STRICT_PREFILTER`) first drops hand-overs and sign-offs ("I will now
   hand over to our CFO", "look forward to speaking with you next quarter"), and skips chunks
   under 40 tokens unless they state an outlook with a number or a period.
2. **Extract** (`services/llm.py`, `services/extraction_prompt.py`): Groq
   `openai/gpt-oss-120b` with strict JSON-schema output. `extract-v4` (default) packs consecutive
   chunks into one request — up to `EXTRACTION_BATCH_MAX_TOKENS=2500` of chunk text, 6 chunks,
   and `EXTRACTION_BATCH_MAX_OUTPUT_TOKENS=1500` of expected reply (from the forward-looking
   sentences in each chunk, so claim-dense chunks go alone; past ~10 claims per reply the model
   starts skipping some). Chunks are tagged `[C1]`, `[C2]`…, analyst questions `Q1`, `Q2`… (sent
   once per request), and each claim names its `chunk_id`. The system prompt is a constant
   (call details are in the user message) with 4 few-shot examples and asks for single-line JSON.
   A claim must be about a period after the reported quarter: past-tense and "this quarter we
   grew…" statements (sequential growth, deal mix, share of revenue, a dividend increase) are
   never claims. Each claim has a `scope`: `company` (default), `services` (guidance for the
   services business apart from the total) or `segment:<name>`.
   Invalid output gets one repair retry; a batch still invalid is retried one chunk at a time,
   and one cut off at `LLM_MAX_OUTPUT_TOKENS` is split in half (no repair). `extract-v1` (one
   chunk per request, 5 examples) is kept in `services/extraction_prompt_v1.py`; set
   `EXTRACTION_PROMPT_VERSION=extract-v1` to use it.
3. **Validate** (`services/claim_validation.py`, `services/timeframe.py`): the quote must be in
   the management chunk the claim names (normalised, fuzzy ≥ 0.9, no changed numbers), else the
   claim is rejected — also when it is elsewhere in the batch. Targets must appear in the source, else they're nulled and the claim downgraded.
   Timeframes are resolved by rules ("next year" on a Q2FY26 call = FY27; "next two years" =
   FY28, flagged as a range), and the model's guess is kept only when consistent with what was
   said. Near-duplicates in a document merge into the highest-confidence one (`mention_count`).
   Metric and type corrections (`services/claim_rules.py`): a claim that names EBIT is
   `ebit_margin`/`ebit`, never EBITDA; a basis-point figure described as an impact, headwind,
   charge, one-off or contribution is claim type `impact` (the size of an effect, not a target).
   Before linking, the same rules run on a company's stored claims, plus one company-level rule:
   when a company states its margins as EBIT and never as EBITDA, a margin claim filed as
   `ebitda_margin` that names neither is `ebit_margin`. A revenue sub-line is never total
   revenue: "product revenue" / "software revenue" get scope `segment:<name>`, "third-party" or
   "segment revenue" become metric `other`, "services revenue growth" gets scope `services`.
   **Past results are dropped** (and each one logged): a claim whose resolved period is the
   call's own quarter or earlier, unless its quote is forward-looking ("will", "expect",
   "guidance", "by FY27"…); also a number with no period at all stated in the past tense.
   Explicit dates resolve by the Indian FY ("March 31, 2027" → Q4FY27; Apr–Dec of year Y is
   FY(Y+1), Jan–Mar of Y is FY(Y)).
4. **Store**: commits after every request and marks its chunks `claims_extracted_at` only once
   it succeeded, so a run paused by the daily quota (`paused_quota`) or a crash resumes at the
   first unhandled chunk.

Rate limits (Groq free tier, per organisation): a shared limiter keeps a rolling 60 s budget of
`LLM_TPM_LIMIT=8000` tokens and `LLM_RPM_LIMIT=30` requests. 429s are retried (retry-after,
exponential backoff, 5 attempts); a daily-quota 429 pauses the run instead. Groq reserves prompt +
`max_completion_tokens` against the minute's budget when a request starts and refunds unused
output and cached prompt tokens afterwards, so expect some 429 waits (they cost no tokens).

Token cost (measured live on the synthetic call; `scripts/extraction_tokens.py`): `extract-v1`
≈ 24K tokens for 7 chunks / 20 claims, `extract-v2` ≈ 12K with the same 20 claims. Projected
for a typical transcript (40 chunks × 450 tokens, ~40 claims): ≈ 136K → ≈ 45K tokens, i.e. ~1.5
→ ~4.5 transcripts a day on the 200K free tier. Groq caches prompt prefixes automatically
(`usage.prompt_tokens_details.cached_tokens`, logged per request), but hits were rare in tests
(about 1 request in 4–6).

```bash
cd backend
uv run python -m scripts.extraction_tokens          # offline: prompt-side token split
uv run python -m scripts.extraction_tokens --live   # Groq: v1 vs v2 tokens and claims (~35K tokens)
```

Check a real transcript end to end (dev database, real APIs; `--fake` for no API calls):

```bash
cd backend
uv run python -m scripts.check_transcript samples/SYMBOL_Q2FY26.pdf --name "Company Ltd"
```

`backend/samples/` is git-ignored. Tests use `FakeLLMProvider`; `uv run pytest -m live` also
runs one real Groq extraction.

## Actuals (reported numbers)

Actuals are typed in from results filings and uploaded as CSV (`POST
/companies/{id}/actuals/import`); nothing is extracted by an LLM. Start from
[`backend/templates/actuals_template.csv`](backend/templates/actuals_template.csv):

| Column | How to fill it |
| --- | --- |
| `nse_symbol` | The company's NSE symbol; must match the company you upload to |
| `metric_normalized` | One of the claim metrics: `revenue`, `ebitda`, `ebitda_margin`, `ebit`, `ebit_margin` (EBIT is never EBITDA), `pat`, `capex`, `net_debt`, `capacity_utilization`, … |
| `fiscal_year` | Indian fiscal year it ends in: `2026` or `FY26` (= Apr 2025 – Mar 2026) |
| `fiscal_quarter` | `1`–`4` (or `Q1`–`Q4`); **blank for the full year** |
| `value` | The number as reported (`1,150.5` is fine) |
| `unit` | `inr_crore`, `inr_lakh`, `inr_bn`, `usd_mn`, `percent`, `bps`, `tonnes`, `mw`, `units`, `count`, `x`. Margins/growth must be `percent`/`bps`; money metrics an INR/USD unit |
| `source_note` | Where it came from ("Q2 FY26 results, consolidated P&L") |
| `source_page` | Page number in that filing (optional) |
| `scope` | Optional column: blank or `company` (default), `services`, or `segment:<name>`. A claim is only checked against actuals of its own scope (HCLTech's services growth guidance needs a `services` row) |

Tips: enter the four quarters of revenue, EBITDA and PAT and let SayDo derive the year; enter
figures as reported (lakh stays lakh, it is converted when compared); re-uploading a row updates
it.

Derived values (`services/derived_metrics.py`) are computed on read and never stored: annual
revenue/EBITDA/EBIT/PAT/capex = sum of all four quarters (only if all four exist); margins =
numerator / denominator from the (summed) parts, never an average; capacity, debt, net debt,
order book, store count for the year = the Q4 value; revenue growth = year-on-year from revenue.
Each carries a `derived_from` sentence. Unit conversion (`services/units.py`): 100 lakh = 1
crore, 1 bn = 100 crore, 100 bps = 1%. Values that can't be converted (USD vs INR) are never
compared; the promise becomes unverifiable with the reason.

## Claim threads

A thread is one promise followed across calls (`services/linking.py`, run by `POST
/companies/{id}/link` or as the first step of verify):

1. **Deterministic first.** Same metric + same scope + same resolved target period + compatible
   unit → same thread (claims with different scopes are never linked, by any rule or the LLM) ("FY27 EBITDA margin 19–20%" on Q1, Q2, Q3). A target in bps (a change) never shares a
   thread with a level in percent, and a money metric stated in another unit gets its own thread,
   so a later claim can never replace a range target with a figure in another unit. `impact`
   claims are never linked. For "by <period>" metrics (debt, capacity, store count,
   order book, cost reduction) an identical target restated for a later period while the earlier
   one was still open — after which the earlier one is no longer mentioned — is the same promise,
   delayed.
2. **Ambiguous claims** (qualitative, metric `other`, plant commissioning, product launch) link
   when they share a name ("Line 4", "Aura") or, for qualitative claims on a standard metric, the
   same period. The rest, when an earlier same-metric thread exists, go to Groq in batches
   (`VERIFY_LINK_BATCH_SIZE`, default 15 per call) within the run's LLM budget — e.g. "Line 4" vs
   "the Pune expansion". Without budget they stay separate threads.
3. Every mention gets a `change_type` vs the previous one: `new`, `reiterated`, `raised`,
   `lowered`, `narrowed`, `widened`, `delayed` (target period moved later), `pulled_forward`.
4. **Dropped**: a thread whose target period has not ended as of the company's latest data and
   that is absent from its last `VERIFY_DROPPED_AFTER_CALLS` (default 2) calls. Threads without a
   target period are never dropped (they're unverifiable). `last_seen_period` records where it
   was last said.

Thread ids are stable across re-runs (a rebuilt group reuses the thread most of its claims
pointed at).

## Verification

`POST /companies/{id}/verify` links, then runs a LangGraph graph per thread
(`services/verification.py`):

```
load_thread ─┬─ dropped ─────────────────────────────── mark_dropped ─┐
             ├─ numeric / directional with a period ── deterministic_check ─┤
             ├─ qualitative / event claims ── gather_evidence ── llm_judge ─┼─ find_explanation* ─ persist
             └─ no resolvable target period ────────── unverifiable ───────┘
                                                  * only for missed, delayed or dropped threads
```

- **As of** = the company's latest extracted call or entered actual (not today's date), so the
  same data always gives the same result. A quarter's results count only if an actual exists.
- **deterministic_check** (`services/deterministic_check.py`, no LLM, template explanations):
  period not over → `in_progress`; over but no actual (none derivable) → `unverifiable`
  ("actual not yet entered"). Numeric: the **latest** target in the thread is used and the
  original stays visible ("Delivered (target lowered from ₹450 crore to ₹400 crore): …"). Inside
  a range → delivered; beyond the good end (above for revenue/margins, below for debt) →
  delivered (beat); otherwise missed with `computed_gap` (claim unit) and `gap_pct`. "At least /
  up to" targets respect their side; a single figure is a floor or ceiling by the claim's
  direction or the metric's polarity (capex needs the exact range). A miss within
  `VERIFY_TOLERANCE_PCT` (default 0) stays missed with `near_miss = true`. Unit mismatch: a
  target in bps is never compared with an actual in percent (or the reverse), and a miss by
  more than 200% of the target is not believable; both are `unverifiable` with "unit
  mismatch", never missed (`verify-v2`). Directional: target-
  period actual vs a baseline (same period last year for growth metrics, else the period the
  claim was made in at the target's granularity), ±`VERIFY_DIRECTIONAL_BAND_PCT` (default 2%)
  counts as no change.
- **gather_evidence → llm_judge** (qualitative and event claims): vector search over management
  chunks from calls after the promise was first made (top 5). No later commentary → decided in
  code without an LLM call. Otherwise one Groq call returns status, ≤ 2 sentences of reasoning,
  evidence ids and a verbatim quote, which must be found in the cited chunk (same strict
  matching as extraction, numbers unchanged). A failed check gets one retry; a second failure →
  unverifiable. The judge (`judge-v2`) also gets the original quote and the thread's key terms
  (names, amounts) and answers unverifiable when the evidence may be about a different deal,
  project or product.
- **find_explanation**: top 2 later management chunks for "<metric> <period> reason impact
  delay", stored as explanation evidence. `VERIFY_SUMMARIZE_EXPLANATIONS=true` adds a one-call
  summary (quote-checked) within the budget.
- **persist**: a new `verifications` row; the thread's previous row loses `is_latest`.

## Say-Do scorecard

`GET /companies/{id}/scorecard` (`services/scorecard.py`, pure code over the latest
verifications): counts per status; **Say-Do ratio = delivered / (delivered + missed + dropped)**
with the denominator shown and `low_sample` below 5 (so 2/2 = 100% isn't read as a strong
record); the same per metric; guidance behaviour (how often targets were raised, lowered,
delayed); the 5 biggest misses by |gap_pct|; every dropped promise. Statements with no
resolvable target period ("medium term", "over the next few years") stay stored and searchable
but are left out of every count and listed under `long_term` (a collapsed "Long-term statements
(not scored)" section on the scorecard page); the ledger leaves them out too unless
`?include_long_term=true`.

**Against original targets** (`say_do_original`): over the same decided promises, a delivered
one counts only if the actual also meets the *first* target in its thread and its deadline never
moved later than the first one stated (`deterministic_check.check_against_original`, pure code
over the stored verdict and mentions). A promise whose latest target was met but not its original
one keeps status `delivered` and gets the display flag `kept_after_lowering` (on thread
verifications, scorecard items and ledger rows). The demo: 6/8 against latest targets, 5/8 against
original ones (PAT lowered from ₹450 to ₹400 crore, then ₹410 crore reported).

## Ledger

`GET /companies/{id}/ledger` (`services/ledger.py`, pure code): columns run quarter by quarter
from the first call to the latest target period any mention refers to. Each cell, by priority:
`kept`/`missed` in the quarter whose actual decided the verdict (or, for promises judged from
commentary, the call with the evidence), with the actual as text; `said`/`raised`/`lowered`/`moved`
on a call that mentioned it (from the change type, target as text: `450`, `18–19%`, `→ Q3FY26`);
`open` at the current deadline of a promise in progress; `silent` on a later call that skipped it
while it was undecided; else `none`. Percentages show one decimal, other numbers use Indian
grouping. Rows: open, missed, dropped, unverifiable, not verified, then kept. Numeric rows with
an actual carry a `comparison` (target, original target, actual) for the target-vs-actual chart.

## Add a real company

`scripts/ingest_company.py` runs the whole pipeline for one company from a folder of
transcripts, against the dev database and the real APIs (it refuses to run with
`EMBEDDING_PROVIDER=fake` or `LLM_PROVIDER=fake`; `--allow-fake` is for tests only).

1. Download the earnings-call transcripts from the company's investor-relations site, BSE or
   NSE, and save them as `backend/samples/<SYMBOL>/<SYMBOL>_Q<n>FY<yy>.pdf` (e.g.
   `INFY_Q3FY26.pdf`). `backend/samples/` is git-ignored. Files with other names, non-PDFs
   and files over `MAX_UPLOAD_MB` are skipped and listed; subfolders (e.g. `results/`) are
   ignored.
2. Run:

   ```bash
   cd backend
   uv run python -m scripts.ingest_company --symbol INFY --name "Infosys Ltd" \
       --sector "IT services" --dir samples/INFY [--actuals samples/INFY/actuals.csv]
   ```

   It creates the company (matched on NSE symbol), uploads each file as a transcript (same
   file twice → reused by SHA-256), then oldest quarter first chunks + embeds and extracts.
   Before a document's first extraction it prints the `check_transcript` checks (speakers
   recognised, Q&A start, leftover headers). When Groq's daily quota runs out it prints the
   progress and `Paused: Groq daily limit. Run the same command again tomorrow.` (exit code
   3); the same command continues from the first chunk not yet handled.
3. When every transcript is extracted it writes `samples/<SYMBOL>/actuals_todo.csv`: the actuals
   template with an empty `value` column, one row per metric/unit/period needed to verify a
   numeric or directional promise whose target period has ended (plus the baseline period for
   directional ones), with the promises it is needed for in `source_note`.
4. Copy it to `samples/<SYMBOL>/actuals.csv` and fill in values from the official results
   (source file and page in `source_note`/`source_page`, unit as listed; for growth guidance
   use the figure the guidance referred to, e.g. constant currency). Rows left empty are
   skipped. Run the same command again: it imports the CSV, runs link + verify and prints the
   scorecard, plus any needed actuals still missing. The todo has a `scope` column: a
   `services` or `segment:<name>` row needs that part of the company's figure, not the total.
5. After a prompt change, add `--reextract`: every transcript whose last extraction used an
   older prompt version is extracted again (its claims are replaced, chunks and embeddings are
   kept). A re-extraction paused by the daily quota resumes on the next run.

Exit codes: 0 done, 1 a step failed (re-run to retry), 2 bad arguments or config, 3 paused for
the Groq daily limit. Every run prints the Groq tokens it used.

## Demo data

`scripts/seed_demo.py` seeds a **fictional** company, Demo Industries Ltd (`DEMOIND`): four calls
(Q1–Q4 FY26) of chunks with fake 768-dim embeddings, 21 claims and 17 actuals covering a
delivered margin, a missed revenue target with a later explanation, a plant delayed twice (and
renamed), a raised guidance, a lowered-then-met target, a dropped promise, a directional claim, a
qualitative launch and an in-progress FY27 target. It writes a small transcript PDF per call
under `uploads/demo/` (so transcript links open), then runs link + verify with
`FakeLLMProvider`, so no API is called. Idempotent; `--reset` starts over.

```bash
cd backend
uv run python -m scripts.seed_demo            # seed/refresh and print the scorecard
uv run python -m scripts.seed_demo --reset
```

## Runs on free tiers

- Groq and Gemini are only called by offline pipeline steps that you trigger and that store
  their results in Postgres: process (embeddings), extract, link, verify.
- Every `GET` endpoint reads Postgres only; a test runs all of them with providers that raise
  if called. The one exception is `POST /companies/{id}/search`, which embeds the query.
- Deterministic code comes first (linking, numeric/directional verdicts, derived metrics,
  explanations, scorecard). The LLM is used only where text needs judgment — ambiguous thread
  links and qualitative/event verdicts — and is capped per run by `VERIFY_MAX_LLM_CALLS`
  (default 30; one call = one request plus at most one schema-repair retry). Once spent,
  remaining LLM-route threads are marked unverifiable ("llm budget exhausted") and the next run
  can pick them up. Embedding calls (Gemini query embeddings for evidence and explanations) are
  counted per run in `verification_runs.embedding_calls`.

Verification settings (`backend/.env`): `VERIFY_MODEL` (default `openai/gpt-oss-120b`),
`VERIFY_MAX_LLM_CALLS=30`, `VERIFY_TOLERANCE_PCT=0`, `VERIFY_DIRECTIONAL_BAND_PCT=2`,
`VERIFY_DROPPED_AFTER_CALLS=2`, `VERIFY_LINK_BATCH_SIZE=15`, `VERIFY_SUMMARIZE_EXPLANATIONS=false`.

## Frontend

Next.js 16 (App Router, TypeScript), Tailwind CSS 4, in `frontend/`. Server components fetch the
API (`NEXT_PUBLIC_API_URL`, default `http://localhost:8000`) with `no-store`; only the company
finder, search form and "report a wrong link" button run in the browser. Reading a page never
triggers Groq or Gemini; search is the one exception (it embeds the query). Design tokens live
in `src/app/globals.css` (`@theme`: Tailwind 4 reads its config from CSS, so they are both
utilities and CSS variables). API types are generated from the backend's OpenAPI schema.
The home page's hero, ticker and ledger preview feature the real company with the most decided
promises (the demo when no real company has a verdict, or when the real one has no hero example
of its own); company lists show real companies first and the demo last, labelled "Demo data".

```bash
cd frontend
cp .env.example .env.local        # optional; defaults to http://localhost:8000
npm install
npm run gen:api                   # regenerate src/lib/api-types.ts (backend running)
npm run dev                       # http://localhost:3000

npm run lint && npm run typecheck && npm test && npm run build
```

End-to-end (installed Google Chrome; no browser download). With the backend running and the
demo seeded, build and start the frontend, then:

```bash
npm run test:e2e                  # smoke: companies → scorecard → thread → search, no console errors
E2E_RUN_SEARCH=1 npm run screenshots   # 4 pages at 1440 and 390 px into screenshots/ (git-ignored)
```

`E2E_RUN_SEARCH=1` runs one search for the search-page shot; use it only with a backend started
with `EMBEDDING_PROVIDER=fake LLM_PROVIDER=fake` (the demo's embeddings are fake anyway), or the
query is sent to Gemini.

## Layout

```
backend/
  app/
    api/routes/   health, companies, documents, claims, verification
    core/         config, db, logging, errors
    models/       SQLAlchemy models
    schemas/      Pydantic request/response models
    services/     fiscal, storage, pdf_parser, documents, transcript_cleaner,
                  transcript_parser, chunker, tokens, embeddings, ingestion, search,
                  llm, claim_schema, extraction_prompt, claim_validation, timeframe,
                  extraction, units, metrics, periods, derived_metrics, actuals_import,
                  linking, llm_budget, verification_prompts, deterministic_check,
                  verification, scorecard, ledger, threads
  alembic/        migrations
  scripts/        check_transcript (manual end-to-end check), extraction_tokens,
                  seed_demo (fictional demo company), ingest_company (add a real company)
  templates/      actuals_template.csv
  samples/        real transcripts for manual checks (git-ignored)
  tests/
frontend/
  src/app/        companies, companies/[id] (scorecard), threads/[id], search, method
  src/components/ ticker, masthead, ledger table, bullet bar, deadline grid, search client
  src/lib/        API client + generated types, formatting, narrative sentences, bullet geometry,
                  sentence highlighting
  e2e/            Playwright smoke test and screenshots
docker-compose.yml
```
