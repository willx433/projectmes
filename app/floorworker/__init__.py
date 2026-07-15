"""Floor session-lifecycle worker (P3-08/09): idle auto-pause + auto-close
sweep. See `app/domain/sessions.run_idle_sweep` for the logic; this package
is just the worker-loop entry point (`python -m app.floorworker`), mirroring
`app/sync` and `app/outbox`'s own `__main__.py` pattern."""
