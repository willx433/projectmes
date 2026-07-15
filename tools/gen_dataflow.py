"""Introspects the running app to list the real routes + sync registry names
+ outbox sender kinds, so docs/dataflow.html's node/arrow set is verifiable
against code rather than hand-claimed (C-01, IMPLEMENTATION_PLAN.md §6).

Run: .venv/bin/python tools/gen_dataflow.py

ponytail: a print-and-read script, not a test -- C-01 asks for something a
human/reviewer runs and diffs against the diagram by eye, not a CI gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _walk(routes) -> list:
    """FastAPI >=0.139 wraps `include_router`-ed routes in a lazy
    `_IncludedRouter` (its own `APIRouter.routes` holds the real
    `APIRoute` objects) instead of flattening them into `app.routes`
    eagerly -- recurse through `.original_router.routes` to reach them."""
    out = []
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            out.extend(_walk(route.original_router.routes))
        else:
            out.append(route)
    return out


def list_routes() -> list[tuple[str, str, str]]:
    """(methods, path, endpoint module.qualname) for every route on the app,
    in registration order -- i.e. the app/main.py include_router order."""
    from app.main import app

    out = []
    for route in _walk(app.routes):
        path = getattr(route, "path", None)
        if path is None:  # e.g. the /static Mount -- no single endpoint fn
            continue
        methods = ",".join(sorted(getattr(route, "methods", None) or {"GET"}))
        endpoint = getattr(route, "endpoint", None)
        target = f"{endpoint.__module__}.{endpoint.__qualname__}" if endpoint else "?"
        out.append((methods, path, target))
    return out


def list_sync_registry() -> list[tuple[str, str, float]]:
    """(resource name, jb2 endpoint, cadence_s) for every mirror resource
    app/sync/worker.py polls."""
    from app.sync.worker import REGISTRY

    return [(r.name, r.endpoint, r.cadence_s) for r in REGISTRY]


def list_display_registry() -> list[tuple[str, str, float]]:
    from app.sync.display import DISPLAY_REGISTRY

    return [(r.name, r.endpoint, r.cadence_s) for r in DISPLAY_REGISTRY]


def list_outbox_senders() -> list[tuple[str, str]]:
    """(kind, sender function name) for every jb2_outbox kind
    app/outbox/drainer.py knows how to send."""
    from app.outbox.drainer import DEFAULT_SENDERS

    return [(kind, fn.__name__) for kind, fn in DEFAULT_SENDERS.items()]


def list_order_hooks() -> list[str]:
    """Callables subscribed to app.sync.worker's order-event hook point.
    Importing app.sync.hooks does NOT register anything by itself (by
    design -- the sync framework has zero import-time dependency on the
    domain layer, see hooks.py's docstring); only `app/sync/__main__.py`
    calls `hooks.register()`, at process startup. Call it here too so this
    report reflects what the real `python -m app.sync` process wires up."""
    from app.sync import hooks
    from app.sync.worker import _order_hooks

    hooks.register()
    return [f"{h.__module__}.{h.__qualname__}" for h in _order_hooks]


def main() -> None:
    print("=" * 70)
    print("HTTP ROUTES (app.main include_router order)")
    print("=" * 70)
    for methods, path, target in list_routes():
        print(f"{methods:8} {path:45} -> {target}")

    print()
    print("=" * 70)
    print("SYNC WORKER REGISTRY (app/sync/worker.py REGISTRY -- JB2 -> mirror)")
    print("=" * 70)
    for name, endpoint, cadence in list_sync_registry():
        print(f"{name:20} {endpoint:25} every {cadence:>6.0f}s")

    print()
    print("=" * 70)
    print("DISPLAY REGISTRY (app/sync/display.py DISPLAY_REGISTRY)")
    print("=" * 70)
    for name, endpoint, cadence in list_display_registry():
        print(f"{name:20} {endpoint:25} every {cadence:>6.0f}s")

    print()
    print("=" * 70)
    print("ORDER HOOKS (app.sync.worker.register_order_hook subscribers)")
    print("=" * 70)
    for hook in list_order_hooks():
        print(f"  {hook}")

    print()
    print("=" * 70)
    print("OUTBOX SENDER REGISTRY (app/outbox/drainer.py DEFAULT_SENDERS -- MES -> JB2)")
    print("=" * 70)
    for kind, fn in list_outbox_senders():
        print(f"{kind:25} -> {fn}")


if __name__ == "__main__":
    main()
