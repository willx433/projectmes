import pytest
from httpx import ASGITransport, AsyncClient

BASE = "http://fake-jb2"


async def _authed_client(transport: ASGITransport, state: dict) -> AsyncClient:
    client = AsyncClient(transport=transport, base_url=BASE)
    resp = await client.post(
        "/oauth2/api-user/token",
        data={"client_id": "x", "client_secret": "y", "grant_type": "client_credentials"},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.mark.asyncio
async def test_auth_rejects_non_form_encoded(fake_jb2):
    transport, _state = fake_jb2
    client = AsyncClient(transport=transport, base_url=BASE)
    resp = await client.post("/oauth2/api-user/token", json={"client_id": "x"})
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_auth_issues_fresh_token(fake_jb2):
    transport, _state = fake_jb2
    client = AsyncClient(transport=transport, base_url=BASE)
    r1 = await client.post("/oauth2/api-user/token", data={"grant_type": "client_credentials"})
    r2 = await client.post("/oauth2/api-user/token", data={"grant_type": "client_credentials"})
    assert r1.json()["access_token"] != r2.json()["access_token"]
    assert r1.json()["expires_in"] == 3600


@pytest.mark.asyncio
async def test_missing_bearer_401(fake_jb2):
    transport, _state = fake_jb2
    client = AsyncClient(transport=transport, base_url=BASE)
    resp = await client.get("/api/v1/employees", params={"take": 1})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_expired_token_401(fake_jb2):
    transport, state = fake_jb2
    client = await _authed_client(transport, state)
    # Expire every issued token.
    for token in state["_tokens"]:
        state["_tokens"][token] = state["_clock"]() - 1
    resp = await client.get("/api/v1/employees", params={"take": 1})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unfiltered_get_on_large_table_500(fake_jb2):
    transport, state = fake_jb2
    state["orders"] = [{"orderNumber": "1", "lastModDate": "2024-01-01T00:00:00Z"}]
    client = await _authed_client(transport, state)
    resp = await client.get("/api/v1/orders")
    assert resp.status_code == 500
    assert resp.json()["Status"] == 500


@pytest.mark.asyncio
async def test_take_bypasses_unfiltered_500(fake_jb2):
    transport, state = fake_jb2
    state["orders"] = [{"orderNumber": "1", "lastModDate": "2024-01-01T00:00:00Z"}]
    client = await _authed_client(transport, state)
    resp = await client.get("/api/v1/orders", params={"take": 200})
    assert resp.status_code == 200
    assert resp.json()["Data"][0]["orderNumber"] == "1"


@pytest.mark.asyncio
async def test_paging_take_skip(fake_jb2):
    transport, state = fake_jb2
    state["employees"] = [{"employeeCode": i} for i in range(10)]
    client = await _authed_client(transport, state)
    resp = await client.get("/api/v1/employees", params={"take": 3, "skip": 4})
    data = resp.json()["Data"]
    assert [r["employeeCode"] for r in data] == [4, 5, 6]


@pytest.mark.asyncio
async def test_default_take_is_200(fake_jb2):
    transport, state = fake_jb2
    state["employees"] = [{"employeeCode": i} for i in range(250)]
    client = await _authed_client(transport, state)
    resp = await client.get("/api/v1/employees")
    assert len(resp.json()["Data"]) == 200


@pytest.mark.asyncio
async def test_filter_ops(fake_jb2):
    transport, state = fake_jb2
    state["employees"] = [{"employeeCode": i, "active": i % 2 == 0} for i in range(5)]
    client = await _authed_client(transport, state)

    resp = await client.get("/api/v1/employees", params={"employeeCode[gt]": 2, "take": 10})
    assert sorted(r["employeeCode"] for r in resp.json()["Data"]) == [3, 4]

    resp = await client.get("/api/v1/employees", params={"employeeCode[gte]": 3, "take": 10})
    assert sorted(r["employeeCode"] for r in resp.json()["Data"]) == [3, 4]

    resp = await client.get("/api/v1/employees", params={"employeeCode[lte]": 1, "take": 10})
    assert sorted(r["employeeCode"] for r in resp.json()["Data"]) == [0, 1]

    resp = await client.get("/api/v1/employees", params={"employeeCode[ne]": 0, "take": 10})
    assert 0 not in [r["employeeCode"] for r in resp.json()["Data"]]

    # Bare field = eq.
    resp = await client.get("/api/v1/employees", params={"employeeCode": 2, "take": 10})
    assert [r["employeeCode"] for r in resp.json()["Data"]] == [2]


@pytest.mark.asyncio
async def test_lastmoddate_gte_inclusive_boundary(fake_jb2):
    transport, state = fake_jb2
    state["orders"] = [
        {"orderNumber": "A", "lastModDate": "2023-10-25T14:30:13Z"},
        {"orderNumber": "B", "lastModDate": "2023-11-20T17:16:01Z"},
    ]
    client = await _authed_client(transport, state)
    resp = await client.get(
        "/api/v1/orders",
        params={"lastModDate[gte]": "2023-10-25T14:30:13Z", "take": 10},
    )
    numbers = {r["orderNumber"] for r in resp.json()["Data"]}
    assert numbers == {"A", "B"}, "gte must be inclusive at the exact boundary"


@pytest.mark.asyncio
async def test_unknown_filter_field_400(fake_jb2):
    transport, state = fake_jb2
    state["orders"] = [{"orderNumber": "1", "lastModDate": "2024-01-01T00:00:00Z"}]
    client = await _authed_client(transport, state)
    resp = await client.get("/api/v1/orders", params={"bogusField[eq]": "x", "take": 10})
    assert resp.status_code == 400
    assert resp.json()["Status"] == 400


@pytest.mark.asyncio
async def test_fields_projection_and_line_items_quirk(fake_jb2):
    transport, state = fake_jb2
    state["order-line-items"] = [
        {"jobNumber": "10008-01", "itemNumber": 1, "lastModDate": "2023-10-25T14:30:13Z"}
    ]
    client = await _authed_client(transport, state)

    # Default (no fields=) omits lastModDate for order-line-items.
    resp = await client.get("/api/v1/order-line-items", params={"take": 10})
    row = resp.json()["Data"][0]
    assert "lastModDate" not in row
    assert row == {"jobNumber": "10008-01", "itemNumber": 1}

    # Explicit fields= brings it back and projects only what was asked.
    resp = await client.get(
        "/api/v1/order-line-items",
        params={"take": 10, "fields": "jobNumber,lastModDate"},
    )
    row = resp.json()["Data"][0]
    assert row == {"jobNumber": "10008-01", "lastModDate": "2023-10-25T14:30:13Z"}


@pytest.mark.asyncio
async def test_write_recording_time_tickets(fake_jb2):
    transport, state = fake_jb2
    client = await _authed_client(transport, state)
    resp = await client.post(
        "/api/v1/time-tickets", json={"employeeCode": 100, "ticketDate": "2024-01-01T00:00:00Z"}
    )
    assert resp.status_code == 201
    assert "uniqueID" in resp.json()
    assert len(state["received_writes"]) == 1
    assert state["received_writes"][0]["path"] == "/time-tickets"
    assert state["received_writes"][0]["body"]["employeeCode"] == 100


@pytest.mark.asyncio
async def test_order_routing_patch_rejects_unknown_field(fake_jb2):
    transport, state = fake_jb2
    state["order-routings"] = [{"stepNumber": 10, "jobNumber": "10008-01"}]
    client = await _authed_client(transport, state)

    resp = await client.patch("/api/v1/order-routings/10", json={"actualPiecesGood": 3})
    assert resp.status_code == 400

    resp = await client.patch("/api/v1/order-routings/10", json={"operationCode": "OP2"})
    assert resp.status_code == 200
    assert resp.json()["operationCode"] == "OP2"
    assert state["order-routings"][0]["operationCode"] == "OP2"
    assert len(state["received_writes"]) == 1


@pytest.mark.asyncio
async def test_error_injection_consumed_in_order(fake_jb2):
    transport, state = fake_jb2
    state["employees"] = [{"employeeCode": 1}]
    state["inject"][("GET", "/api/v1/employees")] = [
        {"status": 500, "body": {"Title": "boom", "Status": 500}},
    ]
    client = await _authed_client(transport, state)

    resp = await client.get("/api/v1/employees", params={"take": 10})
    assert resp.status_code == 500

    # Injection queue exhausted -> falls through to normal behavior.
    resp = await client.get("/api/v1/employees", params={"take": 10})
    assert resp.status_code == 200
    assert resp.json()["Data"][0]["employeeCode"] == 1


@pytest.mark.asyncio
async def test_shopview_and_eci_aps_ignore_query_params(fake_jb2):
    transport, state = fake_jb2
    state["shopview/get-jobs"] = [{"jobNumber": "1"}, {"jobNumber": "2"}]
    state["eci-aps/get-schedule"] = {
        "StartDateProject": "07/23/2024 16:08",
        "EndDateProject": "04/27/2029 14:34",
        "Data": [{"TaskId": 1}],
    }
    client = await _authed_client(transport, state)

    resp = await client.get("/api/v1/shopview/get-jobs", params={"take": 1})
    assert len(resp.json()["Data"]) == 2

    resp = await client.get("/api/v1/eci-aps/get-schedule", params={"take": 1})
    body = resp.json()
    assert len(body["Data"]) == 1
    assert body["StartDateProject"] == "07/23/2024 16:08"


@pytest.mark.asyncio
async def test_reason_codes_seeded_from_fixture(fake_jb2):
    _transport, state = fake_jb2
    assert len(state["reason-codes"]) == 21
    assert any(r["reasonCode"] == "BURRS" for r in state["reason-codes"])
