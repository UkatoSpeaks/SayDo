import uuid

import httpx

from tests.factories import create_company


async def test_create_and_get_company(client: httpx.AsyncClient) -> None:
    created = await create_company(client, nse_symbol=" m&m ", bse_code="500520", name=" Mahindra ")

    assert created["name"] == "Mahindra"
    assert created["nse_symbol"] == "M&M"
    assert created["bse_code"] == "500520"
    assert created["created_at"].endswith(("Z", "+00:00"))

    response = await client.get(f"/companies/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


async def test_identifiers_are_optional(client: httpx.AsyncClient) -> None:
    created = await create_company(client, nse_symbol=None, bse_code="", sector="  ")
    assert created["nse_symbol"] is None
    assert created["bse_code"] is None
    assert created["sector"] is None


async def test_list_companies_sorted_by_name(client: httpx.AsyncClient) -> None:
    await create_company(client, name="Zeta Ltd", nse_symbol="ZETA", bse_code="500002")
    await create_company(client, name="Alpha Ltd", nse_symbol="ALPHA", bse_code="500003")

    response = await client.get("/companies")
    assert response.status_code == 200
    assert [c["name"] for c in response.json()] == ["Alpha Ltd", "Zeta Ltd"]

    response = await client.get("/companies", params={"limit": 1, "offset": 1})
    assert [c["name"] for c in response.json()] == ["Zeta Ltd"]


async def test_get_missing_company_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/companies/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "company_not_found"


async def test_invalid_uuid_is_validation_error(client: httpx.AsyncClient) -> None:
    response = await client.get("/companies/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_duplicate_nse_symbol_conflict(client: httpx.AsyncClient) -> None:
    await create_company(client, nse_symbol="ACME", bse_code="500001")
    response = await client.post(
        "/companies", json={"name": "Other", "nse_symbol": "acme", "bse_code": "500009"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "company_exists"


async def test_duplicate_bse_code_conflict(client: httpx.AsyncClient) -> None:
    await create_company(client, nse_symbol="ACME", bse_code="500001")
    response = await client.post(
        "/companies", json={"name": "Other", "nse_symbol": "OTHER", "bse_code": "500001"}
    )
    assert response.status_code == 409


async def test_invalid_identifiers_rejected(client: httpx.AsyncClient) -> None:
    for payload in (
        {"name": "X", "bse_code": "12345"},
        {"name": "X", "nse_symbol": "BAD SYMBOL"},
        {"name": "   "},
    ):
        response = await client.post("/companies", json=payload)
        assert response.status_code == 422, payload
        error = response.json()["error"]
        assert error["code"] == "validation_error"
        assert error["message"]
        assert error["details"]
