"""Kept-vs-original targets, the promise x call ledger, the companies summary, the
transcript file endpoint and search thread links."""

import uuid
from decimal import Decimal
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select, update

from app.models import ChangeType, Company, Direction, Document, VerificationStatus
from app.services.deterministic_check import check_against_original
from app.services.ledger import LedgerThread, Row, build_ledger, compact_number, group_indian
from app.services.linking import Mention
from app.services.periods import end_ordinal
from app.services.scorecard import ScoredThread, compute_scorecard
from scripts.seed_demo import seed_demo
from tests.verification_factories import mention

S = VerificationStatus
C = ChangeType
FY26 = (2026, None)


def q(label: str) -> int:
    """Quarter ordinal for "Q2FY26"."""
    return end_ordinal(2000 + int(label[-2:]), int(label[1]))


def pat(call: str, low: str) -> Mention:
    return mention(call, metric="pat", low=low, unit="inr_crore", target=FY26)


# ---------------------------------------------------------- original targets


def test_lowered_then_met_is_kept_after_lowering_not_kept_vs_original() -> None:
    mentions = [pat("Q1FY26", "450"), pat("Q3FY26", "400")]
    check = check_against_original(mentions, S.DELIVERED, Decimal("410"), "inr_crore")
    assert check.kept is False
    assert check.kept_after_lowering is True


def test_raised_and_met_counts_against_both_targets() -> None:
    mentions = [
        mention(
            "Q1FY26", metric="revenue_growth", low="10", high="12", unit="percent", target=FY26
        ),
        mention(
            "Q2FY26", metric="revenue_growth", low="12", high="14", unit="percent", target=FY26
        ),
    ]
    check = check_against_original(mentions, S.DELIVERED, Decimal("12.7"), "percent")
    assert check.kept is True and check.kept_after_lowering is False


def test_original_target_compared_after_unit_conversion() -> None:
    mentions = [pat("Q1FY26", "450"), pat("Q3FY26", "400")]
    # 46,000 lakh = 460 crore: meets the original 450 too.
    check = check_against_original(mentions, S.DELIVERED, Decimal("46000"), "inr_lakh")
    assert check.kept is True and check.kept_after_lowering is False


def test_delivered_after_the_deadline_moved_is_not_kept_vs_original() -> None:
    mentions = [
        mention("Q1FY26", metric="plant_commissioning", target=(2026, 2)),
        mention("Q2FY26", metric="plant_commissioning", target=(2026, 3)),
    ]
    check = check_against_original(mentions, S.DELIVERED, None, None)
    assert check.kept is False and check.kept_after_lowering is False


def test_unscored_and_failed_verdicts() -> None:
    mentions = [pat("Q1FY26", "450")]
    assert check_against_original(mentions, S.IN_PROGRESS, None, None).kept is None
    assert check_against_original(mentions, None, None, None).kept is None
    assert check_against_original(mentions, S.MISSED, Decimal("300"), "inr_crore").kept is False
    assert check_against_original(mentions, S.DROPPED, None, None).kept is False
    # A single unrevised target that was delivered is kept against both.
    assert check_against_original(mentions, S.DELIVERED, Decimal("460"), "inr_crore").kept


def test_scorecard_reports_latest_and_original_ratios() -> None:
    def t(status: VerificationStatus, kept_original: bool | None) -> ScoredThread:
        return ScoredThread(
            thread_id=uuid.uuid4(),
            title=str(uuid.uuid4()),
            metric="pat",
            status=status,
            kept_original=kept_original,
        )

    card = compute_scorecard(
        [
            t(S.DELIVERED, True),
            t(S.DELIVERED, False),  # lowered, then met
            t(S.MISSED, False),
            t(S.DROPPED, False),
            t(S.IN_PROGRESS, None),
        ]
    )
    assert (card.say_do_latest.kept, card.say_do_latest.total) == (2, 4)
    assert card.say_do_latest.ratio == 0.5
    assert (card.say_do_original.kept, card.say_do_original.total) == (1, 4)
    assert card.say_do_original.ratio == 0.25


# -------------------------------------------------------------------- ledger


def lt(
    mentions: list[Mention],
    changes: list[ChangeType | None],
    status: VerificationStatus | None,
    title: str = "thread",
    **kwargs: Any,
) -> LedgerThread:
    return LedgerThread(
        thread_id=uuid.uuid4(),
        title=title,
        metric=mentions[-1].metric,
        mentions=mentions,
        change_types={m.claim_id: c for m, c in zip(mentions, changes, strict=True)},
        status=status,
        **kwargs,
    )


CALLS = [q("Q1FY26"), q("Q2FY26"), q("Q3FY26"), q("Q4FY26")]


def kinds(row: Row) -> list[tuple[str, str, str]]:
    return [(c.period, c.kind, c.text) for c in row.cells]


def test_lowered_then_met_row_and_silent_cell() -> None:
    row_input = lt(
        [pat("Q1FY26", "450"), pat("Q3FY26", "400")],
        [C.NEW, C.LOWERED],
        S.DELIVERED,
        kept_after_lowering=True,
        actual_value=Decimal("410"),
        actual_unit="inr_crore",
        actual_ordinal=q("Q4FY26"),
        actual_label="FY26",
    )
    ledger = build_ledger([row_input], CALLS)
    assert ledger.periods == ["Q1FY26", "Q2FY26", "Q3FY26", "Q4FY26"]
    [row] = ledger.rows
    assert kinds(row) == [
        ("Q1FY26", "said", "450"),
        ("Q2FY26", "silent", ""),  # a call after the first mention, still open
        ("Q3FY26", "lowered", "400"),
        ("Q4FY26", "kept", "410"),
    ]
    assert row.kept_after_lowering and row.verdict is S.DELIVERED
    assert row.comparison is not None
    assert row.comparison.original is not None and row.comparison.original.low == Decimal("450")
    assert row.comparison.target.low == Decimal("400")
    assert row.comparison.actual_period == "FY26"


def test_moved_deadline_open_cell_and_slip() -> None:
    plant = [
        mention("Q1FY26", metric="plant_commissioning", target=(2026, 2)),
        mention("Q2FY26", metric="plant_commissioning", target=(2026, 3)),
        mention("Q3FY26", metric="plant_commissioning", target=(2027, 1)),
    ]
    ledger = build_ledger(
        [lt(plant, [C.NEW, C.DELAYED, C.DELAYED], S.IN_PROGRESS, as_of=q("Q4FY26"))], CALLS
    )
    [row] = ledger.rows
    assert ledger.periods[-1] == "Q1FY27"
    assert kinds(row) == [
        ("Q1FY26", "said", "Q2FY26"),
        ("Q2FY26", "moved", "→ Q3FY26"),
        ("Q3FY26", "moved", "→ Q1FY27"),
        ("Q4FY26", "silent", ""),
        ("Q1FY27", "open", "Q1FY27"),  # no call there yet: the open deadline
    ]
    assert row.slipped_quarters == 3
    assert row.first_target_period == "Q2FY26" and row.target_period == "Q1FY27"


def test_missed_row_directional_text_and_none_cells() -> None:
    revenue = [mention("Q1FY26", metric="revenue", low="5000", unit="inr_crore", target=FY26)]
    utilisation = [
        mention("Q2FY26", metric="capacity_utilization", direction=Direction.INCREASE, target=FY26)
    ]
    ledger = build_ledger(
        [
            lt(
                revenue,
                [C.NEW],
                S.MISSED,
                title="revenue",
                actual_value=Decimal("4620"),
                actual_unit="inr_crore",
                actual_ordinal=q("Q4FY26"),
            ),
            lt(
                utilisation,
                [C.NEW],
                S.DELIVERED,
                title="utilisation",
                actual_value=Decimal("78"),
                actual_unit="percent",
                actual_ordinal=q("Q4FY26"),
            ),
        ],
        CALLS,
    )
    by_title = {r.title: r for r in ledger.rows}
    assert kinds(by_title["revenue"])[-1] == ("Q4FY26", "missed", "4,620")
    assert kinds(by_title["utilisation"]) == [
        ("Q1FY26", "none", ""),  # before the first mention
        ("Q2FY26", "said", "up"),
        ("Q3FY26", "silent", ""),
        ("Q4FY26", "kept", "78%"),
    ]
    assert by_title["utilisation"].comparison is None  # directional: no target bar


def test_dropped_promise_goes_silent_and_rows_are_ordered() -> None:
    debt = [
        mention("Q1FY26", metric="net_debt", low="200", unit="inr_crore", target=(2027, None)),
        mention("Q2FY26", metric="net_debt", low="200", unit="inr_crore", target=(2027, None)),
    ]
    kept = [mention("Q1FY26", metric="capex", low="300", high="350", unit="inr_crore", target=FY26)]
    open_ = [mention("Q3FY26", metric="ebitda_margin", low="19", high="20", unit="percent")]
    missed = [mention("Q1FY26", metric="revenue", low="5000", unit="inr_crore", target=FY26)]
    ledger = build_ledger(
        [
            lt(kept, [C.NEW], S.DELIVERED, title="kept", actual_ordinal=q("Q4FY26")),
            lt(debt, [C.NEW, C.REITERATED], S.DROPPED, title="dropped"),
            lt(missed, [C.NEW], S.MISSED, title="missed", actual_ordinal=q("Q4FY26")),
            lt(open_, [C.NEW], S.IN_PROGRESS, title="open"),
        ],
        CALLS,
    )
    assert [r.title for r in ledger.rows] == ["open", "missed", "dropped", "kept"]
    assert ledger.periods[-1] == "Q4FY27"  # the latest target referenced
    dropped = next(r for r in ledger.rows if r.title == "dropped")
    assert [c.kind for c in dropped.cells[:4]] == ["said", "said", "silent", "silent"]
    assert all(c.kind == "none" for c in dropped.cells[4:])  # no calls there


def test_empty_ledger() -> None:
    ledger = build_ledger([], CALLS)
    assert ledger.periods == [] and ledger.rows == []


def test_indian_number_formatting() -> None:
    assert group_indian(4620) == "4,620"
    assert group_indian(1234567) == "12,34,567"
    assert group_indian(100) == "100"
    assert compact_number(Decimal("18.50")) == "18.5"
    assert compact_number(Decimal("18.528")) == "18.53"
    assert compact_number(Decimal("40000")) == "40,000"


# --------------------------------------------------------------------- API


async def test_ledger_endpoint_on_demo(app: FastAPI, client: httpx.AsyncClient) -> None:
    company_id = await seed_demo(app.state.sessionmaker, app.state.settings)
    response = await client.get(f"/companies/{company_id}/ledger")
    assert response.status_code == 200
    body = response.json()
    assert body["periods"][0] == "Q1FY26" and body["periods"][-1] == "Q4FY27"
    assert body["as_of_period"] == "Q4FY26"
    rows = {r["title"]: r for r in body["rows"]}
    assert [r["verdict"] for r in body["rows"]][:4] == [
        "in_progress",
        "in_progress",
        "missed",
        "dropped",
    ]
    pat_row = rows["PAT ₹400 crore in FY26"]
    assert pat_row["verdict"] == "delivered" and pat_row["kept_after_lowering"] is True
    assert [(c["kind"], c["text"]) for c in pat_row["cells"][:4]] == [
        ("said", "450"),
        ("silent", ""),
        ("lowered", "400"),
        ("kept", "410"),
    ]
    assert pat_row["comparison"]["original_low"] == 450.0
    plant = rows["Line 4 commissioning by Q1FY27"]
    assert plant["slipped_quarters"] == 3
    assert [c["kind"] for c in plant["cells"][:5]] == ["said", "moved", "moved", "said", "open"]
    assert rows["Revenue ₹5,000 crore in FY26"]["cells"][3] == {
        "period": "Q4FY26",
        "kind": "missed",
        "text": "4,620",
    }
    margin = rows["EBITDA margin 18–19% in FY26"]["cells"]
    assert margin[0]["text"] == "18–19%"
    assert margin[3] == {"period": "Q4FY26", "kind": "kept", "text": "18.5%"}  # one decimal
    missing = await client.get(f"/companies/{uuid.uuid4()}/ledger")
    assert missing.status_code == 404


async def test_scorecard_original_ratio_on_demo(app: FastAPI, client: httpx.AsyncClient) -> None:
    company_id = await seed_demo(app.state.sessionmaker, app.state.settings)
    body = (await client.get(f"/companies/{company_id}/scorecard")).json()
    assert body["say_do_latest"] == {"kept": 6, "total": 8, "ratio": 0.75}
    assert body["say_do_original"] == {"kept": 5, "total": 8, "ratio": 0.625}
    threads = (await client.get(f"/companies/{company_id}/threads")).json()["threads"]
    flagged = [t["title"] for t in threads if t["verification"]["kept_after_lowering"]]
    assert flagged == ["PAT ₹400 crore in FY26"]
    pat_id = next(t["id"] for t in threads if t["title"] == flagged[0])
    detail = (await client.get(f"/threads/{pat_id}")).json()
    assert detail["latest_verification"]["kept_after_lowering"] is True
    assert detail["status"] == "delivered"


async def test_companies_list_has_summary(app: FastAPI, client: httpx.AsyncClient) -> None:
    await seed_demo(app.state.sessionmaker, app.state.settings)
    created = await client.post("/companies", json={"name": "Empty Co", "nse_symbol": "EMPTY"})
    assert created.status_code == 201
    body = (await client.get("/companies")).json()
    by_symbol = {c["nse_symbol"]: c for c in body}
    demo = by_symbol["DEMOIND"]
    assert demo["say_do_latest"] == {"kept": 6, "total": 8, "ratio": 0.75}
    assert demo["open_count"] == 2
    assert demo["latest_period"] == "Q4FY26"
    empty = by_symbol["EMPTY"]
    assert empty["say_do_latest"] == {"kept": 0, "total": 0, "ratio": None}
    assert empty["open_count"] == 0 and empty["latest_period"] is None


async def test_document_file_served_inline(app: FastAPI, client: httpx.AsyncClient) -> None:
    await seed_demo(app.state.sessionmaker, app.state.settings)
    async with app.state.sessionmaker() as session:
        document = (await session.scalars(select(Document).limit(1))).one()
    response = await client.get(f"/documents/{document.id}/file")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline")
    assert response.content.startswith(b"%PDF-")

    async with app.state.sessionmaker() as session:
        await session.execute(
            update(Document).where(Document.id == document.id).values(file_path="demo/gone.pdf")
        )
        await session.commit()
    gone = await client.get(f"/documents/{document.id}/file")
    assert gone.status_code == 404
    assert gone.json()["error"]["code"] == "file_not_found"


async def test_search_results_link_to_threads(app: FastAPI, client: httpx.AsyncClient) -> None:
    company_id = await seed_demo(app.state.sessionmaker, app.state.settings)
    async with app.state.sessionmaker() as session:
        assert await session.scalar(select(Company.id).where(Company.id == company_id))
    response = await client.post(
        f"/companies/{company_id}/search",
        json={"query": "We expect PAT of Rs. 450 crore in FY26.", "top_k": 30},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    linked = [r for r in results if r["thread_id"]]
    unlinked = [r for r in results if r["speaker_role"] == "moderator"]
    assert linked, "chunks with claims carry their thread id"
    assert all(r["thread_id"] is None for r in unlinked)
