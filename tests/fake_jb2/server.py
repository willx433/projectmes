"""Fake JobBOSS2 (JB2) server for offline tests.

Emulates just enough of the real JB2 API (see docs/jb2-api-findings.md and
documentation/MES_Design_Document.md N6/§4.1) to drive the MES's JB2 client and
sync worker in tests with zero network. Backed entirely by an in-memory
``state`` dict the caller seeds/inspects directly:

- ``state[resource]``          -> list[dict] of records for a GET /api/v1/{resource}
- ``state["shopview/get-jobs"]``     -> list[dict]
- ``state["eci-aps/get-schedule"]``  -> dict with StartDateProject/EndDateProject/Data
- ``state["_tokens"]``         -> {token: expiry_epoch_seconds}
- ``state["_clock"]``          -> callable returning epoch seconds (override to fudge time)
- ``state["received_writes"]`` -> list[dict], append-order log of every write call
- ``state["inject"]``          -> {(method, path_prefix): [canned_response, ...]}
                                   consumed in order, longest-prefix-match wins
- ``state["delay"]``           -> {path: seconds} artificial delay for shopview/eci-aps
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

RESOURCES = [
    "orders",
    "order-line-items",
    "order-routings",
    "job-materials",
    "job-requirements",
    "estimates",
    "work-centers",
    "operation-codes",
    "employees",
    "reason-codes",
    "document-controls",
    "document-histories",
    "time-tickets",
    "time-ticket-details",
]

# Resources that 500 on a truly unfiltered GET (no take, no filter) — the real
# JB2 bug (docs/jb2-api-findings.md §4). Small reference tables don't exhibit it.
LARGE_TABLES = {
    "orders",
    "order-line-items",
    "order-routings",
    "job-materials",
    "job-requirements",
    "estimates",
    "document-controls",
    "document-histories",
    "time-tickets",
    "time-ticket-details",
}

# order-line-items' default field set silently omits lastModDate unless the
# caller explicitly requests it via fields= (findings §4).
DEFAULT_EXCLUDE_FIELDS = {
    "order-line-items": {"lastModDate"},
}

ORDER_ROUTING_PATCH_ALLOWED = {
    "operationCode",
    "employeeCode",
    "estimatedStartDate",
    "estimatedEndDate",
    "workCenter",
}

_RESERVED_QUERY_PARAMS = {"take", "skip", "fields", "sort"}
_FILTER_KEY_RE = re.compile(r"^(?P<field>\w+)\[(?P<op>\w+)\]$")

_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
}


def _coerce(value: str) -> Any:
    """Best-effort type coercion of a raw query-string filter value."""
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value


def _comparable(value: Any) -> Any:
    """Make a record's raw value comparable against a coerced filter value."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value


def _problem(status: int, title: str, detail: str = "") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"Title": title, "Status": status, "Detail": detail, "TraceId": str(uuid4())},
    )


def seed_state() -> dict[str, Any]:
    """Fresh state dict: resources present (empty lists) plus reserved keys.

    Use ``load_fixtures`` (or the fixtures directly) to populate real shapes;
    reason-codes is the one resource callers usually want fully seeded since a
    real full dump is recorded — most tests seed the others directly.
    """
    state: dict[str, Any] = {resource: [] for resource in RESOURCES}
    state["shopview/get-jobs"] = []
    state["eci-aps/get-schedule"] = {"StartDateProject": "", "EndDateProject": "", "Data": []}
    state["_tokens"] = {}
    state["_clock"] = time.time
    state["_next_unique_id"] = 900000
    state["received_writes"] = []
    state["inject"] = {}
    state["delay"] = {}
    return state


def _next_unique_id(state: dict) -> int:
    state["_next_unique_id"] += 1
    return state["_next_unique_id"]


def _check_injection(state: dict, method: str, path: str) -> JSONResponse | None:
    inject = state.get("inject") or {}
    best_match: tuple[str, str] | None = None
    for key in inject:
        inj_method, prefix = key
        if inj_method.upper() != method.upper():
            continue
        if not path.startswith(prefix):
            continue
        if best_match is None or len(prefix) > len(best_match[1]):
            best_match = key
    if best_match is None:
        return None
    queue = inject[best_match]
    if not queue:
        return None
    canned = queue.pop(0)
    return JSONResponse(status_code=canned["status"], content=canned.get("body", {}))


def _clock(state: dict) -> float:
    return state.get("_clock", time.time)()


def _check_auth(request: Request, state: dict) -> JSONResponse | None:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return _problem(401, "Unauthorized", "Missing bearer token")
    token = auth[len("Bearer ") :]
    expiry = state.get("_tokens", {}).get(token)
    if expiry is None or _clock(state) >= expiry:
        return _problem(401, "Unauthorized", "Missing or expired bearer token")
    return None


def create_fake_jb2(state: dict[str, Any]) -> FastAPI:
    app = FastAPI()

    @app.post("/oauth2/api-user/token")
    async def token(request: Request):
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("application/x-www-form-urlencoded"):
            return _problem(415, "Unsupported Media Type", "Expected form-encoded body")
        access_token = uuid4().hex
        state.setdefault("_tokens", {})[access_token] = _clock(state) + 3600
        return JSONResponse(
            {"access_token": access_token, "token_type": "Bearer", "expires_in": 3600}
        )

    @app.get("/api/v1/shopview/get-jobs")
    async def shopview_get_jobs(request: Request):
        inj = _check_injection(state, "GET", request.url.path)
        if inj is not None:
            return inj
        unauth = _check_auth(request, state)
        if unauth is not None:
            return unauth
        delay = state.get("delay", {}).get("shopview/get-jobs")
        if delay:
            time.sleep(delay)
        return JSONResponse({"Data": state.get("shopview/get-jobs", [])})

    @app.get("/api/v1/eci-aps/get-schedule")
    async def eci_aps_get_schedule(request: Request):
        inj = _check_injection(state, "GET", request.url.path)
        if inj is not None:
            return inj
        unauth = _check_auth(request, state)
        if unauth is not None:
            return unauth
        delay = state.get("delay", {}).get("eci-aps/get-schedule")
        if delay:
            time.sleep(delay)
        return JSONResponse(state.get("eci-aps/get-schedule", {}))

    @app.get("/api/v1/{resource:path}")
    async def get_resource(resource: str, request: Request):
        inj = _check_injection(state, "GET", request.url.path)
        if inj is not None:
            return inj
        unauth = _check_auth(request, state)
        if unauth is not None:
            return unauth
        if resource not in RESOURCES:
            return _problem(404, "Not Found", f"Unknown resource {resource!r}")

        records = list(state.get(resource, []))
        query = request.query_params

        # Parse filters.
        filters: list[tuple[str, str, Any]] = []
        for key, raw_value in query.items():
            if key in _RESERVED_QUERY_PARAMS:
                continue
            m = _FILTER_KEY_RE.match(key)
            if m:
                field, op = m.group("field"), m.group("op")
            else:
                field, op = key, "eq"
            if op not in _OPS:
                return _problem(400, "Bad Request", f"Unsupported filter operator {op!r}")
            filters.append((field, op, _coerce(raw_value)))

        known_fields: set[str] | None = None
        if records:
            known_fields = set()
            for r in records:
                known_fields.update(r.keys())
        for field, _op, _value in filters:
            if known_fields is not None and field not in known_fields:
                return _problem(400, "Bad Request", f"Unknown filter field {field!r}")

        has_take = "take" in query
        if resource in LARGE_TABLES and not has_take and not filters:
            return _problem(500, "Internal Server Error", "Unfiltered GET not supported")

        # Apply filters.
        def matches(record: dict) -> bool:
            for field, op, value in filters:
                if field not in record:
                    return False
                try:
                    if not _OPS[op](_comparable(record[field]), value):
                        return False
                except TypeError:
                    return False
            return True

        records = [r for r in records if matches(r)]

        take = int(query.get("take", 200))
        skip = int(query.get("skip", 0))
        records = records[skip : skip + take]

        fields_param = query.get("fields")
        if fields_param:
            wanted = [f.strip() for f in fields_param.split(",") if f.strip()]
            records = [{f: r[f] for f in wanted if f in r} for r in records]
        else:
            exclude = DEFAULT_EXCLUDE_FIELDS.get(resource)
            if exclude:
                records = [{k: v for k, v in r.items() if k not in exclude} for r in records]

        return JSONResponse({"Data": records})

    @app.post("/api/v1/time-tickets")
    async def post_time_ticket(request: Request):
        inj = _check_injection(state, "POST", request.url.path)
        if inj is not None:
            return inj
        unauth = _check_auth(request, state)
        if unauth is not None:
            return unauth
        body = await request.json()
        # findings §2 item 2: timeStart/timeEnd are HH:MM clock strings, max
        # length 5 -- real JB2 400s "value for field timeStart exceeds
        # maximum length of 5" on a full ISO datetime. Mimic that here so a
        # regression back to ISO timestamps fails the test suite, not just
        # live JB2.
        for detail in body.get("timeTicketDetails") or []:
            for field in ("timeStart", "timeEnd"):
                value = detail.get(field)
                if value is not None and len(str(value)) > 5:
                    return _problem(
                        400, "Bad Request",
                        f"value for field {field} exceeds maximum length of 5",
                    )
        state.setdefault("received_writes", []).append(
            {"method": "POST", "path": "/time-tickets", "body": body}
        )
        created = {**body, "uniqueID": _next_unique_id(state)}
        state.setdefault("time-tickets", []).append(created)
        return JSONResponse(status_code=201, content=created)

    @app.post("/api/v1/time-ticket-details")
    async def post_time_ticket_detail(request: Request):
        inj = _check_injection(state, "POST", request.url.path)
        if inj is not None:
            return inj
        unauth = _check_auth(request, state)
        if unauth is not None:
            return unauth
        body = await request.json()
        # findings §2 item 1 (CR-018): real JB2 rejects a standalone detail
        # POST -- there is no header to attach it to, since header+detail
        # are only ever created together via the nested POST /time-tickets.
        # Mimic the real 400 instead of accepting.
        return _problem(
            400, "Bad Request",
            f"Cannot find Time Ticket for employeeCode {body.get('employeeCode')} "
            f"and date {body.get('ticketDate')}",
        )

    @app.patch("/api/v1/order-routings/{step_number}")
    async def patch_order_routing(step_number: str, request: Request):
        inj = _check_injection(state, "PATCH", request.url.path)
        if inj is not None:
            return inj
        unauth = _check_auth(request, state)
        if unauth is not None:
            return unauth
        body = await request.json()
        unknown = set(body.keys()) - ORDER_ROUTING_PATCH_ALLOWED
        if unknown:
            return _problem(400, "Bad Request", f"Unexpected properties: {sorted(unknown)}")

        state.setdefault("received_writes", []).append(
            {"method": "PATCH", "path": f"/order-routings/{step_number}", "body": body}
        )

        routings = state.setdefault("order-routings", [])
        match = None
        for r in routings:
            if str(r.get("stepNumber")) == str(step_number):
                match = r
                break
        if match is not None:
            match.update(body)
            match["lastModDate"] = datetime.fromtimestamp(
                _clock(state), tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            return JSONResponse(match)

        created = {"stepNumber": step_number, **body}
        return JSONResponse(created)

    return app
