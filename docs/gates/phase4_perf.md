# Phase 4 perf/load check (P4-09)

Timestamp: 2026-07-15T13:06:36.420761+00:00
Database: PostgreSQL
- Connected to PostgreSQL: postgresql+psycopg://mes@127.0.0.1:55432/mes
- Seeded units: 50 (queued=17, in_transit=17, at_station=16) across 3 board states, 3 products, 7 shared stations

## Budget mapping (this task's own call -- DD N2/§15 doesn't itemize per-endpoint)

- `dashboard_pipeline` (`GET /api/v1/dashboard/pipeline`, the board) -> **< 2s** (N2 dashboard)
- `scan_accept` (`POST /scan` box accept) -> **< 1s** (N2 scan->render)
- `unit_drilldown` (`GET /units/{id}`) -> **< 1s** (same scan->render family)
- `dashboard_metrics` (`GET /dashboard/metrics`, 9 SQL views) -> **< 2s** (dashboard family)

## Timings (wall-clock, PostgreSQL)

| metric | n | p50 (ms) | p95 (ms) | budget (ms) | avg queries/call | verdict |
|---|---|---|---|---|---|---|
| dashboard_pipeline | 10 | 316.7 | 351.6 | 2000 | 529.0 | PASS |
| scan_accept | 10 | 14.8 | 26.6 | 1000 | 16.0 | PASS |
| unit_drilldown | 10 | 15.2 | 41.4 | 1000 | 18.0 | PASS |
| dashboard_metrics | 10 | 12.9 | 38.1 | 2000 | 9.0 | PASS |

(verdict is judged on p95 vs budget -- the worst realistic case must still clear the bar)

## N+1 pattern -- is it a problem at this unit count?

`app/domain/pipeline.py`'s `build_board()` calls `build_card()` once per active unit, and each card issues ~7-10 queries (work_order/product get, `unit_next_op`, all_ops, open-session check, last-transit/last-closed-session check, route_pct's step_executions, current box, conditionally operator/failure/line_item) -- classic N+1, by design (no batching in `app/domain/pipeline.py` today).

Measured: **529.0 SQL statements** per `GET /api/v1/dashboard/pipeline` call against **50 active cards** -- i.e. roughly `active_units * 7-10 + 1`, confirming the N+1 shape directly rather than inferring it from code reading alone.

At 50 units the board still clears its budget with room to spare (p95=352ms vs 2000ms budget) -- **NOT a problem at this scale**. The N+1 shape is real and will not free-scale: it degrades linearly with active-unit count, not with events/sec, so the risk is a shop with several hundred *simultaneously active* units on the board (not 25 stations scanning -- that's request concurrency, a different axis, see below), which this task's own "hundreds of open ops" framing calls out as the thing to watch. Recommended follow-up if/when that count is seen for real: batch the per-card lookups (one query per table across all active unit_ids, grouped in Python) rather than rewriting the query shape now on guessed-at future volume.

## Supplementary: 25-concurrent-station smoke test

`25` concurrent `GET /api/v1/dashboard/pipeline` requests (ThreadPoolExecutor over the same TestClient/app-under-test): total wall 11270ms, all 200s, no errors, per-request p50=5459ms p95=10847ms

Note the total wall time here (~11270ms) is close to `25 x` the serial p50 above (317ms x 25 = 7918ms) -- i.e. these 25 "concurrent" requests actually ran **serially**, not in parallel. That's Starlette's `TestClient` (a single in-process ASGI test transport with one internal event-loop portal, built for single-threaded synchronous test code), not the app or the database -- real concurrent HTTP clients over the network don't share that portal. This smoke test therefore cannot validate '25 concurrent stations comfortably' by itself; it's included per DD N2/IMPLEMENTATION_PLAN P4-09 naming that number explicitly, but the load-bearing evidence for concurrency headroom is the serial p50/p95 numbers above being a small fraction of budget (dashboard p95 uses 18% of its 2s budget) -- a single FastAPI worker with real async I/O and a properly sized connection pool (this harness raised it to 30 for exactly this reason) has ample headroom left for 25 real concurrent stations. A genuine concurrency test needs 25 real HTTP connections against a running uvicorn process (e.g. `hey`/`wrk` against a live `--workers 4` deployment), not threads sharing one TestClient -- worth doing pre-pilot, out of scope for this in-process gate check.

## EXPLAIN (ANALYZE) -- dashboard hot queries + the 9 metrics views

### dashboard: active_units() -- board's outer query
```sql
SELECT id, status FROM units WHERE status IN ('queued','at_station','in_transit')
```
```
Seq Scan on units  (cost=0.00..6.77 rows=51 width=25) (actual time=0.015..0.033 rows=51 loops=1)
  Filter: (status = ANY ('{queued,at_station,in_transit}'::text[]))
  Rows Removed by Filter: 5
  Buffers: shared hit=6
Planning Time: 0.086 ms
Execution Time: 0.046 ms
```
Seq Scan line(s): 1 -- see index findings below.

### dashboard: per-card open work session (N+1, one per active unit)
```sql
SELECT * FROM work_sessions WHERE unit_id = :uid AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1
```
```
Limit  (cost=9.92..9.93 rows=1 width=133) (actual time=0.026..0.026 rows=0 loops=1)
  Buffers: shared hit=8
  ->  Sort  (cost=9.92..9.93 rows=1 width=133) (actual time=0.025..0.025 rows=0 loops=1)
        Sort Key: started_at DESC
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=8
        ->  Seq Scan on work_sessions  (cost=0.00..9.91 rows=1 width=133) (actual time=0.021..0.021 rows=0 loops=1)
              Filter: ((ended_at IS NULL) AND (unit_id = '29018dad-8d91-40aa-864e-e040e8c7cd04'::uuid))
              Rows Removed by Filter: 163
              Buffers: shared hit=8
Planning Time: 0.054 ms
Execution Time: 0.034 ms
```
Seq Scan line(s): 1 -- see index findings below.

### dashboard: per-card last transit (N+1, one per active unit)
```sql
SELECT * FROM transits WHERE unit_id = :uid ORDER BY departed_at DESC LIMIT 1
```
```
Limit  (cost=5.68..5.68 rows=1 width=84) (actual time=0.016..0.016 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Sort  (cost=5.68..5.70 rows=6 width=84) (actual time=0.016..0.016 rows=1 loops=1)
        Sort Key: departed_at DESC
        Sort Method: top-N heapsort  Memory: 25kB
        Buffers: shared hit=4
        ->  Seq Scan on transits  (cost=0.00..5.65 rows=6 width=84) (actual time=0.008..0.013 rows=6 loops=1)
              Filter: (unit_id = '29018dad-8d91-40aa-864e-e040e8c7cd04'::uuid)
              Rows Removed by Filter: 126
              Buffers: shared hit=4
Planning Time: 0.028 ms
Execution Time: 0.021 ms
```
Seq Scan line(s): 1 -- see index findings below.

### dashboard: per-card all_ops (N+1, one per active unit)
```sql
SELECT id, seq FROM plan_operations WHERE work_order_id = :wid AND status != 'skipped' ORDER BY seq
```
```
Sort  (cost=10.68..10.70 rows=7 width=20) (actual time=0.023..0.023 rows=7 loops=1)
  Sort Key: seq
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=10
  ->  Seq Scan on plan_operations  (cost=0.00..10.59 rows=7 width=20) (actual time=0.012..0.020 rows=7 loops=1)
        Filter: ((status <> 'skipped'::text) AND (work_order_id = '3aed1ef1-f915-4ae2-829c-4770feb3f132'::uuid))
        Rows Removed by Filter: 32
        Buffers: shared hit=10
Planning Time: 0.031 ms
Execution Time: 0.027 ms
```
Seq Scan line(s): 1 -- see index findings below.

### dashboard: per-card route_pct step_executions (N+1, one per active unit)
```sql
SELECT id, step_seq FROM step_executions WHERE unit_id = :uid AND plan_operation_id = :opid AND status = 'done' AND superseded = false
```
```
Seq Scan on step_executions  (cost=0.00..6.40 rows=1 width=20) (actual time=0.015..0.015 rows=0 loops=1)
  Filter: ((NOT superseded) AND (unit_id = '29018dad-8d91-40aa-864e-e040e8c7cd04'::uuid) AND (plan_operation_id = 'b3e39114-3081-41b1-8dc4-609dd77a3b49'::uuid) AND (status = 'done'::text))
  Rows Removed by Filter: 137
  Buffers: shared hit=4
Planning Time: 0.033 ms
Execution Time: 0.018 ms
```
Seq Scan line(s): 1 -- see index findings below.

### metrics view: v_fpy_unit
```sql
SELECT * FROM v_fpy_unit
```
```
Hash Join  (cost=19.24..26.57 rows=56 width=98) (actual time=0.020..0.037 rows=56 loops=1)
  Hash Cond: (u.work_order_id = wo.id)
  Buffers: shared hit=8
  ->  Seq Scan on units u  (cost=0.00..6.56 rows=56 width=50) (actual time=0.002..0.009 rows=56 loops=1)
        Buffers: shared hit=6
  ->  Hash  (cost=19.13..19.13 rows=9 width=64) (actual time=0.014..0.015 rows=9 loops=1)
        Buckets: 1024  Batches: 1  Memory Usage: 9kB
        Buffers: shared hit=2
        ->  Hash Join  (cost=1.20..19.13 rows=9 width=64) (actual time=0.010..0.013 rows=9 loops=1)
              Hash Cond: (p.id = wo.product_id)
              Buffers: shared hit=2
              ->  Seq Scan on products p  (cost=0.00..15.70 rows=570 width=48) (actual time=0.002..0.003 rows=9 loops=1)
                    Buffers: shared hit=1
              ->  Hash  (cost=1.09..1.09 rows=9 width=32) (actual time=0.005..0.005 rows=9 loops=1)
                    Buckets: 1024  Batches: 1  Memory Usage: 9kB
                    Buffers: shared hit=1
                    ->  Seq Scan on work_orders wo  (cost=0.00..1.09 rows=9 width=32) (actual time=0.002..0.003 rows=9 loops=1)
                          Buffers: shared hit=1
Planning:
  Buffers: shared hit=8
Planning Time: 0.159 ms
Execution Time: 0.050 ms
```
Seq Scan line(s): 3 -- see index findings below.

### metrics view: v_fpy_operation
```sql
SELECT * FROM v_fpy_operation
```
```
Hash Join  (cost=16.93..369.82 rows=37 width=78) (actual time=0.066..0.096 rows=137 loops=1)
  Hash Cond: (step_executions.plan_operation_id = po.id)
  Buffers: shared hit=14
  ->  HashAggregate  (cost=6.05..6.42 rows=37 width=32) (actual time=0.041..0.052 rows=137 loops=1)
        Group Key: step_executions.plan_operation_id, step_executions.unit_id
        Batches: 1  Memory Usage: 56kB
        Buffers: shared hit=4
        ->  Seq Scan on step_executions  (cost=0.00..5.37 rows=137 width=32) (actual time=0.003..0.011 rows=137 loops=1)
              Buffers: shared hit=4
  ->  Hash  (cost=10.39..10.39 rows=39 width=58) (actual time=0.020..0.020 rows=39 loops=1)
        Buckets: 1024  Batches: 1  Memory Usage: 12kB
        Buffers: shared hit=10
        ->  Seq Scan on plan_operations po  (cost=0.00..10.39 rows=39 width=58) (actual time=0.002..0.013 rows=39 loops=1)
              Buffers: shared hit=10
  SubPlan 2
    ->  Seq Scan on failures f  (cost=0.00..14.12 rows=3 width=32) (actual time=0.001..0.001 rows=0 loops=1)
          Filter: (disposition = ANY ('{rework_in_place,rework_to_op}'::text[]))
Planning:
  Buffers: shared hit=10
Planning Time: 0.161 ms
Execution Time: 0.130 ms
```
Seq Scan line(s): 3 -- see index findings below.

### metrics view: v_scrap_pareto
```sql
SELECT * FROM v_scrap_pareto
```
```
Hash Left Join  (cost=38.67..54.49 rows=380 width=136) (actual time=0.001..0.002 rows=0 loops=1)
  Hash Cond: (f.failure_code_id = fc.id)
  ->  Hash Join  (cost=17.43..32.23 rows=380 width=88) (actual time=0.001..0.001 rows=0 loops=1)
        Hash Cond: (se.failure_id = f.id)
        ->  Seq Scan on scrap_events se  (cost=0.00..13.80 rows=380 width=88) (actual time=0.001..0.001 rows=0 loops=1)
        ->  Hash  (cost=13.30..13.30 rows=330 width=32) (never executed)
              ->  Seq Scan on failures f  (cost=0.00..13.30 rows=330 width=32) (never executed)
  ->  Hash  (cost=15.00..15.00 rows=500 width=80) (never executed)
        ->  Seq Scan on failure_codes fc  (cost=0.00..15.00 rows=500 width=80) (never executed)
Planning:
  Buffers: shared hit=8
Planning Time: 0.112 ms
Execution Time: 0.012 ms
```
Seq Scan line(s): 3 -- see index findings below.

### metrics view: v_throughput_daily
```sql
SELECT * FROM v_throughput_daily
```
```
Nested Loop  (cost=0.15..12.09 rows=1 width=76) (actual time=0.037..0.055 rows=5 loops=1)
  Buffers: shared hit=21
  ->  Nested Loop  (cost=0.00..7.90 rows=1 width=40) (actual time=0.006..0.020 rows=5 loops=1)
        Join Filter: (wo.id = u.work_order_id)
        Rows Removed by Join Filter: 21
        Buffers: shared hit=11
        ->  Seq Scan on units u  (cost=0.00..6.70 rows=1 width=40) (actual time=0.003..0.011 rows=5 loops=1)
              Filter: ((completed_at IS NOT NULL) AND (status = 'done'::text))
              Rows Removed by Filter: 51
              Buffers: shared hit=6
        ->  Seq Scan on work_orders wo  (cost=0.00..1.09 rows=9 width=32) (actual time=0.000..0.001 rows=5 loops=5)
              Buffers: shared hit=5
  ->  Index Scan using products_pkey on products p  (cost=0.15..4.17 rows=1 width=48) (actual time=0.001..0.001 rows=1 loops=5)
        Index Cond: (id = wo.product_id)
        Buffers: shared hit=10
Planning:
  Buffers: shared hit=8
Planning Time: 0.137 ms
Execution Time: 0.067 ms
```
Seq Scan line(s): 2 -- see index findings below.

### metrics view: v_work_session_minutes
```sql
SELECT * FROM v_work_session_minutes
```
```
Hash Right Join  (cost=36.20..42.61 rows=137 width=107) (actual time=0.062..0.112 rows=137 loops=1)
  Hash Cond: (session_pauses.work_session_id = ws.id)
  Buffers: shared hit=8
  ->  HashAggregate  (cost=24.96..27.46 rows=200 width=48) (actual time=0.002..0.002 rows=0 loops=1)
        Group Key: session_pauses.work_session_id
        Batches: 1  Memory Usage: 40kB
        ->  Seq Scan on session_pauses  (cost=0.00..17.50 rows=746 width=32) (actual time=0.001..0.001 rows=0 loops=1)
              Filter: (ended_at IS NOT NULL)
  ->  Hash  (cost=9.53..9.53 rows=137 width=91) (actual time=0.051..0.051 rows=137 loops=1)
        Buckets: 1024  Batches: 1  Memory Usage: 26kB
        Buffers: shared hit=8
        ->  Seq Scan on work_sessions ws  (cost=0.00..9.53 rows=137 width=91) (actual time=0.004..0.025 rows=137 loops=1)
              Filter: (ended_at IS NOT NULL)
              Rows Removed by Filter: 26
              Buffers: shared hit=8
Planning:
  Buffers: shared hit=2
Planning Time: 0.105 ms
Execution Time: 0.134 ms
```
Seq Scan line(s): 2 -- see index findings below.

### metrics view: v_actual_vs_estimate
```sql
SELECT * FROM v_actual_vs_estimate
```
```
GroupAggregate  (cost=57.38..60.71 rows=39 width=110) (actual time=0.237..0.313 rows=33 loops=1)
  Group Key: po.id
  Buffers: shared hit=18
  ->  Sort  (cost=57.38..57.72 rows=137 width=126) (actual time=0.229..0.235 rows=137 loops=1)
        Sort Key: po.id, ws.unit_id
        Sort Method: quicksort  Memory: 42kB
        Buffers: shared hit=18
        ->  Hash Join  (cost=47.08..52.52 rows=137 width=126) (actual time=0.123..0.158 rows=137 loops=1)
              Hash Cond: (ws.plan_operation_id = po.id)
              Buffers: shared hit=18
              ->  Hash Right Join  (cost=36.20..41.24 rows=137 width=80) (actual time=0.093..0.107 rows=137 loops=1)
                    Hash Cond: (session_pauses.work_session_id = ws.id)
                    Buffers: shared hit=8
                    ->  HashAggregate  (cost=24.96..27.46 rows=200 width=48) (actual time=0.002..0.002 rows=0 loops=1)
                          Group Key: session_pauses.work_session_id
                          Batches: 1  Memory Usage: 40kB
                          ->  Seq Scan on session_pauses  (cost=0.00..17.50 rows=746 width=32) (actual time=0.001..0.001 rows=0 loops=1)
                                Filter: (ended_at IS NOT NULL)
                    ->  Hash  (cost=9.53..9.53 rows=137 width=64) (actual time=0.089..0.089 rows=137 loops=1)
                          Buckets: 1024  Batches: 1  Memory Usage: 21kB
                          Buffers: shared hit=8
                          ->  Seq Scan on work_sessions ws  (cost=0.00..9.53 rows=137 width=64) (actual time=0.002..0.021 rows=137 loops=1)
                                Filter: (ended_at IS NOT NULL)
                                Rows Removed by Filter: 26
                                Buffers: shared hit=8
              ->  Hash  (cost=10.39..10.39 rows=39 width=62) (actual time=0.021..0.021 rows=39 loops=1)
                    Buckets: 1024  Batches: 1  Memory Usage: 12kB
                    Buffers: shared hit=10
                    ->  Seq Scan on plan_operations po  (cost=0.00..10.39 rows=39 width=62) (actual time=0.003..0.015 rows=39 loops=1)
                          Buffers: shared hit=10
Planning:
  Buffers: shared hit=16
Planning Time: 0.192 ms
Execution Time: 0.338 ms
```
Seq Scan line(s): 3 -- see index findings below.

### metrics view: v_queue_time_by_station
```sql
SELECT * FROM v_queue_time_by_station
```
```
Hash Join  (cost=1.56..1947.01 rows=153 width=127) (actual time=0.034..2.524 rows=163 loops=1)
  Hash Cond: (ws.station_id = s.id)
  Buffers: shared hit=1313
  ->  Seq Scan on work_sessions ws  (cost=0.00..9.53 rows=153 width=56) (actual time=0.001..0.016 rows=163 loops=1)
        Buffers: shared hit=8
  ->  Hash  (cost=1.25..1.25 rows=25 width=47) (actual time=0.009..0.010 rows=25 loops=1)
        Buckets: 1024  Batches: 1  Memory Usage: 10kB
        Buffers: shared hit=1
        ->  Seq Scan on stations s  (cost=0.00..1.25 rows=25 width=47) (actual time=0.004..0.006 rows=25 loops=1)
              Buffers: shared hit=1
  SubPlan 1
    ->  Aggregate  (cost=6.31..6.32 rows=1 width=8) (actual time=0.007..0.007 rows=1 loops=163)
          Buffers: shared hit=652
          ->  Seq Scan on transits t  (cost=0.00..6.31 rows=1 width=8) (actual time=0.005..0.007 rows=1 loops=163)
                Filter: ((arrived_at <= ws.started_at) AND (unit_id = ws.unit_id) AND (to_station_id = ws.station_id))
                Rows Removed by Filter: 131
                Buffers: shared hit=652
  SubPlan 2
    ->  Aggregate  (cost=6.31..6.32 rows=1 width=8) (actual time=0.007..0.007 rows=1 loops=163)
          Buffers: shared hit=652
          ->  Seq Scan on transits t_1  (cost=0.00..6.31 rows=1 width=8) (actual time=0.005..0.007 rows=1 loops=163)
                Filter: ((arrived_at <= ws.started_at) AND (unit_id = ws.unit_id) AND (to_station_id = ws.station_id))
                Rows Removed by Filter: 131
                Buffers: shared hit=652
Planning:
  Buffers: shared hit=10
Planning Time: 0.174 ms
Execution Time: 2.549 ms
```
Seq Scan line(s): 4 -- see index findings below.

### metrics view: v_rework_hours_pct
```sql
SELECT * FROM v_rework_hours_pct
```
```
Aggregate  (cost=45.01..45.02 rows=1 width=64) (actual time=0.094..0.095 rows=1 loops=1)
  Buffers: shared hit=8
  ->  Hash Right Join  (cost=36.20..41.24 rows=137 width=59) (actual time=0.038..0.050 rows=137 loops=1)
        Hash Cond: (session_pauses.work_session_id = ws.id)
        Buffers: shared hit=8
        ->  HashAggregate  (cost=24.96..27.46 rows=200 width=48) (actual time=0.001..0.001 rows=0 loops=1)
              Group Key: session_pauses.work_session_id
              Batches: 1  Memory Usage: 40kB
              ->  Seq Scan on session_pauses  (cost=0.00..17.50 rows=746 width=32) (actual time=0.000..0.000 rows=0 loops=1)
                    Filter: (ended_at IS NOT NULL)
        ->  Hash  (cost=9.53..9.53 rows=137 width=43) (actual time=0.035..0.035 rows=137 loops=1)
              Buckets: 1024  Batches: 1  Memory Usage: 19kB
              Buffers: shared hit=8
              ->  Seq Scan on work_sessions ws  (cost=0.00..9.53 rows=137 width=43) (actual time=0.003..0.018 rows=137 loops=1)
                    Filter: (ended_at IS NOT NULL)
                    Rows Removed by Filter: 26
                    Buffers: shared hit=8
Planning:
  Buffers: shared hit=2
Planning Time: 0.099 ms
Execution Time: 0.113 ms
```
Seq Scan line(s): 2 -- see index findings below.

### metrics view: v_wip_age
```sql
SELECT * FROM v_wip_age
```
```
Hash Join  (cost=19.24..27.15 rows=51 width=125) (actual time=0.018..0.044 rows=51 loops=1)
  Hash Cond: (u.work_order_id = wo.id)
  Buffers: shared hit=8
  ->  Seq Scan on units u  (cost=0.00..6.70 rows=51 width=45) (actual time=0.003..0.013 rows=51 loops=1)
        Filter: (status <> ALL ('{done,scrapped}'::text[]))
        Rows Removed by Filter: 5
        Buffers: shared hit=6
  ->  Hash  (cost=19.13..19.13 rows=9 width=72) (actual time=0.011..0.012 rows=9 loops=1)
        Buckets: 1024  Batches: 1  Memory Usage: 9kB
        Buffers: shared hit=2
        ->  Hash Join  (cost=1.20..19.13 rows=9 width=72) (actual time=0.008..0.010 rows=9 loops=1)
              Hash Cond: (p.id = wo.product_id)
              Buffers: shared hit=2
              ->  Seq Scan on products p  (cost=0.00..15.70 rows=570 width=48) (actual time=0.002..0.002 rows=9 loops=1)
                    Buffers: shared hit=1
              ->  Hash  (cost=1.09..1.09 rows=9 width=40) (actual time=0.004..0.004 rows=9 loops=1)
                    Buckets: 1024  Batches: 1  Memory Usage: 9kB
                    Buffers: shared hit=1
                    ->  Seq Scan on work_orders wo  (cost=0.00..1.09 rows=9 width=40) (actual time=0.001..0.002 rows=9 loops=1)
                          Buffers: shared hit=1
Planning:
  Buffers: shared hit=8
Planning Time: 0.142 ms
Execution Time: 0.056 ms
```
Seq Scan line(s): 3 -- see index findings below.

## Index findings

- `units` has no standalone index on `status` -- only `ix_units_work_order_id_status` (composite, leading column `work_order_id`), which the board's `active_units()` query (`WHERE status IN ('queued','at_station','in_transit')`, no `work_order_id` predicate) cannot use. At this task's 50-unit scale Postgres correctly seq-scans anyway (cheaper than an index for a table this size) -- not a bug today, but the one concrete gap worth a follow-up once `units` holds thousands of historical done/scrapped rows alongside a few hundred active ones. Recommended (report only, not applied): `CREATE INDEX ix_units_active_status ON units (status) WHERE status IN ('queued','at_station','in_transit');` -- a partial index sized to exactly the board's query, not a full-column index nobody else's query needs.
- `v_queue_time_by_station` (app/domain/metrics.py) runs a correlated subplan per `work_sessions` row scanning `transits` (`WHERE unit_id = ws.unit_id AND to_station_id = ws.station_id AND arrived_at <= ws.started_at`) to find the matching arrival -- an N+1-shaped pattern *inside* the view itself, O(work_sessions x transits-per-unit). `transits` is only indexed on `unit_id` alone (models_floor.py), so this filter can't use an index on the other two predicates; at this scale it's still sub-millisecond (163 correlated scans, ~2ms total) but will not stay that way once units accumulate many historical transits each. Recommended (report only, not applied): `CREATE INDEX ix_transits_unit_station_arrived ON transits (unit_id, to_station_id, arrived_at);`

## PERF: PASS vs N2
