"""The verification agent (LangGraph): routing, LLM judging, budget, persistence, API."""

import uuid
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.config import Settings
from app.models import (
    ChunkSection,
    ClaimType,
    Direction,
    MetricNormalized,
    SpeakerRole,
    Verification,
    VerificationMethod,
    VerificationRun,
    VerificationRunStatus,
    VerificationStatus,
)
from app.services.embeddings import FakeEmbeddingProvider
from app.services.linking import company_as_of, link_company
from app.services.llm import FakeLLMProvider
from app.services.llm_budget import LLMBudget
from app.services.verification import (
    VerifyContext,
    build_graph,
    link_and_verify,
    load_actuals,
    load_thread_views,
)
from scripts.seed_demo import DemoChunk, DemoClaim, demo_responder
from tests.verification_factories import Calls, csv_rows, seed_company

S = VerificationStatus
M = MetricNormalized
MGMT = SpeakerRole.MANAGEMENT


def say(text: str, page: int = 2) -> DemoChunk:
    return DemoChunk("Asha Iyer", MGMT, ChunkSection.PREPARED_REMARKS, page, text)


ROUTES: Calls = {
    "Q1FY26": (
        [
            say("We expect an EBITDA margin of 18% to 19% in FY26."),
            say("We will launch the Zenith product range in Q3 FY26."),
            say("We expect PAT margin of 10% over time."),
            say("We aim to open 50 new stores by FY28."),
        ],
        [
            DemoClaim(
                0,
                "We expect an EBITDA margin of 18% to 19% in FY26.",
                M.EBITDA_MARGIN,
                "EBITDA margin",
                ClaimType.NUMERIC,
                "in FY26",
                2026,
                low="18",
                high="19",
                unit="percent",
            ),
            DemoClaim(
                1,
                "We will launch the Zenith product range in Q3 FY26.",
                M.PRODUCT_LAUNCH,
                "Zenith launch",
                ClaimType.QUALITATIVE,
                "in Q3 FY26",
                2026,
                3,
            ),
            DemoClaim(
                2,
                "We expect PAT margin of 10% over time.",
                M.PAT_MARGIN,
                "PAT margin",
                ClaimType.NUMERIC,
                "over time",
                None,
                low="10",
                unit="percent",
            ),
            DemoClaim(
                3,
                "We aim to open 50 new stores by FY28.",
                M.STORE_COUNT,
                "new stores",
                ClaimType.NUMERIC,
                "by FY28",
                2028,
                low="50",
                unit="count",
                direction=Direction.INCREASE,
            ),
        ],
    ),
    "Q2FY26": ([say("The Zenith launch is progressing well.")], []),
    "Q3FY26": ([say("We successfully launched the Zenith range in December.")], []),
    "Q4FY26": ([say("Demand stayed healthy through the year.")], []),
}
ROUTE_ACTUALS = csv_rows("TESTCO", "ebitda_margin,2026,,18.6,percent,FY26,4")


async def _context(
    app: FastAPI, session: Any, company_id: uuid.UUID, llm: FakeLLMProvider, budget: LLMBudget
) -> VerifyContext:
    settings: Settings = app.state.settings
    await link_company(
        session, company_id, llm, budget, model="m", batch_size=15, dropped_after_calls=2
    )
    return VerifyContext(
        session=session,
        company_id=company_id,
        threads=await load_thread_views(session, company_id),
        actuals=await load_actuals(session, company_id),
        as_of=await company_as_of(session, company_id),
        llm=llm,
        embedder=FakeEmbeddingProvider(),
        budget=budget,
        settings=settings,
    )


async def _visit(graph: Any, thread_id: uuid.UUID) -> list[str]:
    nodes = []
    async for update in graph.astream({"thread_id": thread_id}, stream_mode="updates"):
        nodes.extend(update)
    return nodes


async def test_every_route_is_reached(app: FastAPI) -> None:
    sessionmaker = app.state.sessionmaker
    company_id = await seed_company(sessionmaker, ROUTES, actuals_csv=ROUTE_ACTUALS)
    llm = FakeLLMProvider(responder=demo_responder)
    async with sessionmaker() as session:
        ctx = await _context(app, session, company_id, llm, LLMBudget(5))
        graph = build_graph(ctx)
        paths = {}
        for thread in ctx.threads.values():
            paths[thread.metric] = await _visit(graph, thread.id)
        await session.commit()

    assert paths == {
        "ebitda_margin": ["load_thread", "deterministic_check", "persist"],
        "product_launch": ["load_thread", "gather_evidence", "llm_judge", "persist"],
        "pat_margin": ["load_thread", "unverifiable", "persist"],
        "store_count": ["load_thread", "mark_dropped", "find_explanation", "persist"],
    }
    async with sessionmaker() as session:
        rows = list(await session.scalars(select(Verification).where(Verification.is_latest)))
    by_method = {(v.status, v.method) for v in rows}
    assert by_method == {
        (S.DELIVERED, VerificationMethod.DETERMINISTIC),
        (S.DELIVERED, VerificationMethod.LLM),
        (S.UNVERIFIABLE, VerificationMethod.DETERMINISTIC),
        (S.DROPPED, VerificationMethod.DETERMINISTIC),
    }
    llm_verdict = next(v for v in rows if v.method is VerificationMethod.LLM)
    unverifiable = next(v for v in rows if v.status is S.UNVERIFIABLE)
    assert llm_verdict.details["quote"] == "We successfully launched the Zenith range in December."
    assert llm_verdict.evidence_chunk_ids
    assert len(llm.calls) == 1  # only the qualitative claim used the LLM
    assert unverifiable.reasoning.startswith("Unverifiable: no target period could be resolved")


EVENTS: Calls = {
    "Q1FY26": (
        [
            say(f"We will commission the {name} plant in Q3 FY26.")
            for name in ("Alpha", "Beta", "Gamma")
        ],
        [
            DemoClaim(
                i,
                f"We will commission the {name} plant in Q3 FY26.",
                M.PLANT_COMMISSIONING,
                f"{name} plant",
                ClaimType.QUALITATIVE,
                "in Q3 FY26",
                2026,
                3,
            )
            for i, name in enumerate(("Alpha", "Beta", "Gamma"))
        ],
    ),
    "Q2FY26": (
        [say(f"The {name} plant has been commissioned.") for name in ("Alpha", "Beta", "Gamma")],
        [],
    ),
}


async def test_llm_budget_is_respected(app: FastAPI) -> None:
    sessionmaker = app.state.sessionmaker
    company_id = await seed_company(sessionmaker, EVENTS)
    llm = FakeLLMProvider(responder=demo_responder)
    settings = app.state.settings.model_copy(update={"verify_max_llm_calls": 1})
    run = await link_and_verify(sessionmaker, llm, FakeEmbeddingProvider(), settings, company_id)

    assert run.status is VerificationRunStatus.COMPLETED
    assert len(llm.calls) == 1 and run.llm_calls == 1
    assert run.status_counts == {"delivered": 1, "unverifiable": 2}
    async with sessionmaker() as session:
        reasons = sorted(
            v.reasoning
            for v in await session.scalars(
                select(Verification).where(Verification.status == S.UNVERIFIABLE)
            )
        )
    assert reasons == ["Unverifiable: llm budget exhausted."] * 2


async def test_failed_quote_validation_twice_is_unverifiable(app: FastAPI) -> None:
    sessionmaker = app.state.sessionmaker
    company_id = await seed_company(
        sessionmaker, {"Q1FY26": EVENTS["Q1FY26"], "Q2FY26": EVENTS["Q2FY26"]}
    )
    made_up = {
        "status": "delivered",
        "reasoning": "It happened.",
        "evidence_ids": ["E1"],
        "quote": "The plant was commissioned in record time with 120% capacity.",
        "quote_evidence_id": "E1",
    }
    llm = FakeLLMProvider([made_up] * 6)
    run = await link_and_verify(
        sessionmaker, llm, FakeEmbeddingProvider(), app.state.settings, company_id
    )
    assert len(llm.calls) == 6  # 3 threads x (call + one retry)
    assert "could not be found" in llm.calls[1].user  # the retry says what was wrong
    assert run.status_counts == {"unverifiable": 3}
    async with sessionmaker() as session:
        verification = await session.scalar(select(Verification).limit(1))
    assert verification is not None
    assert "could not be found in the evidence twice" in verification.reasoning
    assert verification.method is VerificationMethod.LLM


async def test_quote_fixed_on_retry_is_accepted(app: FastAPI) -> None:
    sessionmaker = app.state.sessionmaker
    one: Calls = {
        "Q1FY26": (EVENTS["Q1FY26"][0][:1], EVENTS["Q1FY26"][1][:1]),
        "Q2FY26": (EVENTS["Q2FY26"][0][:1], []),
    }
    company_id = await seed_company(sessionmaker, one)
    bad = {
        "status": "delivered",
        "reasoning": "r",
        "evidence_ids": [],
        "quote": "nope nope",
        "quote_evidence_id": "E1",
    }
    llm = FakeLLMProvider([bad], responder=demo_responder)
    run = await link_and_verify(
        sessionmaker, llm, FakeEmbeddingProvider(), app.state.settings, company_id
    )
    assert len(llm.calls) == 2
    assert run.status_counts == {"delivered": 1}


async def test_only_one_latest_verification_per_thread_after_reruns(app: FastAPI) -> None:
    sessionmaker = app.state.sessionmaker
    company_id = await seed_company(sessionmaker, ROUTES, actuals_csv=ROUTE_ACTUALS)
    llm = FakeLLMProvider(responder=demo_responder)
    for _ in range(3):
        await link_and_verify(
            sessionmaker, llm, FakeEmbeddingProvider(), app.state.settings, company_id
        )
    async with sessionmaker() as session:
        total = await session.scalar(select(func.count()).select_from(Verification))
        latest = (
            await session.execute(
                select(Verification.thread_id, func.count())
                .where(Verification.is_latest)
                .group_by(Verification.thread_id)
            )
        ).all()
        runs = await session.scalar(select(func.count()).select_from(VerificationRun))
    assert total == 12 and runs == 3
    assert len(latest) == 4 and all(count == 1 for _, count in latest)


async def test_verify_api_flow(app: FastAPI, client: httpx.AsyncClient) -> None:
    sessionmaker = app.state.sessionmaker
    company_id = await seed_company(sessionmaker, ROUTES, actuals_csv=ROUTE_ACTUALS)
    app.state.llm = FakeLLMProvider(responder=demo_responder)

    response = await client.post(f"/companies/{company_id}/verify")
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    assert response.json()["kind"] == "verify"

    runs = (await client.get(f"/companies/{company_id}/verification-runs")).json()
    assert runs[0]["id"] == run_id and runs[0]["status"] == "completed"
    assert runs[0]["threads_total"] == 4 and runs[0]["threads_verified"] == 4
    assert runs[0]["as_of_period"] == "Q4FY26"
    assert runs[0]["llm_calls"] == 1 and runs[0]["embedding_calls"] >= 1

    threads = (await client.get(f"/companies/{company_id}/threads")).json()
    assert threads["total"] == 4
    dropped = (
        await client.get(f"/companies/{company_id}/threads", params={"dropped": True})
    ).json()
    assert [t["metric_normalized"] for t in dropped["threads"]] == ["store_count"]
    delivered = (
        await client.get(f"/companies/{company_id}/threads", params={"status": "delivered"})
    ).json()
    assert {t["metric_normalized"] for t in delivered["threads"]} == {
        "ebitda_margin",
        "product_launch",
    }

    margin = next(t for t in threads["threads"] if t["metric_normalized"] == "ebitda_margin")
    detail = (await client.get(f"/threads/{margin['id']}")).json()
    assert detail["actual_used"]["value"] == "18.6" and not detail["actual_used"]["derived"]
    assert detail["mentions"][0]["change_type"] == "new"
    assert detail["latest_verification"]["method"] == "deterministic"

    launch = next(t for t in threads["threads"] if t["metric_normalized"] == "product_launch")
    detail = (await client.get(f"/threads/{launch['id']}")).json()
    assert (
        detail["evidence_chunks"][0]["text"]
        == "We successfully launched the Zenith range in December."
    )

    card = (await client.get(f"/companies/{company_id}/scorecard")).json()
    assert card["say_do"] == {"delivered": 2, "denominator": 3, "ratio": 0.6667, "low_sample": True}
    assert [t["metric"] for t in card["dropped"]] == ["store_count"]

    link = await client.post(f"/companies/{company_id}/link")
    assert link.status_code == 202 and link.json()["kind"] == "link"


async def test_verify_conflicts_while_a_run_is_active(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    sessionmaker = app.state.sessionmaker
    company_id = await seed_company(sessionmaker, ROUTES)
    assert app.state.verifying.try_start(company_id)
    response = await client.post(f"/companies/{company_id}/verify")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "verification_in_progress"
    app.state.verifying.finish(company_id)


async def test_stale_running_run_is_marked_failed(app: FastAPI) -> None:
    sessionmaker = app.state.sessionmaker
    company_id = await seed_company(sessionmaker, ROUTES)
    async with sessionmaker() as session:
        session.add(VerificationRun(company_id=company_id, kind="verify", verifier_version="v"))
        await session.commit()
    run = await link_and_verify(
        sessionmaker,
        FakeLLMProvider(responder=demo_responder),
        FakeEmbeddingProvider(),
        app.state.settings,
        company_id,
    )
    assert run.status is VerificationRunStatus.COMPLETED
    async with sessionmaker() as session:
        statuses = sorted(r.status.value for r in await session.scalars(select(VerificationRun)))
    assert statuses == ["completed", "failed"]


async def test_explanation_summary_only_when_enabled(app: FastAPI) -> None:
    from scripts.seed_demo import ACTUALS_CSV, CALLS

    sessionmaker = app.state.sessionmaker
    company_id = await seed_company(sessionmaker, CALLS, symbol="DEMOIND", actuals_csv=ACTUALS_CSV)

    def responder(messages: list[Any]) -> dict[str, Any]:
        user = messages[1]["content"]
        if "EXCERPTS:" not in user:
            return demo_responder(messages)
        return {
            "summary": "Management blamed weak rural demand.",
            "quote": "the main reason was the impact of weak rural demand",
            "quote_excerpt_id": "X1" if "[X1] Q4FY26" in user else "X2",
        }

    off = FakeLLMProvider(responder=responder)
    await link_and_verify(
        sessionmaker, off, FakeEmbeddingProvider(), app.state.settings, company_id
    )
    assert not any("EXCERPTS:" in c.user for c in off.calls)

    on = FakeLLMProvider(responder=responder)
    settings = app.state.settings.model_copy(update={"verify_summarize_explanations": True})
    await link_and_verify(sessionmaker, on, FakeEmbeddingProvider(), settings, company_id)
    async with sessionmaker() as session:
        missed = await session.scalar(
            select(Verification).where(Verification.is_latest, Verification.status == S.MISSED)
        )
    assert missed is not None
    assert len(missed.explanation_chunk_ids) == 2
    assert missed.explanation_summary == "Management blamed weak rural demand."
