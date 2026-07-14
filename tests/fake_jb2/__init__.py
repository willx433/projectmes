"""Fake JobBOSS2 test server package. See server.py for behavior docs."""

import json
from pathlib import Path

from tests.fake_jb2.server import RESOURCES, create_fake_jb2, seed_state

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "jb2"

__all__ = ["create_fake_jb2", "seed_state", "seed_from_fixtures", "RESOURCES"]


def seed_from_fixtures() -> dict:
    """A fresh state dict with reason-codes fully seeded from the recorded
    fixture dump (tests/fixtures/jb2/reason-codes.json) — the one resource
    Phase 0 captured a complete real dataset for. Everything else starts
    empty; tests seed those resources directly per-case.
    """
    state = seed_state()
    reason_codes_path = _FIXTURES_DIR / "reason-codes.json"
    if reason_codes_path.exists():
        data = json.loads(reason_codes_path.read_text())
        state["reason-codes"] = data.get("Data", [])
    return state
