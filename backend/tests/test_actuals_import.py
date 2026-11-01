"""Actuals CSV import (validation, upsert) and GET /companies/{id}/actuals."""

from pathlib import Path

import httpx

from app.core.config import BACKEND_DIR
from app.models import Company
from app.services.actuals_import import COLUMNS, OPTIONAL_COLUMNS, parse_actuals_csv
from tests.factories import create_company
from tests.verification_factories import csv_rows

COMPANY = Company(name="Acme", nse_symbol="ACME")


def test_template_is_valid() -> None:
    template = (BACKEND_DIR / "templates" / "actuals_template.csv").read_bytes()
    header = template.decode().splitlines()[0]
    assert header == ",".join((*COLUMNS, *OPTIONAL_COLUMNS))
    rows, errors = parse_actuals_csv(template, Company(name="x", nse_symbol="EXAMPLE"))
    assert errors == [] and len(rows) == 5
    assert "ebit_margin" in {row.metric for row in rows}
    assert [row.scope for row in rows] == ["company"] * 4 + ["services"]


def test_every_bad_row_is_reported_with_its_row_number() -> None:
    content = csv_rows(
        "ACME",
        "revenue,2026,1,1100,inr_crore,Q1 results,2",  # row 2: fine
        "revenu,2026,1,1100,inr_crore,,",  # row 3: bad metric
        "revenue,FY2x,5,abc,crores,,0",  # row 4: bad year, quarter, value, unit, page
        "ebitda_margin,2026,,18.5,inr_crore,,",  # row 5: unit not valid for the metric
        "revenue,2026,1,1200,inr_crore,,",  # row 6: duplicate of row 2
    ).replace("ACME,ebitda", "OTHER,ebitda")  # row 5: wrong company too
    rows, errors = parse_actuals_csv(content.encode(), COMPANY)
    by_row: dict[int, set[str | None]] = {}
    for error in errors:
        by_row.setdefault(error.row, set()).add(error.field)
    assert by_row == {
        3: {"metric_normalized"},
        4: {"fiscal_year", "fiscal_quarter", "value", "unit", "source_page"},
        5: {"nse_symbol", "unit"},
        6: {None},
    }
    assert [r.row for r in rows] == [2]


def test_header_problems() -> None:
    _, errors = parse_actuals_csv(b"nse_symbol,metric,value\nACME,revenue,1\n", COMPANY)
    assert len(errors) == 1 and errors[0].row == 1
    assert (
        "missing column(s)" in errors[0].message
        and "unknown column(s): metric" in errors[0].message
    )
    _, errors = parse_actuals_csv((",".join(COLUMNS) + "\n").encode(), COMPANY)
    assert errors[0].message == "The file has no data rows."


def test_accepted_formats() -> None:
    content = csv_rows(
        "acme",
        'Revenue,FY26,Q2,"1,150.5",INR_CRORE,,',
        "pat,26,,410,inr_crore,,",
    )
    rows, errors = parse_actuals_csv(content.encode(), COMPANY)
    assert errors == []
    assert [(r.metric, r.fiscal_year, r.fiscal_quarter, str(r.value)) for r in rows] == [
        ("revenue", 2026, 2, "1150.5"),
        ("pat", 2026, None, "410"),
    ]


async def _upload(client: httpx.AsyncClient, company_id: str, content: str) -> httpx.Response:
    return await client.post(
        f"/companies/{company_id}/actuals/import",
        files={"file": ("actuals.csv", content.encode(), "text/csv")},
    )


async def test_import_endpoint_upserts(client: httpx.AsyncClient) -> None:
    company = await create_company(client)
    first = csv_rows(
        "ACME",
        "revenue,2026,1,1100,inr_crore,Q1,2",
        "revenue,2026,2,1150,inr_crore,Q2,2",
        "revenue,2026,3,1170,inr_crore,Q3,2",
        "revenue,2026,4,1200,inr_crore,Q4,2",
        "ebitda,2026,,856,inr_crore,FY26,4",
    )
    response = await _upload(client, company["id"], first)
    assert response.status_code == 200, response.text
    assert response.json() | {"company_id": None} == {
        "company_id": None,
        "rows_total": 5,
        "created": 5,
        "updated": 0,
        "errors": [],
    }

    corrected = csv_rows("ACME", "revenue,2026,4,1210,inr_crore,Q4 restated,3")
    response = await _upload(client, company["id"], corrected)
    assert response.json()["created"] == 0 and response.json()["updated"] == 1

    listing = (await client.get(f"/companies/{company['id']}/actuals")).json()
    reported = [a for a in listing["actuals"] if not a["derived"]]
    assert listing["reported"] == 5 == len(reported)
    q4 = next(a for a in reported if a["fiscal_quarter"] == 4)
    assert q4["value"] == "1210" and q4["source_note"] == "Q4 restated" and q4["source_page"] == 3
    derived = {
        (a["metric_normalized"], a["period_label"]): a for a in listing["actuals"] if a["derived"]
    }
    assert derived[("revenue", "FY26")]["value"] == "4630"
    assert derived[("revenue", "FY26")]["id"] is None
    assert "Sum of Q1–Q4" in derived[("revenue", "FY26")]["derived_from"]
    assert ("ebitda_margin", "FY26") in derived

    filtered = await client.get(
        f"/companies/{company['id']}/actuals",
        params={"metric": "revenue", "period_from": "Q3FY26", "period_to": "Q4FY26"},
    )
    labels = sorted(a["period_label"] for a in filtered.json()["actuals"])
    assert labels == ["FY26", "Q3FY26", "Q4FY26"]  # FY26 ends in Q4FY26


async def test_import_endpoint_rejects_the_whole_file(client: httpx.AsyncClient) -> None:
    company = await create_company(client)
    content = csv_rows("ACME", "revenue,2026,1,1100,inr_crore,,", "revenue,2026,9,1,inr_crore,,")
    response = await _upload(client, company["id"], content)
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "invalid_csv"
    assert body["details"] == [
        {"row": 3, "field": "fiscal_quarter", "message": body["details"][0]["message"]}
    ]
    listing = (await client.get(f"/companies/{company['id']}/actuals")).json()
    assert listing["actuals"] == []  # nothing imported


async def test_actuals_period_validation(client: httpx.AsyncClient) -> None:
    company = await create_company(client)
    response = await client.get(
        f"/companies/{company['id']}/actuals",
        params={"period_from": "Q4FY26", "period_to": "Q1FY26"},
    )
    assert response.status_code == 422


def test_template_path_exists() -> None:
    assert Path(BACKEND_DIR / "templates" / "actuals_template.csv").is_file()
