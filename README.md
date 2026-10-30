# SayDo

**Track what management promised vs what they delivered.**

SayDo is an AI research tool for Indian listed companies (NSE/BSE). It reads earnings-call
transcripts, results and presentations, pulls out what management promised ("revenue growth of
15-18% in FY27"), and checks those promises against reported numbers in later quarters.

This repo currently contains the backend foundation: companies, PDF upload and page-level text
extraction, Indian fiscal-year helpers, and the full database schema. LLM extraction, embeddings
and the Next.js frontend come later.

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
| POST | `/companies/{id}/documents` | Upload a PDF (multipart: `file`, `doc_type`, `period_label`); parsed synchronously; `409` on duplicate |
| GET | `/companies/{id}/documents` | List documents (`?doc_type=`, `?order=asc\|desc`) sorted by fiscal period |
| GET | `/documents/{id}` | Get a document |
| GET | `/documents/{id}/pages` | Page text (`?start=&end=`, max 50 pages per request) |
| DELETE | `/documents/{id}` | Delete a document, its pages and its stored file |

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
| `chunks` | Speaker-aware text chunks with `vector(1024)` embeddings | Prompt 2 |
| `claims` | Promises extracted from management commentary | Prompt 3 |
| `actuals` | Reported metric values per fiscal period | Later |
| `verifications` | Claim vs actual outcome (delivered, missed, …) | Later |

## Layout

```
backend/
  app/
    api/routes/   health, companies, documents
    core/         config, db, logging, errors
    models/       SQLAlchemy models
    schemas/      Pydantic request/response models
    services/     fiscal, storage, pdf_parser, documents
  alembic/        migrations
  tests/
docker-compose.yml
```
