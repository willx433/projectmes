"""Unit tests for app/jb2/client.py — zero network, httpx.MockTransport only.

Covers: unfiltered-GET guard, fields kwarg, form-encoded auth (415-avoidance),
401 reauth, retry schedule on 5xx (no retry on 4xx), circuit breaker
open/half-open/close, throttle enforcement, envelope unwrap, revisedDate[null]
guard.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.jb2.client import (
    BREAKER_RESET_S,
    Jb2Client,
    Jb2Error,
    Jb2Unavailable,
    UnfilteredReadError,
)


class FakeClock:
    """Manually-advanced clock so breaker/retry timing is deterministic in tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


def recording_sleeper(calls: list[float]):
    def sleeper(seconds: float) -> None:
        calls.append(seconds)
    return sleeper


def make_client(handler, **kwargs) -> Jb2Client:
    transport = httpx.MockTransport(handler)
    kwargs.setdefault("clock", FakeClock())
    kwargs.setdefault("sleeper", lambda s: None)
    return Jb2Client(
        "https://api-jb2.example.com",
        "https://auth-jb2.example.com",
        "test-client-id",
        "test-client-secret",
        transport=transport,
        **kwargs,
    )


def auth_response() -> httpx.Response:
    return httpx.Response(
        200, json={"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600}
    )


# -- auth / 415-avoidance -----------------------------------------------------

def test_auth_body_is_form_urlencoded_not_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/api-user/token":
            seen["content_type"] = request.headers.get("content-type", "")
            seen["body"] = request.content.decode()
            return auth_response()
        return httpx.Response(200, json={"Data": []})

    client = make_client(handler)
    client.get("/orders", take=1)

    assert "application/x-www-form-urlencoded" in seen["content_type"]
    assert "application/json" not in seen["content_type"]
    # form-encoded body, not a JSON blob
    assert seen["body"] == (
        "client_id=test-client-id&client_secret=test-client-secret&grant_type=client_credentials"
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(seen["body"])


# -- guards -------------------------------------------------------------------

def test_unfiltered_get_raises():
    client = make_client(lambda req: httpx.Response(200, json={"Data": []}))
    with pytest.raises(UnfilteredReadError):
        client.get("/orders")


def test_get_with_take_is_allowed():
    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return auth_response()
        return httpx.Response(200, json={"Data": [{"orderNumber": 1}]})

    client = make_client(handler)
    result = client.get("/orders", take=5)
    assert result == [{"orderNumber": 1}]


def test_get_with_filter_param_is_allowed():
    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return auth_response()
        return httpx.Response(200, json={"Data": []})

    client = make_client(handler)
    client.get("/order-routings", params={"orderNumber[eq]": "10008"})  # no take needed


def test_fields_kwarg_lands_in_query():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return auth_response()
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"Data": []})

    client = make_client(handler)
    client.get("/order-line-items", take=5, fields=["jobNumber", "itemNumber", "lastModDate"])

    assert seen["query"]["fields"] == "jobNumber,itemNumber,lastModDate"


def test_revised_date_null_guard_raises():
    client = make_client(lambda req: httpx.Response(200, json={"Data": []}))
    with pytest.raises(Jb2Error):
        client.get("/orders", params={"revisedDate[null]": "true", "take": 5})


def test_base_url_has_api_v1_prefix():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return auth_response()
        seen["path"] = request.url.path
        return httpx.Response(200, json={"Data": []})

    client = make_client(handler)
    client.get("/orders", take=1)
    assert seen["path"] == "/api/v1/orders"


# -- envelope unwrap ------------------------------------------------------

def test_envelope_unwrap_data_list():
    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return auth_response()
        return httpx.Response(200, json={"Data": [{"a": 1}, {"a": 2}]})

    client = make_client(handler)
    assert client.get("/orders", take=5) == [{"a": 1}, {"a": 2}]


def test_non_envelope_body_passes_through():
    # eci-aps/get-schedule shape per findings §1: {StartDateProject, ..., Data: [...]}
    # still unwraps Data; but a body with no Data key passes through untouched.
    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return auth_response()
        return httpx.Response(200, json={"weird": "shape"})

    client = make_client(handler)
    assert client.get("/orders", take=5) == {"weird": "shape"}


# -- 401 reauth -----------------------------------------------------------

def test_token_refresh_on_401():
    tokens_issued = []
    auth_calls = 0
    get_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls, get_calls
        if request.url.path == "/oauth2/api-user/token":
            auth_calls += 1
            token = f"tok-{auth_calls}"
            tokens_issued.append(token)
            return httpx.Response(
                200, json={"access_token": token, "token_type": "Bearer", "expires_in": 3600}
            )
        get_calls += 1
        used_token = request.headers["authorization"].removeprefix("Bearer ")
        if used_token == "tok-1":
            return httpx.Response(401, json={"Title": "Unauthorized"})
        return httpx.Response(200, json={"Data": [{"ok": True}]})

    client = make_client(handler)
    result = client.get("/orders", take=1)

    assert result == [{"ok": True}]
    assert auth_calls == 2  # initial + reactive reauth
    assert get_calls == 2  # first 401, then succeeds with fresh token


def test_401_reauth_happens_once_per_request_then_gives_up():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/api-user/token":
            return auth_response()
        return httpx.Response(401, json={"Title": "Unauthorized"})

    client = make_client(handler)
    with pytest.raises(Jb2Error):
        client.get("/orders", take=1)


# -- retry schedule ---------------------------------------------------------

def test_retry_schedule_on_500_three_total_attempts():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return auth_response()
        attempts.append(1)
        return httpx.Response(500, json={"Title": "Server Error"})

    client = make_client(handler)
    with pytest.raises(Jb2Error):
        client.get("/orders", take=1)

    assert len(attempts) == 3  # initial + 2 retries (MAX_RETRIES=2)


def test_retry_eventually_succeeds():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return auth_response()
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(500)
        return httpx.Response(200, json={"Data": [{"ok": True}]})

    client = make_client(handler)
    result = client.get("/orders", take=1)
    assert result == [{"ok": True}]
    assert len(attempts) == 3


def test_no_retry_on_400():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return auth_response()
        attempts.append(1)
        return httpx.Response(400, json={"Title": "Bad Request"})

    client = make_client(handler)
    with pytest.raises(Jb2Error):
        client.get("/orders", take=1)

    assert len(attempts) == 1  # never retried


def test_no_retry_on_400_never_trips_breaker():
    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return auth_response()
        return httpx.Response(400, json={"Title": "Bad Request"})

    client = make_client(handler)
    for _ in range(10):
        with pytest.raises(Jb2Error):
            client.get("/orders", take=1)
    assert client.breaker.state == "closed"


# -- circuit breaker ----------------------------------------------------------

def test_breaker_opens_after_5_failures_then_half_opens_then_closes():
    fail = {"on": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return auth_response()
        if fail["on"]:
            return httpx.Response(500)
        return httpx.Response(200, json={"Data": [{"ok": True}]})

    clock = FakeClock()
    client = make_client(handler, clock=clock)

    for _ in range(5):
        with pytest.raises(Jb2Error):
            client.get("/orders", take=1)

    assert client.breaker.state == "open"
    with pytest.raises(Jb2Unavailable):
        client.get("/orders", take=1)

    clock.advance(BREAKER_RESET_S + 1)
    assert client.breaker.state == "half-open"

    fail["on"] = False
    result = client.get("/orders", take=1)  # trial call succeeds
    assert result == [{"ok": True}]
    assert client.breaker.state == "closed"


# -- throttle -------------------------------------------------------------

def test_throttle_enforced_between_calls():
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return auth_response()
        return httpx.Response(200, json={"Data": []})

    clock = FakeClock()
    client = make_client(handler, clock=clock, sleeper=recording_sleeper(sleeps))

    client.get("/orders", take=1)
    client.get("/orders", take=1)
    client.get("/orders", take=1)

    # clock never advances on its own -> every call after the first sees
    # elapsed=0 and must wait the full politeness floor.
    assert sleeps.count(0.5) >= 2


# -- paging helper ----------------------------------------------------------

def test_iter_pages_stops_on_short_page():
    pages_served = [
        [{"i": 1}, {"i": 2}],
        [{"i": 3}],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return auth_response()
        page = pages_served.pop(0)
        return httpx.Response(200, json={"Data": page})

    client = make_client(handler)
    pages = list(client.iter_pages("/reason-codes", take=2))
    assert pages == [[{"i": 1}, {"i": 2}], [{"i": 3}]]
