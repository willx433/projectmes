# Gate 3 walk output

Run tag: `20260715t121302-17fc99`
Timestamp: 2026-07-15T12:13:05.644432+00:00
Database: PostgreSQL 16
- Connected to PostgreSQL: postgresql+psycopg://mes@127.0.0.1:55432/mes
- Postgres: assuming schema already at head via alembic (no create_all).

## 1. Seed summary (real domain layer: app.domain.library / app.domain.workorders)

- Product: Apollo Gate3 20260715t121302-17fc99 (ab39fa8b-134d-4278-a9bb-aa895e867cf1)
- JB2 part number: GATE3-20260715t121302-17fc99-BLK  |  job number: GATE3-20260715t121302-17fc99-01
- Work order: e4ede431-2939-4939-9f08-d82ceeadd2bc status=ready
- Unit: 32f0a172-139f-4a86-a398-1df8e9fe58aa unit_no=1
- Plan operations (3):
    seq=1 'Gate3 CNC Slide' instruction_set=248889df-1d6e-416b-add2-c6aeede2817e v1 blocked=False
    seq=2 'Gate3 Barrel Fit' instruction_set=8511867b-d3f0-4378-9b42-f24f12682070 v1 blocked=False
    seq=3 'Gate3 Final QC' instruction_set=5a7a2a5f-2f49-487c-a393-6776cf509acd v1 blocked=False
- Operator (jb2 employee-linked, roles=['operator', 'lead']): Gate3 Operator 20260715t121302-17fc99 (93544573-1e8a-4736-b3eb-2bf2b2eefc91)
- QC signoff authorizer (roles=['lead']): Gate3 QC Authorizer 20260715t121302-17fc99 (4097e1e1-01bd-4b81-8f42-0cef3be4ec37)
- Failure code seeded: GATE3-20260715t121302-17fc99 (b26068bd-5b22-4f61-a2b6-9a87b1303cbf)

## 2. Kit-up

- POST /admin/kitup -> 303 /admin/kitup?work_order_id=e4ede431-2939-4939-9f08-d82ceeadd2bc&success=box%20assigned
- box_qr=BOX:gate3-20260715t121302-17fc99  serial=GATE3-SN-20260715t121302-17fc99

## 3. Station-by-station walk (real HTTP: badge -> scan -> substeps -> finish)

### Op seq=1 (CNC1, station Gate3 CNC 20260715t121302-17fc99)
- badge-in: 200
- scan: 200 code='accepted' work_session_id=a5659301-1b4d-4d51-8800-fc4986494b9d
- substep 1 [action] 'Load fixture': 303 ok=True
- substep 2 [measurement] 'Verify chamber depth': 303 ok=True
- finish: 200 code='finished' unit_status='in_transit' outbox_ids=['2c1c0a83-7fde-414b-8888-bde59ec39873']

### Op seq=2 (FIT1, station Gate3 Fit 20260715t121302-17fc99)
- badge-in: 200
- scan: 200 code='accepted' work_session_id=9e7efc9e-4da8-48da-a25e-2ebd93e5a8b8
- substep 1 [action] 'Press-fit barrel to slide': 303 ok=True
- substep 2 [action] 'Verify fit by hand': 303 ok=True
- finish: 200 code='finished' unit_status='in_transit' outbox_ids=['20c2e837-9ce3-48fa-905e-e87faa2f5a13']

### Op seq=3 (QC1, station Gate3 QC 20260715t121302-17fc99)
- badge-in: 200
- scan: 200 code='accepted' work_session_id=67a0ab8b-9d61-4d92-8696-c366adb6e52b
- substep 1 [inspection] 'Visual/function inspection': 303 ok=True
- substep 2 [signoff] 'QC signoff': 303 ok=True
- finish: 200 code='finished' unit_status='done' outbox_ids=['8a52e2e5-ba06-4afd-88db-067ef74e22ca']

## 4. Unit final state

- unit.status = 'done'
- unit.serial_number = 'GATE3-SN-20260715t121302-17fc99'
- unit.first_pass = True  rework_count=0
- unit.completed_at = 2026-07-15 08:13:05.600261-04:00

## 5. Outbox drain to fake-JB2

- drain outcomes: [(UUID('2c1c0a83-7fde-414b-8888-bde59ec39873'), 'confirmed'), (UUID('20c2e837-9ce3-48fa-905e-e87faa2f5a13'), 'confirmed'), (UUID('8a52e2e5-ba06-4afd-88db-067ef74e22ca'), 'confirmed')]
- distinct write paths received: ['/time-ticket-details']
- writes:
    /time-ticket-details  body={'timeEnd': '2026-07-15T12:13:05Z', 'cycleTime': None, 'jobNumber': 'GATE3-20260715t121302-17fc99-01', 'setupTime': None, 'timeStart': '2026-07-15T12:13:05Z', 'stepNumber': 1, 'ticketDate': '2026-07-15T00:00:00Z', 'workCenter': 17841161271, 'employeeCode': 4200, 'reasonNumber': None, 'piecesFinished': 1, 'piecesScrapped': 0}
    /time-ticket-details  body={'timeEnd': '2026-07-15T12:13:05Z', 'cycleTime': None, 'jobNumber': 'GATE3-20260715t121302-17fc99-01', 'setupTime': None, 'timeStart': '2026-07-15T12:13:05Z', 'stepNumber': 2, 'ticketDate': '2026-07-15T00:00:00Z', 'workCenter': 17841161272, 'employeeCode': 4200, 'reasonNumber': None, 'piecesFinished': 1, 'piecesScrapped': 0}
    /time-ticket-details  body={'timeEnd': '2026-07-15T12:13:05Z', 'cycleTime': None, 'jobNumber': 'GATE3-20260715t121302-17fc99-01', 'setupTime': None, 'timeStart': '2026-07-15T12:13:05Z', 'stepNumber': 3, 'ticketDate': '2026-07-15T00:00:00Z', 'workCenter': 17841161273, 'employeeCode': 4200, 'reasonNumber': None, 'piecesFinished': 1, 'piecesScrapped': 0}
- **CR-010 check (zero /order-routings PATCH, only time-tickets/time-ticket-details): PASS**

## 6. §9.1-9.4 data queryability (DD §9, MES_Design_Document.md)

### §9.1 Identity & traceability
- work_order e4ede431-2939-4939-9f08-d82ceeadd2bc <-> jb2_line_item 5e496429-8dc9-4533-aa83-a798a50245f4
- unit serial: GATE3-SN-20260715t121302-17fc99  box: 7ce79e7d-368a-4c76-90fe-fea713bdfe63 qr=BOX:gate3-20260715t121302-17fc99
- instruction sets/version per op: seq1:248889df-1d6e-416b-add2-c6aeede2817e/v1, seq2:8511867b-d3f0-4378-9b42-f24f12682070/v1, seq3:5a7a2a5f-2f49-487c-a393-6776cf509acd/v1

### §9.2 Location & movement
- scans recorded: 3
    scan 10eaaaff-12f9-4a17-8e7b-eed778376556: station=95c62530-0df2-4d85-ad54-bdbf45ffb9fd result=accepted override_by=None at=2026-07-15 08:13:05.267457-04:00
    scan 1cce2534-8b63-4d86-9de8-617fcfbb131a: station=89801d08-d4ff-4a77-aee1-f9b31f1be004 result=accepted override_by=None at=2026-07-15 08:13:05.440583-04:00
    scan b6692103-5c8b-4647-a9ce-760d35e03312: station=b0c22cc5-412b-468f-9d50-0bb93f928287 result=accepted override_by=None at=2026-07-15 08:13:05.529405-04:00
- transit hops: 2
    transit 9549b3b8-57bd-49a1-9359-9c13dbf0c3f0: 95c62530-0df2-4d85-ad54-bdbf45ffb9fd -> 89801d08-d4ff-4a77-aee1-f9b31f1be004 seconds=0 arrived=True
    transit b6b671a3-e444-46bf-b670-ba9752ff7fb4: 89801d08-d4ff-4a77-aee1-f9b31f1be004 -> b0c22cc5-412b-468f-9d50-0bb93f928287 seconds=0 arrived=True
- box current location (station_id): None  current_unit: 32f0a172-139f-4a86-a398-1df8e9fe58aa

### §9.3 Time
- work sessions: 3
    session a5659301-1b4d-4d51-8800-fc4986494b9d: op=2366fdb1-144b-4c38-b7a1-c8aa315e011d kind=first_pass started=2026-07-15 08:13:05.270323-04:00 ended=2026-07-15 08:13:05.407043-04:00 close_reason=finished
    session 9e7efc9e-4da8-48da-a25e-2ebd93e5a8b8: op=2de2ca4f-01af-4659-8a3d-c713ab4a5537 kind=first_pass started=2026-07-15 08:13:05.443734-04:00 ended=2026-07-15 08:13:05.505654-04:00 close_reason=finished
    session 67a0ab8b-9d61-4d92-8696-c366adb6e52b: op=004664a6-30a7-4e29-836f-3afbfb6fb48e kind=first_pass started=2026-07-15 08:13:05.531989-04:00 ended=2026-07-15 08:13:05.600261-04:00 close_reason=finished
- substep executions: 6 (started=6, completed=6)

### §9.4 Quality & measurements
- measurements recorded: 1
    Verify chamber depth: value=0.500 nominal=0.5 in_tolerance=True gauge_id=None
- signoffs completed: 1
- first_pass flag: True
- scrap events: 0 (expected 0 on this happy path)

### Unit event timeline
`events.timeline(session, unit_id=unit.id)` (unit-entity-only rows, per its own docstring):
  [2026-07-15 08:13:05.397711-04:00] unit.moved             entity=unit:32f0a172-139f-4a86-a398-1df8e9fe58aa after={'to_plan_operation_id': '2de2ca4f-01af-4659-8a3d-c713ab4a5537', 'from_plan_operation_id': '2366fdb1-144b-4c38-b7a1-c8aa315e011d'}
  [2026-07-15 08:13:05.498392-04:00] unit.moved             entity=unit:32f0a172-139f-4a86-a398-1df8e9fe58aa after={'to_plan_operation_id': '004664a6-30a7-4e29-836f-3afbfb6fb48e', 'from_plan_operation_id': '2de2ca4f-01af-4659-8a3d-c713ab4a5537'}
  [2026-07-15 08:13:05.593003-04:00] unit.done              entity=unit:32f0a172-139f-4a86-a398-1df8e9fe58aa after={}

Full cross-entity journey timeline (unit + its scans/sessions/substeps/measurements):
  [2026-07-15 08:13:05.267457-04:00] session.opened         entity=worksession:a5659301-1b4d-4d51-8800-fc4986494b9d after={'kind': 'first_pass', 'unit_id': '32f0a172-139f-4a86-a398-1df8e9fe58aa', 'plan_operation_id': '2366fdb1-144b-4c38-b7a1-c8aa315e011d'}
  [2026-07-15 08:13:05.267457-04:00] scan.accepted          entity=scan:10eaaaff-12f9-4a17-8e7b-eed778376556 after={'unit_id': '32f0a172-139f-4a86-a398-1df8e9fe58aa', 'override': False, 'work_session_id': 'a5659301-1b4d-4d51-8800-fc4986494b9d', 'plan_operation_id': '2366fdb1-144b-4c38-b7a1-c8aa315e011d'}
  [2026-07-15 08:13:05.321803-04:00] substep.done           entity=substepexecution:cb8198ca-293a-405e-a0ac-440a0f567f5b after={'type': 'action', 'unit_id': '32f0a172-139f-4a86-a398-1df8e9fe58aa'}
  [2026-07-15 08:13:05.373422-04:00] measurement.recorded   entity=measurement:f4c96a29-f163-4d8f-adc4-aabab7d25254 after={'value': '0.500', 'unit_id': '32f0a172-139f-4a86-a398-1df8e9fe58aa', 'in_tolerance': True, 'substep_execution_id': '986d98a1-2568-4e6a-8ecf-2031195dae90'}
  [2026-07-15 08:13:05.373422-04:00] substep.done           entity=substepexecution:986d98a1-2568-4e6a-8ecf-2031195dae90 after={'unit_id': '32f0a172-139f-4a86-a398-1df8e9fe58aa', 'measurement_id': 'f4c96a29-f163-4d8f-adc4-aabab7d25254'}
  [2026-07-15 08:13:05.397711-04:00] session.closed         entity=worksession:a5659301-1b4d-4d51-8800-fc4986494b9d after={'reason': 'finished'}
  [2026-07-15 08:13:05.397711-04:00] unit.moved             entity=unit:32f0a172-139f-4a86-a398-1df8e9fe58aa after={'to_plan_operation_id': '2de2ca4f-01af-4659-8a3d-c713ab4a5537', 'from_plan_operation_id': '2366fdb1-144b-4c38-b7a1-c8aa315e011d'}
  [2026-07-15 08:13:05.440583-04:00] session.opened         entity=worksession:9e7efc9e-4da8-48da-a25e-2ebd93e5a8b8 after={'kind': 'first_pass', 'unit_id': '32f0a172-139f-4a86-a398-1df8e9fe58aa', 'plan_operation_id': '2de2ca4f-01af-4659-8a3d-c713ab4a5537'}
  [2026-07-15 08:13:05.440583-04:00] scan.accepted          entity=scan:1cce2534-8b63-4d86-9de8-617fcfbb131a after={'unit_id': '32f0a172-139f-4a86-a398-1df8e9fe58aa', 'override': False, 'work_session_id': '9e7efc9e-4da8-48da-a25e-2ebd93e5a8b8', 'plan_operation_id': '2de2ca4f-01af-4659-8a3d-c713ab4a5537'}
  [2026-07-15 08:13:05.461457-04:00] substep.done           entity=substepexecution:95926776-c8b9-4c9a-bfc6-b1daabc8555e after={'type': 'action', 'unit_id': '32f0a172-139f-4a86-a398-1df8e9fe58aa'}
  [2026-07-15 08:13:05.481286-04:00] substep.done           entity=substepexecution:6a45bc27-649b-4b9c-b1cf-c2acd2fab133 after={'type': 'action', 'unit_id': '32f0a172-139f-4a86-a398-1df8e9fe58aa'}
  [2026-07-15 08:13:05.498392-04:00] session.closed         entity=worksession:9e7efc9e-4da8-48da-a25e-2ebd93e5a8b8 after={'reason': 'finished'}
  [2026-07-15 08:13:05.498392-04:00] unit.moved             entity=unit:32f0a172-139f-4a86-a398-1df8e9fe58aa after={'to_plan_operation_id': '004664a6-30a7-4e29-836f-3afbfb6fb48e', 'from_plan_operation_id': '2de2ca4f-01af-4659-8a3d-c713ab4a5537'}
  [2026-07-15 08:13:05.529405-04:00] session.opened         entity=worksession:67a0ab8b-9d61-4d92-8696-c366adb6e52b after={'kind': 'first_pass', 'unit_id': '32f0a172-139f-4a86-a398-1df8e9fe58aa', 'plan_operation_id': '004664a6-30a7-4e29-836f-3afbfb6fb48e'}
  [2026-07-15 08:13:05.529405-04:00] scan.accepted          entity=scan:b6692103-5c8b-4647-a9ce-760d35e03312 after={'unit_id': '32f0a172-139f-4a86-a398-1df8e9fe58aa', 'override': False, 'work_session_id': '67a0ab8b-9d61-4d92-8696-c366adb6e52b', 'plan_operation_id': '004664a6-30a7-4e29-836f-3afbfb6fb48e'}
  [2026-07-15 08:13:05.550747-04:00] substep.done           entity=substepexecution:1ddcaea1-265f-435e-bf3b-e35c26341720 after={'type': 'inspection', 'unit_id': '32f0a172-139f-4a86-a398-1df8e9fe58aa'}
  [2026-07-15 08:13:05.574028-04:00] substep.done           entity=substepexecution:f42471ee-14a6-47a7-8a8d-7c52fc7f5ba1 after={'unit_id': '32f0a172-139f-4a86-a398-1df8e9fe58aa', 'signoff_role': 'lead', 'acting_operator_id': '93544573-1e8a-4736-b3eb-2bf2b2eefc91'}
  [2026-07-15 08:13:05.593003-04:00] session.closed         entity=worksession:67a0ab8b-9d61-4d92-8696-c366adb6e52b after={'reason': 'finished'}
  [2026-07-15 08:13:05.593003-04:00] unit.done              entity=unit:32f0a172-139f-4a86-a398-1df8e9fe58aa after={}

## GATE 3 WALK: PASS
