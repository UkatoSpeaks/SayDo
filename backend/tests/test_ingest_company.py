"""scripts/ingest_company.py with fake providers: upload, pause on the daily quota,
resume, actuals_todo.csv, then actuals import + verify."""

import csv
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.models import Company, Document, DocumentStatus
from app.services.llm import FakeLLMProvider, LLMQuotaExhausted, Message
from scripts.ingest_company import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_PAUSED,
    EXIT_USAGE,
    PAUSED_MESSAGE,
    Options,
    Pipeline,
    find_transcripts,
    run,
)
from tests.concall_fixture import build_concall_pdf
from tests.factories import make_pdf
from tests.test_extraction import default_responder

HEADER = [
    "nse_symbol",
    "metric_normalized",
    "fiscal_year",
    "fiscal_quarter",
    "value",
    "unit",
    "source_note",
    "source_page",
    "scope",
]


# A later call where management only reports results: nothing forward-looking.
LATER_CALL_PAGES = [
    "Acme Industries Limited\nQ4 FY27 Earnings Conference Call\n"
    "Management:\nMr. Rahul Mehta - Chief Financial Officer",
    "Moderator:\nLadies and gentlemen, good day and welcome to the call.\n"
    "Rahul Mehta:\nThank you. Revenue grew 14% this year and margins were steady.",
]
# The same call without a participant list: no speaker is known to be management.
NO_MANAGEMENT_PAGES = [
    "Acme Industries Limited\nQ4 FY27 Earnings Conference Call",
    "Moderator:\nLadies and gentlemen, good day and welcome to the call.\n"
    "Rahul Mehta:\nThank you. We expect revenue growth of 15-18% in FY28.\n"
    "Rahul Mehta:\nWe aim to reach 20% EBITDA margin by Q4 FY28.",
]


@pytest.fixture
def samples(tmp_path: Path) -> Path:
    """Two calls: the synthetic Q2FY26 call (three FY27 targets) and a later Q4FY27 call,
    so the FY27 targets have ended. Plus files the command must skip."""
    folder = tmp_path / "ACME"
    folder.mkdir()
    (folder / "ACME_Q2FY26.pdf").write_bytes(build_concall_pdf())
    (folder / "ACME_Q4FY27.pdf").write_bytes(make_pdf(LATER_CALL_PAGES))
    (folder / "ACME_Q3FY26.pdf").write_bytes(b"<html>not a pdf</html>")
    (folder / "press-release.pdf").write_bytes(make_pdf(["Press release"]))
    (folder / "results").mkdir()
    (folder / "results" / "ACME_Q1FY26.pdf").write_bytes(make_pdf(["Results"]))
    return folder


def _options(folder: Path, **overrides: Any) -> Options:
    values: dict[str, Any] = {
        "symbol": "ACME",
        "name": "Acme Industries Ltd",
        "sector": "Industrials",
        "directory": folder,
        "allow_fake": True,
    }
    values.update(overrides)
    return Options(**values)


def _pipeline(app: FastAPI, llm: FakeLLMProvider) -> Pipeline:
    return Pipeline(
        settings=app.state.settings,
        sessionmaker=app.state.sessionmaker,
        storage=app.state.storage,
        embedder=app.state.embedder,
        llm=llm,
    )


class Out:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


async def _statuses(app: FastAPI) -> dict[str, DocumentStatus]:
    async with app.state.sessionmaker() as session:
        rows = await session.execute(select(Document.period_label, Document.status))
        return {label: status for label, status in rows}


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == HEADER
        return list(reader)


def test_find_transcripts_sorts_by_quarter_and_skips_bad_files(samples: Path) -> None:
    found, skipped = find_transcripts(samples, "ACME", 50 * 1024 * 1024)
    assert [t.period for t in found] == ["Q2FY26", "Q4FY27"]
    assert sorted(skipped) == [
        "ACME_Q3FY26.pdf: not a PDF (no %PDF- header)",
        "press-release.pdf: name is not ACME_Q<n>FY<yy>.pdf",
    ]
    _, too_big = find_transcripts(samples, "ACME", 100)
    assert any("over the upload limit" in note for note in too_big)


async def test_refuses_fake_providers_without_allow_fake(app: FastAPI, samples: Path) -> None:
    out = Out()
    code = await run(_options(samples, allow_fake=False), _pipeline(app, FakeLLMProvider()), out)
    assert code == EXIT_USAGE
    assert "--allow-fake is for tests only" in out.text
    async with app.state.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Company)) == 0


async def test_pauses_on_daily_quota_resumes_and_writes_actuals_todo(
    app: FastAPI, samples: Path
) -> None:
    def quota(messages: list[Message]) -> Any:
        return LLMQuotaExhausted("Groq daily quota exhausted: tokens per day (TPD)")

    llm = FakeLLMProvider(responder=quota, default_model="openai/gpt-oss-120b")
    out = Out()
    code = await run(_options(samples), _pipeline(app, llm), out)

    assert code == EXIT_PAUSED
    assert out.lines[-1] == PAUSED_MESSAGE
    assert "Created company Acme Industries Ltd (ACME)." in out.lines
    assert "ACME Q2FY26: transcript checks: OK" in out.lines
    assert "  ACME Q2FY26: embedded, 1/8 management chunks handled" in out.lines
    assert "  ACME Q4FY27: parsed, 0/0 management chunks handled" in out.lines
    assert not (samples / "actuals_todo.csv").exists()

    # Same command again (quota back): reuses the uploads, resumes, finishes.
    llm.responder = default_responder
    out = Out()
    code = await run(_options(samples), _pipeline(app, llm), out)

    assert code == EXIT_OK, out.text
    assert "Created company" not in out.text
    assert await _statuses(app) == {
        "Q2FY26": DocumentStatus.EXTRACTED,
        "Q4FY27": DocumentStatus.EXTRACTED,
    }
    assert "Wrote actuals_todo.csv: 4 actual(s) needed for promises due by Q4FY27." in out.text
    assert "Next: copy actuals_todo.csv to actuals.csv" in out.text

    rows = _rows(samples / "actuals_todo.csv")
    assert [
        (r["metric_normalized"], r["fiscal_year"], r["fiscal_quarter"], r["unit"]) for r in rows
    ] == [
        ("ebitda_margin", "2027", "", "percent"),
        # "Net debt free" has no number in the quote, so the claim is directional and
        # needs the year before as a baseline too.
        ("net_debt", "2026", "", "inr_crore"),
        ("net_debt", "2027", "", "inr_crore"),
        ("revenue_growth", "2027", "", "percent"),
    ]
    assert all(r["nse_symbol"] == "ACME" and r["value"] == "" for r in rows)
    assert rows[0]["source_note"] == "needed for: EBITDA margin 19–20% in FY27"
    assert rows[1]["source_note"] == "needed for: baseline for Net debt to decrease by FY27"

    # A third run changes nothing and doesn't call the LLM.
    llm.calls.clear()
    assert await run(_options(samples), _pipeline(app, llm), Out()) == EXIT_OK
    assert llm.calls == []
    async with app.state.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Document)) == 2


async def test_imports_filled_actuals_then_verifies(app: FastAPI, samples: Path) -> None:
    llm = FakeLLMProvider(responder=default_responder, default_model="openai/gpt-oss-120b")
    assert await run(_options(samples), _pipeline(app, llm), Out()) == EXIT_OK

    todo = _rows(samples / "actuals_todo.csv")
    values = {"ebitda_margin": "19.6", "revenue_growth": "14.2", "net_debt": ""}
    with (samples / "actuals.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        for row in todo:
            writer.writerow(
                row | {"value": values[row["metric_normalized"]], "source_note": "FY27 results"}
            )

    out = Out()
    code = await run(_options(samples), _pipeline(app, llm), out)

    assert code == EXIT_OK, out.text
    assert "Imported actuals.csv: 2 new, 0 updated, 2 row(s) with no value skipped." in out.lines
    assert "  Say-Do (latest targets):   1/2 = 50.0% (small sample)" in out.lines
    assert any(line.startswith("  missed: Revenue growth 15–18% in FY27") for line in out.lines)
    assert "  2 needed actual(s) still missing:" in out.lines
    assert "    net_debt FY26 (inr_crore)" in out.lines
    assert "    net_debt FY27 (inr_crore)" in out.lines


async def test_transcript_without_management_speakers_fails_loudly(
    app: FastAPI, samples: Path
) -> None:
    (samples / "ACME_Q4FY27.pdf").write_bytes(make_pdf(NO_MANAGEMENT_PAGES))
    llm = FakeLLMProvider(responder=default_responder, default_model="openai/gpt-oss-120b")
    out = Out()
    code = await run(_options(samples), _pipeline(app, llm), out)

    assert code == EXIT_FAILED, out.text
    # The good call is still extracted; the bad one is failed, never "extracted".
    assert await _statuses(app) == {
        "Q2FY26": DocumentStatus.EXTRACTED,
        "Q4FY27": DocumentStatus.FAILED,
    }
    warning = next(line for line in out.lines if line.startswith("ACME Q4FY27: warning:"))
    assert "No management speakers recognised in this transcript (3 speaker turn(s)" in warning
    assert "1 transcript(s) had no management speakers" in out.text
    assert not (samples / "actuals_todo.csv").exists()
    async with app.state.sessionmaker() as session:
        error = await session.scalar(
            select(Document.error_message).where(Document.period_label == "Q4FY27")
        )
    assert error is not None and error.startswith("No management speakers recognised")


async def test_reextract_rebuilds_documents_from_an_older_prompt(
    app: FastAPI, samples: Path
) -> None:
    from sqlalchemy import update

    from app.models import Claim, ExtractionRun

    llm = FakeLLMProvider(responder=default_responder, default_model="openai/gpt-oss-120b")
    assert await run(_options(samples), _pipeline(app, llm), Out()) == EXIT_OK
    async with app.state.sessionmaker() as session:
        await session.execute(update(ExtractionRun).values(prompt_version="extract-v2"))
        await session.execute(update(Claim).values(prompt_version="extract-v2"))
        await session.commit()

    # Without --reextract, extracted documents are left alone.
    out = Out()
    assert await run(_options(samples), _pipeline(app, llm), out) == EXIT_OK
    assert "re-extracting" not in out.text

    out = Out()
    assert await run(_options(samples, reextract=True), _pipeline(app, llm), out) == EXIT_OK
    assert out.text.count("re-extracting with extract-v4 (last run used extract-v2)") == 2
    assert "chunking and embedding" not in out.text
    assert await _statuses(app) == {
        "Q2FY26": DocumentStatus.EXTRACTED,
        "Q4FY27": DocumentStatus.EXTRACTED,
    }
    async with app.state.sessionmaker() as session:
        versions = set(await session.scalars(select(Claim.prompt_version)))
    assert versions == {"extract-v4"}

    # Nothing is rebuilt twice.
    out = Out()
    assert await run(_options(samples, reextract=True), _pipeline(app, llm), out) == EXIT_OK
    assert "re-extracting" not in out.text
