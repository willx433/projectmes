"""Shared web deps: Jinja templates + per-request DB connection."""
from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import db

templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def render(request, name, **ctx):
    return templates.TemplateResponse(request, name, ctx)
