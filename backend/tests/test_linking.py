"""Thread linking: deterministic grouping, LLM-linked ambiguous claims, change types,
dropped detection, titles and persistence."""

from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.models import ChangeType, Claim, ClaimThread, ClaimType, Direction
from app.services.linking import (
    change_type,
    change_types,
    group_mentions,
    is_dropped,
    link_company,
    name_keys,
    resolve_questions,
    thread_title,
)
from app.services.llm import FakeLLMProvider, Message
from app.services.llm_budget import LLMBudget
from tests.verification_factories import mention, ordinal

C = ChangeType


def groups_of(mentions: list[Any]) -> list[set[str]]:
    grouping = group_mentions(mentions)
    by_id = {m.claim_id: m for m in mentions}
    out = []
    for members in grouping.groups.members().values():
        out.append({by_id[grouping.mentions[i].claim_id].quote for i in members})
    return sorted(out, key=lambda g: sorted(g))


# ------------------------------------------------------------ change types


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (("19", "20"), ("19", "20"), C.REITERATED),
        (("19", "20"), ("20", "21"), C.RAISED),
        (("19", "20"), ("17", "18"), C.LOWERED),
        (("18", "21"), ("19", "20"), C.NARROWED),
        (("19", "20"), ("18", "21"), C.WIDENED),
        (("450", None), ("400", None), C.LOWERED),
        (("19", "20"), ("19.5", "20"), C.NARROWED),
    ],
)
def test_value_change_types(
    before: tuple[str, str | None], after: tuple[str, str | None], expected: ChangeType
) -> None:
    a = mention("Q1FY26", low=before[0], high=before[1], unit="percent")
    b = mention("Q2FY26", low=after[0], high=after[1], unit="percent")
    assert change_type(a, b) is expected


def test_change_type_converts_units_before_comparing() -> None:
    a = mention("Q1FY26", metric="capex", low="300", unit="inr_crore")
    b = mention("Q2FY26", metric="capex", low="30000", unit="inr_lakh")
    assert change_type(a, b) is C.REITERATED


def test_period_changes_are_delayed_or_pulled_forward() -> None:
    q2 = mention("Q1FY26", metric="plant_commissioning", target=(2026, 2))
    q3 = mention("Q2FY26", metric="plant_commissioning", target=(2026, 3))
    q1 = mention("Q3FY26", metric="plant_commissioning", target=(2026, 1))
    assert change_type(q2, q3) is C.DELAYED
    assert change_type(q3, q1) is C.PULLED_FORWARD
    # FY27 and Q4FY27 end together: not a delay.
    annual = mention("Q1FY26", target=(2027, None), direction=Direction.INCREASE)
    q4 = mention("Q2FY26", target=(2027, 4), direction=Direction.INCREASE)
    assert change_type(annual, q4) is C.REITERATED


def test_direction_changes() -> None:
    up = mention("Q1FY26", direction=Direction.INCREASE)
    flat = mention("Q2FY26", direction=Direction.MAINTAIN)
    assert change_type(up, flat) is C.LOWERED
    assert change_type(flat, up) is C.RAISED


def test_change_types_first_is_new() -> None:
    a = mention("Q1FY26", low="19", high="20", unit="percent")
    b = mention("Q2FY26", low="20", high="21", unit="percent")
    assert change_types([b, a]) == [C.NEW, C.RAISED]


# ---------------------------------------------------------------- grouping


def test_same_metric_and_period_link_deterministically() -> None:
    a = mention("Q1FY26", low="19", high="20", unit="percent", quote="a")
    b = mention("Q2FY26", low="19", high="20", unit="percent", quote="b")
    c = mention("Q3FY26", low="19", high="20", unit="percent", target=(2028, None), quote="c")
    d = mention("Q3FY26", metric="pat_margin", low="9", unit="percent", quote="d")
    assert groups_of([a, b, c, d]) == [{"a", "b"}, {"c"}, {"d"}]
    assert group_mentions([a, b, c, d]).questions == []


def test_by_period_metric_restated_later_is_a_delay() -> None:
    first = mention(
        "Q1FY26", metric="net_debt", low="0", unit="inr_crore", target=(2027, None), quote="a"
    )
    moved = mention(
        "Q2FY26", metric="net_debt", low="0", unit="inr_crore", target=(2028, None), quote="b"
    )
    assert groups_of([first, moved]) == [{"a", "b"}]
    assert change_types([first, moved]) == [C.NEW, C.DELAYED]


def test_by_period_rule_needs_same_target_and_open_period() -> None:
    first = mention(
        "Q1FY26", metric="net_debt", low="0", unit="inr_crore", target=(2026, None), quote="a"
    )
    # Said on the Q4FY26 call: FY26 is over, so FY27 is new guidance, not a delay.
    later = mention(
        "Q4FY26", metric="net_debt", low="0", unit="inr_crore", target=(2027, None), quote="b"
    )
    other = mention(
        "Q2FY26", metric="net_debt", low="100", unit="inr_crore", target=(2027, 2), quote="c"
    )
    assert groups_of([first, later, other]) == [{"a"}, {"b"}, {"c"}]
    # Margins are not "by <period>" metrics: FY27 and FY28 guidance stay separate.
    m1 = mention("Q1FY26", low="20", unit="percent", target=(2027, None), quote="d")
    m2 = mention("Q2FY26", low="20", unit="percent", target=(2028, None), quote="e")
    assert groups_of([m1, m2]) == [{"d"}, {"e"}]


def test_name_keys() -> None:
    assert name_keys("We will commission Line 4 in Q2 FY26.") == {"line 4"}
    assert name_keys("The Pune expansion remains on track for Q1 FY27.") == {"pune"}
    assert name_keys("We launched our Aura range in January.") == {"aura"}


def test_event_claims_sharing_a_name_link_without_llm() -> None:
    a = mention(
        "Q1FY26",
        metric="plant_commissioning",
        claim_type=ClaimType.QUALITATIVE,
        target=(2026, 2),
        quote="We will commission Line 4 in Q2 FY26.",
    )
    b = mention(
        "Q2FY26",
        metric="plant_commissioning",
        claim_type=ClaimType.QUALITATIVE,
        target=(2026, 3),
        quote="Line 4 will now be commissioned in Q3 FY26.",
    )
    grouping = group_mentions([a, b])
    assert len(grouping.groups.members()) == 1
    assert grouping.questions == []
    assert change_types([a, b]) == [C.NEW, C.DELAYED]


def test_ambiguous_claims_become_llm_questions() -> None:
    line4 = mention(
        "Q1FY26",
        metric="plant_commissioning",
        claim_type=ClaimType.QUALITATIVE,
        target=(2026, 2),
        quote="We will commission Line 4 in Q2 FY26.",
    )
    pune = mention(
        "Q3FY26",
        metric="plant_commissioning",
        claim_type=ClaimType.QUALITATIVE,
        target=(2027, 1),
        quote="We now expect the Pune expansion in Q1 FY27.",
    )
    launch = mention(
        "Q3FY26",
        metric="product_launch",
        claim_type=ClaimType.QUALITATIVE,
        target=(2027, 1),
        quote="We will launch a new range.",
    )
    grouping = group_mentions([line4, pune, launch])
    assert len(grouping.questions) == 1  # the launch has no earlier same-metric candidate
    question = grouping.questions[0]
    assert grouping.group_members(question.item)[0].quote.startswith("We now expect the Pune")
    assert [grouping.group_members(c)[0].quote for c in question.candidates] == [line4.quote]


def _line4_and_pune() -> list[Any]:
    return [
        mention(
            "Q1FY26",
            metric="plant_commissioning",
            claim_type=ClaimType.QUALITATIVE,
            target=(2026, 2),
            quote="We will commission Line 4 in Q2 FY26.",
        ),
        mention(
            "Q3FY26",
            metric="plant_commissioning",
            claim_type=ClaimType.QUALITATIVE,
            target=(2027, 1),
            quote="We now expect the Pune expansion in Q1 FY27.",
        ),
    ]


def _link_all(messages: list[Message]) -> dict[str, Any]:
    import re

    items = re.findall(r"^(N\d+) .*?\n\s+may be the same as: (T\d+)", messages[1]["content"], re.M)
    return {"decisions": [{"item": n, "same_as": t} for n, t in items]}


async def test_llm_links_ambiguous_claims_in_one_batched_call() -> None:
    grouping = group_mentions(_line4_and_pune())
    llm = FakeLLMProvider(responder=_link_all)
    budget = LLMBudget(5)
    links = await resolve_questions(grouping, llm, budget, model="m", batch_size=10)
    assert links == 1 and len(llm.calls) == 1 and budget.calls == 1
    assert len(grouping.groups.members()) == 1
    assert "Line 4" in llm.calls[0].user and "Pune" in llm.calls[0].user


async def test_llm_answer_outside_the_candidates_is_ignored() -> None:
    grouping = group_mentions(_line4_and_pune())
    llm = FakeLLMProvider([{"decisions": [{"item": "N1", "same_as": "T9"}]}])
    assert await resolve_questions(grouping, llm, LLMBudget(5), model="m", batch_size=10) == 0
    assert len(grouping.groups.members()) == 2


async def test_no_budget_means_no_llm_call_and_separate_threads() -> None:
    grouping = group_mentions(_line4_and_pune())
    llm = FakeLLMProvider(responder=_link_all)
    assert await resolve_questions(grouping, llm, LLMBudget(0), model="m", batch_size=10) == 0
    assert llm.calls == []
    assert len(grouping.groups.members()) == 2


# ------------------------------------------------------------------ dropped


def test_dropped_detection() -> None:
    recent = [ordinal("Q4FY26"), ordinal("Q3FY26")]
    as_of = ordinal("Q4FY26")
    old = [mention("Q1FY26", target=(2027, None)), mention("Q2FY26", target=(2027, None))]
    assert is_dropped(old, recent, as_of)
    # Still mentioned on one of the last two calls.
    assert not is_dropped([*old, mention("Q3FY26", target=(2027, None))], recent, as_of)
    # Target period already over: verified on results instead.
    assert not is_dropped([mention("Q1FY26", target=(2026, None))], recent, as_of)
    # No target period: never dropped (unverifiable instead).
    assert not is_dropped([mention("Q1FY26", target=None)], recent, as_of)
    # Configurable window: absent from only the last call.
    assert is_dropped([mention("Q3FY26", target=(2027, None))], recent[:1], as_of)


# ------------------------------------------------------------------- titles


def test_titles() -> None:
    assert (
        thread_title([mention(low="19", high="20", unit="percent")])
        == "EBITDA margin 19–20% in FY27"
    )
    assert (
        thread_title([mention(metric="net_debt", low="200", unit="inr_crore", target=(2027, None))])
        == "Net debt ₹200 crore by FY27"
    )
    assert (
        thread_title(
            [
                mention(
                    metric="capacity_utilization", direction=Direction.INCREASE, target=(2026, None)
                )
            ]
        )
        == "Capacity utilization to increase in FY26"
    )
    assert (
        thread_title(
            [
                mention(
                    metric="plant_commissioning",
                    metric_raw="Line 4 commissioning",
                    claim_type=ClaimType.QUALITATIVE,
                    target=(2027, 1),
                )
            ]
        )
        == "Line 4 commissioning by Q1FY27"
    )


# -------------------------------------------------------------- persistence


async def test_link_company_persists_threads_and_keeps_ids(app: FastAPI) -> None:
    from scripts.seed_demo import CALLS
    from tests.verification_factories import seed_company

    sessionmaker = app.state.sessionmaker
    company_id = await seed_company(sessionmaker, CALLS, symbol="LINKCO")
    llm = FakeLLMProvider(responder=_link_all)

    async with sessionmaker() as session:
        stats = await link_company(
            session, company_id, llm, LLMBudget(5), model="m", batch_size=15, dropped_after_calls=2
        )
    assert stats.threads_total == 10 and stats.threads_created == 10
    assert stats.questions == 1 and stats.llm_links == 1

    async with sessionmaker() as session:
        threads = {t.id: t for t in await session.scalars(select(ClaimThread))}
        unlinked = await session.scalar(select(func.count()).where(Claim.thread_id.is_(None)))
        plant = next(t for t in threads.values() if t.metric_normalized == "plant_commissioning")
        kinds = [
            c.change_type
            for c in await session.scalars(
                select(Claim)
                .where(Claim.thread_id == plant.id)
                .order_by(Claim.target_fiscal_year, Claim.target_fiscal_quarter, Claim.created_at)
            )
        ]
        dropped = [t.title for t in threads.values() if t.is_dropped]
    assert unlinked == 0
    assert plant.mention_count == 4 and plant.first_seen_period == "Q1FY26"
    assert plant.last_seen_period == "Q4FY26"
    assert sorted(kinds, key=str) == sorted([C.NEW, C.DELAYED, C.DELAYED, C.REITERATED], key=str)
    assert dropped == ["Net debt ₹200 crore by FY27"]

    # A second run reuses every thread.
    async with sessionmaker() as session:
        again = await link_company(
            session, company_id, llm, LLMBudget(5), model="m", batch_size=15, dropped_after_calls=2
        )
    async with sessionmaker() as session:
        ids = set(await session.scalars(select(ClaimThread.id)))
    assert again.threads_created == 0 and again.threads_removed == 0
    assert ids == set(threads)
