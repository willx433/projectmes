"""Badge + box-label print sheets (P2-12) — DD §14, §8 (reuses the WeasyPrint
generator, no separate rendering path).

`badge_sheet`: grid of operator badge cards, QR payload `OP:{uuid}` per DD
§14 ("badge QR scan, payload OP:{uuid}, random, revocable"). Badge uuids are
generated fresh per print — there's no real `operators` table yet (Phase 3),
so today's badge is a printable stand-in keyed to a jb2_employees mirror row
(see app/api/workorders.py's /admin/print/badges for the caveat that
reprinting mints a new uuid until Phase 3 persists one per operator).

`box_label_sheet`: grid of box labels, QR payload `BOX:{label}` per DD §6.1
(lead prints a new box QR at kit-up).
"""
from __future__ import annotations

import base64
import io

import segno
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

from app.config import REPO_ROOT

_env = Environment(loader=FileSystemLoader(str(REPO_ROOT / "templates")), autoescape=True)


def _qr_data_uri(payload: str) -> str:
    buf = io.BytesIO()
    segno.make(payload, error="m").save(buf, kind="svg", xmldecl=False, svgns=True)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def badge_sheet(operators: list[dict]) -> bytes:
    """`operators`: list of {"name": str, "badge_uuid": str}. Card QR payload
    is `OP:{badge_uuid}` (DD §14)."""
    cards = [
        {
            "name": op["name"],
            "qr": _qr_data_uri(f"OP:{op['badge_uuid']}"),
            "payload": f"OP:{op['badge_uuid']}",
        }
        for op in operators
    ]
    html_str = _env.get_template("guide/badge_sheet.html").render(cards=cards)
    return HTML(string=html_str, base_url=str(REPO_ROOT / "templates")).write_pdf()


def box_label_sheet(labels: list[str]) -> bytes:
    """`labels`: box label strings. Card QR payload is `BOX:{label}` (DD §6.1)."""
    cards = [
        {"label": label, "qr": _qr_data_uri(f"BOX:{label}"), "payload": f"BOX:{label}"}
        for label in labels
    ]
    html_str = _env.get_template("guide/box_label_sheet.html").render(cards=cards)
    return HTML(string=html_str, base_url=str(REPO_ROOT / "templates")).write_pdf()
