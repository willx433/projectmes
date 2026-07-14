"""JobBOSS2 API client (P1-03).

Contract sources (findings override the DD where they differ — see
docs/jb2-api-findings.md): DD §4.1, docs/jb2-api-findings.md §1/§4/§5,
IMPLEMENTATION_PLAN.md P1-03, CR-010/CR-011 in docs/CHANGE_REQUESTS.md.

Key findings baked in here:
- Auth: POST {AuthBaseUrl}/oauth2/api-user/token, form-urlencoded body
  (JSON -> 415). Token TTL is a flat 3600s and the endpoint mints a fresh
  token every call (no server-side reuse) -> cache in-process only,
  refresh proactively at 3300s or reactively on 401.
- Base URL: {ApiBaseUrl}/api/v1/ prefix is required explicitly, an
  unprefixed call 404s.
- Unfiltered collection GETs 500 on JB2 -> every collection GET must carry
  a filter param or take=.
- revisedDate[null] is a known 500 on JB2 -> refuse it client-side.
- No documented rate limit -> adopt a ≥0.5s politeness floor + circuit
  breaker as the safety net (CR-011 lets individual calls override the
  default timeout for the two unfiltered/heavy display endpoints).
"""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

import httpx

from app.config import Config

logger = logging.getLogger("app.jb2.client")

# Module-level JB2 call status (P1-10 admin health page), updated by every
# Jb2Client instance's _call() -- the same place that already logs each
# call.
#
# ponytail: process-local only. The sync worker and outbox drainer run as
# separate systemd units (mes-sync, mes-outbox) from the API process serving
# /health/jb2, so this only reflects calls made by a Jb2Client living in
# *this* process. Fine for tests and any same-process client; upgrade to a
# small persisted heartbeat row if the API process itself never calls JB2
# and cross-process accuracy is needed post-P1-11.
_status_lock = threading.Lock()
_status: dict[str, Any] = {
    "breaker_state": "closed",
    "last_success_at": None,
    "last_failure_at": None,
}


def _record_call_status(success: bool, breaker_state: str) -> None:
    with _status_lock:
        _status["breaker_state"] = breaker_state
        if success:
            _status["last_success_at"] = datetime.now(timezone.utc)
        else:
            _status["last_failure_at"] = datetime.now(timezone.utc)


def get_jb2_status() -> dict[str, Any]:
    """Snapshot of the module-level JB2 call status. See ponytail note above."""
    with _status_lock:
        return dict(_status)

DEFAULT_TIMEOUT_S = 10.0
TOKEN_REFRESH_MARGIN_S = 3300  # findings §1: TTL is 3600s, refresh with margin
MIN_REQUEST_INTERVAL_S = 0.5  # findings §5: politeness floor, no evidence for stricter
MAX_RETRIES = 2  # on 5xx/network only; total attempts = MAX_RETRIES + 1
BACKOFF_BASE_S = 0.5
BREAKER_FAILURE_THRESHOLD = 5
BREAKER_RESET_S = 60.0

_NON_FILTER_PARAMS = {"take", "skip", "sort", "fields"}


class Jb2Error(Exception):
    """Base for all jb2 client errors."""


class UnfilteredReadError(Jb2Error):
    """Raised when a collection GET has neither a filter param nor take=."""


class Jb2Unavailable(Jb2Error):
    """Raised immediately when the circuit breaker is open."""


class Jb2PermanentError(Jb2Error):
    """A 4xx response — caller/contract error, not JB2 unavailability.

    Never retried by this client, and callers (e.g. the outbox drainer,
    P1-09) should treat it as permanent: park, don't retry with backoff.
    """


class _Throttle:
    """Module-level politeness floor between calls, thread-safe.

    ponytail: a lock + last-call timestamp is the whole rate limiter — no
    token bucket needed for a single polite floor (findings §5 found no
    evidence for anything fancier). Upgrade if JB2 ever documents a real
    limit.
    """

    def __init__(self, min_interval: float, clock: Callable[[], float],
                 sleeper: Callable[[float], None]) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._last_call = float("-inf")

    def wait(self) -> None:
        with self._lock:
            elapsed = self._clock() - self._last_call
            if elapsed < self._min_interval:
                self._sleeper(self._min_interval - elapsed)
            self._last_call = self._clock()


class CircuitBreaker:
    """N consecutive failures -> open; half-open after a reset window; close on success."""

    def __init__(self, threshold: int = BREAKER_FAILURE_THRESHOLD,
                 reset_after: float = BREAKER_RESET_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._threshold = threshold
        self._reset_after = reset_after
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if self._clock() - self._opened_at >= self._reset_after:
                return "half-open"
            return "open"

    def before_call(self) -> None:
        if self.state == "open":
            raise Jb2Unavailable("JB2 circuit breaker open")

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._opened_at = self._clock()


class Jb2Client:
    """Sync httpx client for the JobBOSS2 API (workers are sync processes)."""

    def __init__(
        self,
        api_base_url: str,
        auth_base_url: str,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        on_call: Callable[[bool], None] | None = None,
    ) -> None:
        self._api_base = api_base_url.rstrip("/") + "/api/v1"
        self._auth_base = auth_base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._default_timeout = timeout
        self._clock = clock
        self._sleeper = sleeper
        self._httpx = httpx.Client(transport=transport)
        self._throttle = _Throttle(MIN_REQUEST_INTERVAL_S, clock, sleeper)
        self.breaker = CircuitBreaker(clock=clock)
        self._token: str | None = None
        self._token_obtained_at: float = float("-inf")
        # Optional extra hook (P1-10): called with True/False after every
        # breaker-relevant success/failure, on top of the always-on
        # module-level status tracking (get_jb2_status()).
        self.on_call = on_call

    def _notify_call(self, *, success: bool) -> None:
        _record_call_status(success, self.breaker.state)
        if self.on_call is not None:
            self.on_call(success)

    @classmethod
    def from_config(cls, config: Config, **kwargs: Any) -> "Jb2Client":
        config.validate([
            "jobboss2_api_base_url", "jobboss2_auth_base_url",
            "jobboss2_client_id", "jobboss2_client_secret",
        ])
        return cls(
            config.jobboss2_api_base_url,
            config.jobboss2_auth_base_url,
            config.jobboss2_client_id,
            config.jobboss2_client_secret,
            **kwargs,
        )

    def close(self) -> None:
        self._httpx.close()

    # -- auth --------------------------------------------------------

    def _authenticate(self) -> None:
        resp = self._call(
            "POST",
            f"{self._auth_base}/oauth2/api-user/token",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
            },
            auth=False,
        )
        body = resp.json()
        self._token = body["access_token"]
        self._token_obtained_at = self._clock()

    def _ensure_token(self) -> str:
        stale = (self._clock() - self._token_obtained_at) >= TOKEN_REFRESH_MARGIN_S
        if self._token is None or stale:
            self._authenticate()
        return self._token

    # -- public GET ----------------------------------------------------

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        take: int | None = None,
        timeout: float | None = None,
        unwrap: bool = True,
    ) -> Any:
        """GET a JB2 resource. Unwraps the `{"Data": [...]}` envelope transparently.

        Guards (findings-grounded): raises UnfilteredReadError unless the query
        carries a filter param or take=; raises Jb2Error on revisedDate[null]
        (known 500 on JB2, never send it).

        ``unwrap=False`` returns the raw response body untouched -- needed for
        `eci-aps/get-schedule` (CR-011, findings §1), whose envelope is
        `{StartDateProject, EndDateProject, Data: [...]}`, not the plain
        `{Data: [...]}` convention every other resource uses.
        """
        query = dict(params or {})
        if take is not None:
            query["take"] = take
        if fields:
            query["fields"] = ",".join(fields)

        if "revisedDate[null]" in query:
            raise Jb2Error("revisedDate[null] is a known 500 on JB2 — never send it")

        has_filter = any(k not in _NON_FILTER_PARAMS for k in query)
        if "take" not in query and not has_filter:
            raise UnfilteredReadError(
                f"GET {path} needs a filter param or take= — unfiltered collection reads 500 on JB2"
            )

        url = f"{self._api_base}{path if path.startswith('/') else '/' + path}"
        resp = self._call("GET", url, params=query, timeout=timeout)
        body = resp.json()
        if unwrap and isinstance(body, dict) and "Data" in body:
            return body["Data"]
        return body

    # -- public POST (P1-09 outbox writes) ------------------------------

    def post(self, path: str, json_body: dict[str, Any], *, timeout: float | None = None) -> Any:
        """POST a JB2 write (time-tickets, time-ticket-details, ...).

        Raises Jb2PermanentError on 4xx (never retried — caller should park),
        Jb2Error/Jb2Unavailable on exhausted 5xx retries or breaker-open
        (caller should retry later).
        """
        url = f"{self._api_base}{path if path.startswith('/') else '/' + path}"
        resp = self._call("POST", url, json=json_body, timeout=timeout)
        body = resp.json()
        if isinstance(body, dict) and "Data" in body:
            return body["Data"]
        return body

    def iter_pages(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        fields: list[str] | None = None,
        take: int = 200,
        timeout: float | None = None,
    ) -> Iterator[list[dict]]:
        """Yield successive pages (lists of rows) via skip/take until a short page."""
        skip = 0
        while True:
            page_params = dict(params or {})
            page_params["skip"] = skip
            rows = self.get(path, params=page_params, fields=fields, take=take, timeout=timeout)
            if not isinstance(rows, list):
                rows = [rows] if rows else []
            yield rows
            if len(rows) < take:
                return
            skip += take

    # -- request execution: throttle, retry/backoff, 401 reauth, breaker, logging --

    def _call(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
        auth: bool = True,
    ) -> httpx.Response:
        self.breaker.before_call()
        timeout = self._default_timeout if timeout is None else timeout
        attempt = 0
        reauthed = False
        while True:
            self._throttle.wait()
            headers = {"Authorization": f"Bearer {self._ensure_token()}"} if auth else {}
            start = self._clock()
            try:
                resp = self._httpx.request(
                    method, url, params=params, data=data, json=json,
                    headers=headers, timeout=timeout,
                )
            except httpx.HTTPError as exc:
                self._log(url, None, attempt, start)
                if attempt >= MAX_RETRIES:
                    self.breaker.record_failure()
                    self._notify_call(success=False)
                    raise Jb2Error(f"network error calling {method} {url}") from exc
                attempt += 1
                self._sleeper(self._backoff_delay(attempt))
                continue

            self._log(url, resp.status_code, attempt, start)

            if resp.status_code == 401 and auth and not reauthed:
                self._token = None
                reauthed = True
                continue  # once per request, no backoff, per findings §1

            if resp.status_code >= 500:
                if attempt >= MAX_RETRIES:
                    self.breaker.record_failure()
                    self._notify_call(success=False)
                    raise Jb2Error(f"JB2 {method} {url} failed with {resp.status_code}")
                attempt += 1
                self._sleeper(self._backoff_delay(attempt))
                continue

            if resp.status_code >= 400:
                # 4xx is a caller/contract error, not JB2 unavailability — never retried,
                # doesn't trip the breaker (breaker guards infra failures, §4.1/17.1).
                raise Jb2PermanentError(
                    f"JB2 {method} {url} failed with {resp.status_code}: {resp.text[:200]}"
                )

            self.breaker.record_success()
            self._notify_call(success=True)
            return resp

    def _backoff_delay(self, attempt: int) -> float:
        return BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, BACKOFF_BASE_S)

    def _log(self, path: str, status: int | None, attempt: int, start: float) -> None:
        latency_ms = (self._clock() - start) * 1000
        logger.info(
            "jb2_call",
            extra={"jb2_path": path, "jb2_status": status, "jb2_latency_ms": round(latency_ms, 1),
                   "jb2_attempt": attempt},
        )
