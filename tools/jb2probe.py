#!/usr/bin/env python3
"""JobBOSS2 live API probe harness (Phase 0, P0-02/P0-03).

Reads credentials from the repo-root .env (simple line parser, no
python-dotenv dependency). Records every request/response pair to
tests/fixtures/jb2/<name>.json for the fake-JB2 server, scrubbed of
secrets.

Usage:
    python3 tools/jb2probe.py auth
    python3 tools/jb2probe.py spec
    python3 tools/jb2probe.py activation
    python3 tools/jb2probe.py lastmod
    python3 tools/jb2probe.py reason-codes
    python3 tools/jb2probe.py endpoint /orders --params take=1 status[eq]=open
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "jb2"
MIN_REQUEST_INTERVAL = 0.5  # seconds, politeness floor
TIMEOUT = 10.0
MAX_5XX_RETRIES = 2

# Response headers worth recording (rate-limit-ish, case-insensitive match).
RATE_LIMIT_HEADER_PREFIXES = ("x-ratelimit", "x-rate-limit", "retry-after")


def load_env(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE lines, '#' comments, no quoting."""
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


class JB2Probe:
    def __init__(self, env: dict[str, str]):
        self.api_base = env["JobBoss2__ApiBaseUrl"].rstrip("/")
        self.auth_base = env["JobBoss2__AuthBaseUrl"].rstrip("/")
        self.client_id = env["JobBoss2__ClientId"]
        self.client_secret = env["JobBoss2__ClientSecret"]
        self._secrets = {self.client_id, self.client_secret}
        self._token: str | None = None
        self._last_request_ts: float = 0.0
        self.client = httpx.Client(timeout=TIMEOUT)

    # -- politeness -------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_ts = time.monotonic()

    # -- auth ---------------------------------------------------------
    def authenticate(self) -> dict:
        self._throttle()
        resp = self.client.post(
            f"{self.auth_base}/oauth2/api-user/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        return body

    def _ensure_token(self) -> str:
        if self._token is None:
            self.authenticate()
        return self._token

    # -- generic request with retry/backoff on 5xx, re-auth on 401 -----
    def request(self, method: str, url: str, *, params: dict | None = None,
                headers: dict | None = None, retried_auth: bool = False) -> httpx.Response:
        attempt = 0
        while True:
            self._throttle()
            req_headers = dict(headers or {})
            req_headers["Authorization"] = f"Bearer {self._ensure_token()}"
            try:
                resp = self.client.request(method, url, params=params, headers=req_headers)
            except httpx.RequestError:
                if attempt >= MAX_5XX_RETRIES:
                    raise
                attempt += 1
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 401 and not retried_auth:
                self._token = None
                return self.request(method, url, params=params, headers=headers, retried_auth=True)

            if resp.status_code >= 500 and attempt < MAX_5XX_RETRIES:
                attempt += 1
                time.sleep(2 ** attempt)
                continue

            return resp

    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        return self.request("GET", f"{self.api_base}/api/v1{path}", params=params)

    # -- fixture recording ----------------------------------------------
    def _scrub(self, text: str) -> None:
        for secret in self._secrets:
            if secret and secret in text:
                msg = "SECRET LEAK detected in fixture output (client id/secret substring found)"
                raise RuntimeError(msg)
        if "Bearer " in text:
            raise RuntimeError("SECRET LEAK detected in fixture output ('Bearer ' token found)")

    def record(self, name: str, method: str, path: str, params: dict | None,
               resp: httpx.Response) -> Path:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        headers = {
            k: v for k, v in resp.headers.items()
            if k.lower().startswith(RATE_LIMIT_HEADER_PREFIXES)
        }
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        fixture = {
            "request": {"method": method, "path": path, "params": params or {}},
            "response": {"status": resp.status_code, "headers": headers, "body": body},
            "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        out_path = FIXTURE_DIR / f"{name}.json"
        text = json.dumps(fixture, indent=2, default=str)
        self._scrub(text)
        out_path.write_text(text + "\n")
        # self-check the file on disk too
        self._scrub(out_path.read_text())
        return out_path


def cmd_auth(probe: JB2Probe, args: argparse.Namespace) -> None:
    body1 = probe.authenticate()
    print(f"[1] token_type={body1.get('token_type')} expires_in={body1.get('expires_in')}")
    time.sleep(5)
    token1 = probe._token
    body2 = probe.authenticate()
    token2 = probe._token
    print(f"[2] token_type={body2.get('token_type')} expires_in={body2.get('expires_in')}")
    print(f"same token reused across calls 5s apart: {token1 == token2}")
    # record scrubbed summary only (never the token itself)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    summary = {
        "request": {"method": "POST", "path": "/oauth2/api-user/token", "params": {}},
        "response": {
            "status": 200,
            "headers": {},
            "body": {
                "token_type_1": body1.get("token_type"),
                "expires_in_1": body1.get("expires_in"),
                "token_type_2": body2.get("token_type"),
                "expires_in_2": body2.get("expires_in"),
                "token_reused_5s_apart": token1 == token2,
            },
        },
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out_path = FIXTURE_DIR / "auth.json"
    text = json.dumps(summary, indent=2)
    probe._scrub(text)
    out_path.write_text(text + "\n")
    probe._scrub(out_path.read_text())
    print(f"fixture: {out_path}")


def cmd_spec(probe: JB2Probe, args: argparse.Namespace) -> None:
    resp = probe.request("GET", f"{probe.api_base}/openapi.json")
    resp.raise_for_status()
    out_dir = REPO_ROOT / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "openapi-jb2-2026-07-14.json"
    out_path.write_text(resp.text)
    print(f"spec saved verbatim: {out_path} ({len(resp.text)} bytes)")


RESOURCES = [
    ("orders", "/orders", {"take": 1}),
    ("order-line-items", "/order-line-items", {"take": 1}),
    ("order-routings", "/order-routings", {"take": 1}),
    ("job-materials", "/job-materials", {"take": 1}),
    ("job-requirements", "/job-requirements", {"take": 1}),
    ("estimates", "/estimates", {"take": 1}),
    ("work-centers", "/work-centers", {"take": 1}),
    ("operation-codes", "/operation-codes", {"take": 1}),
    ("employees", "/employees", {"take": 1}),
    ("reason-codes", "/reason-codes", {"take": 1}),
    ("document-controls", "/document-controls", {"take": 1}),
    ("document-histories", "/document-histories", {"take": 1}),
    ("time-tickets", "/time-tickets", {"take": 1}),
    ("time-ticket-details", "/time-ticket-details", {"take": 1}),
    ("attendance-tickets", "/attendance-tickets", {"take": 1}),
    ("eci-aps-get-schedule", "/eci-aps/get-schedule", {"take": 1}),
    ("shopview-get-jobs", "/shopview/get-jobs", {"take": 1}),
    ("non-conformances", "/non-conformances", {"take": 1}),
]


def cmd_activation(probe: JB2Probe, args: argparse.Namespace) -> None:
    results = []
    for name, path, params in RESOURCES:
        resp = probe.get(path, params=params)
        fixture_path = probe.record(f"activation-{name}", "GET", path, params, resp)
        results.append((name, path, resp.status_code))
        print(f"{name:24s} {path:28s} -> {resp.status_code}  ({fixture_path.name})")
    return results


def _find_field(row: dict, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in row:
            return c
    return None


def cmd_lastmod(probe: JB2Probe, args: argparse.Namespace) -> None:
    # Recent-ish checkpoint, well in the past to guarantee hits.
    checkpoint = "2020-01-01T00:00:00Z"

    resp = probe.get("/orders", params={"lastModDate[gte]": checkpoint, "take": 5})
    probe.record("lastmod-orders-window", "GET", "/orders",
                  {"lastModDate[gte]": checkpoint, "take": 5}, resp)
    print(f"orders lastModDate[gte]={checkpoint} -> {resp.status_code}")

    order_number = None
    lastmod_value = None
    if resp.status_code == 200:
        rows = resp.json().get("Data", [])
        if rows:
            row = rows[0]
            field = _find_field(row, ["lastModDate", "lastModifiedDate", "lastModified"])
            lastmod_value = row.get(field) if field else None
            order_number = row.get("orderNumber") or row.get("jobNumber")
            print(
                f"sample record lastModDate field={field!r} value={lastmod_value!r} "
                f"orderNumber={order_number!r}"
            )

    if lastmod_value:
        resp2 = probe.get("/orders", params={"lastModDate[gte]": lastmod_value, "take": 5})
        probe.record("lastmod-orders-boundary", "GET", "/orders",
                      {"lastModDate[gte]": lastmod_value, "take": 5}, resp2)
        boundary_rows = resp2.json().get("Data", []) if resp2.status_code == 200 else []
        found = any(
            (r.get("orderNumber") or r.get("jobNumber")) == order_number
            for r in boundary_rows
        )
        print(
            f"boundary re-query exact value -> {resp2.status_code}, "
            f"record present: {found} (inclusive={found})"
        )

    resp3 = probe.get("/order-line-items", params={"lastModDate[gte]": checkpoint, "take": 5})
    probe.record("lastmod-order-line-items", "GET", "/order-line-items",
                 {"lastModDate[gte]": checkpoint, "take": 5}, resp3)
    print(f"order-line-items lastModDate[gte]={checkpoint} -> {resp3.status_code}")
    if resp3.status_code == 200:
        rows = resp3.json().get("Data", [])
        if rows:
            field = _find_field(rows[0], ["lastModDate", "lastModifiedDate", "lastModified"])
            value = rows[0].get(field) if field else None
            print(
                f"order-line-items sample lastModDate field={field!r} value={value!r}"
            )

    params = {"lastModDate[gte]": checkpoint, "take": 5}
    if order_number:
        params["orderNumber[eq]"] = order_number
    resp4 = probe.get("/order-routings", params=params)
    probe.record("lastmod-order-routings", "GET", "/order-routings", params, resp4)
    print(
        f"order-routings lastModDate[gte]={checkpoint} orderNumber={order_number} "
        f"-> {resp4.status_code}"
    )
    if resp4.status_code == 200:
        rows = resp4.json().get("Data", [])
        if rows:
            field = _find_field(rows[0], ["lastModDate", "lastModifiedDate", "lastModified"])
            value = rows[0].get(field) if field else None
            print(
                f"order-routings sample lastModDate field={field!r} value={value!r}"
            )


def cmd_reason_codes(probe: JB2Probe, args: argparse.Namespace) -> None:
    all_rows = []
    skip = 0
    take = 200
    for page in range(3):
        resp = probe.get("/reason-codes", params={"take": take, "skip": skip})
        probe.record(f"reason-codes-page{page}", "GET", "/reason-codes",
                      {"take": take, "skip": skip}, resp)
        if resp.status_code != 200:
            print(f"page {page}: status {resp.status_code}, stopping")
            break
        rows = resp.json().get("Data", [])
        print(f"page {page}: {len(rows)} rows")
        all_rows.extend(rows)
        if len(rows) < take:
            break
        skip += take

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = FIXTURE_DIR / "reason-codes.json"
    text = json.dumps({"Data": all_rows}, indent=2, default=str)
    probe._scrub(text)
    out_path.write_text(text + "\n")
    probe._scrub(out_path.read_text())
    print(f"total reason codes: {len(all_rows)} -> {out_path}")


def cmd_endpoint(probe: JB2Probe, args: argparse.Namespace) -> None:
    params = {}
    for kv in args.params or []:
        k, _, v = kv.partition("=")
        params[k] = v
    resp = probe.get(args.path, params=params)
    name = args.path.strip("/").replace("/", "-")
    fixture_path = probe.record(name, "GET", args.path, params, resp)
    print(f"{args.path} -> {resp.status_code}")
    is_json = resp.headers.get("content-type", "").startswith("application/json")
    body = resp.json() if is_json else resp.text
    output = json.dumps(body, indent=2)[:2000]
    print(output)
    print(f"fixture: {fixture_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth")
    sub.add_parser("spec")
    sub.add_parser("activation")
    sub.add_parser("lastmod")
    sub.add_parser("reason-codes")
    p_endpoint = sub.add_parser("endpoint")
    p_endpoint.add_argument("path")
    p_endpoint.add_argument("--params", nargs="*", default=[])

    args = parser.parse_args()
    env = load_env(REPO_ROOT / ".env")
    probe = JB2Probe(env)

    dispatch = {
        "auth": cmd_auth,
        "spec": cmd_spec,
        "activation": cmd_activation,
        "lastmod": cmd_lastmod,
        "reason-codes": cmd_reason_codes,
        "endpoint": cmd_endpoint,
    }
    dispatch[args.command](probe, args)


if __name__ == "__main__":
    main()
