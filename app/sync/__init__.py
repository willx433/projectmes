"""Sync worker framework (P1-05, DD §4.2).

Polls JB2 collection endpoints on a per-resource cadence, diffs against the
local mirror by content hash, and records every cycle to sync_runs. See
worker.py (loop + registry), checkpoints.py (lastModDate bookmarks),
engine.py (generic upsert), runs.py (sync_runs recording).
"""
