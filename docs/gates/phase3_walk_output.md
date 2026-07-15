# Gate 3 walk output

Run tag: `20260715t145641-1a24a0`
Timestamp: 2026-07-15T14:56:45.256434+00:00
Database: PostgreSQL 16
- Connected to PostgreSQL: postgresql+psycopg://mes@127.0.0.1:55432/mes
- Postgres: assuming schema already at head via alembic (no create_all).

## 1. Seed summary (real domain layer: app.domain.library / app.domain.workorders)

- Product: Apollo Gate3 20260715t145641-1a24a0 (bc777704-a0a0-47f5-97fb-9d57930739ad)
- JB2 part number: GATE3-20260715t145641-1a24a0-BLK  |  job number: GATE3-20260715t145641-1a24a0-01
- Work order: 39b7ee23-481c-4093-84f0-50c2283470ed status=ready
- Unit: ae121338-773f-4c9b-999d-edbd2583ec4e unit_no=1
- Plan operations (3):
    seq=1 'Gate3 CNC Slide' instruction_set=33f72a3a-31e9-4afe-9cbb-586b7c027be3 v1 blocked=False
    seq=2 'Gate3 Barrel Fit' instruction_set=c6d74170-0aa6-488d-8de1-2777eb868d7d v1 blocked=False
    seq=3 'Gate3 Final QC' instruction_set=88178391-0eb2-4975-ae35-543029c10260 v1 blocked=False
- Operator (jb2 employee-linked, roles=['operator', 'lead']): Gate3 Operator 20260715t145641-1a24a0 (f0ba782c-ef8e-4e76-acaf-5a255643995c)
- QC signoff authorizer (roles=['lead']): Gate3 QC Authorizer 20260715t145641-1a24a0 (081ed247-d562-40c4-8df5-4901cf8bf69e)
- Failure code seeded: GATE3-20260715t145641-1a24a0 (d7d216b2-f33c-453a-889e-8817b1e4d2e9)

## 2. Kit-up

- POST /admin/kitup -> 303 /admin/kitup?work_order_id=39b7ee23-481c-4093-84f0-50c2283470ed&success=box%20assigned
- box_qr=BOX:gate3-20260715t145641-1a24a0  serial=GATE3-SN-20260715t145641-1a24a0

## 3. Station-by-station walk (real HTTP: badge -> scan -> substeps -> finish)

### Op seq=1 (CNC1, station Gate3 CNC 20260715t145641-1a24a0)
- badge-in: 200
- scan: 200 code='accepted' work_session_id=a0d578d2-f988-494a-a028-63e007ad0b28
- substep 1 [action] 'Load fixture': 303 ok=True
- substep 2 [measurement] 'Verify chamber depth': 303 ok=True
- finish: 200 code='finished' unit_status='in_transit' outbox_ids=['ef4a4534-cadc-4db5-b1be-43071663f91a']

### Op seq=2 (FIT1, station Gate3 Fit 20260715t145641-1a24a0)
- badge-in: 200
- scan: 200 code='accepted' work_session_id=f938d7de-971f-40b7-b29b-8de69ed0ed16
- substep 1 [action] 'Press-fit barrel to slide': 303 ok=True
- substep 2 [action] 'Verify fit by hand': 303 ok=True
- finish: 200 code='finished' unit_status='in_transit' outbox_ids=['bea81506-6b1f-40de-8157-01bb24540df8']

### Op seq=3 (QC1, station Gate3 QC 20260715t145641-1a24a0)
- badge-in: 200
- scan: 200 code='accepted' work_session_id=9965a004-fe06-4a8b-bfb1-959873123b0f
- substep 1 [inspection] 'Visual/function inspection': 303 ok=True
- substep 2 [signoff] 'QC signoff': 303 ok=True
- finish: 200 code='finished' unit_status='done' outbox_ids=['470c62ed-127a-42b9-9b10-9ef3929df518']

## 4. Unit final state

- unit.status = 'done'
- unit.serial_number = 'GATE3-SN-20260715t145641-1a24a0'
- unit.first_pass = True  rework_count=0
- unit.completed_at = 2026-07-15 10:56:45.208135-04:00

## 5. Outbox drain to fake-JB2

- drain outcomes: [(UUID('ef4a4534-cadc-4db5-b1be-43071663f91a'), 'confirmed'), (UUID('bea81506-6b1f-40de-8157-01bb24540df8'), 'confirmed'), (UUID('470c62ed-127a-42b9-9b10-9ef3929df518'), 'confirmed')]
- distinct write paths received: ['/time-tickets']
- writes:
    /time-tickets  body={'ticketDate': '2026-07-15T00:00:00Z', 'employeeCode': 4200, 'allowClosedJobs': True, 'timeTicketDetails': [{'timeEnd': '14:56', 'jobNumber': 'GATE3-20260715t145641-1a24a0-01', 'timeStart': '14:56', 'stepNumber': 1, 'reasonNumber': None, 'piecesFinished': 1, 'piecesScrapped': 0}]}
    /time-tickets  body={'ticketDate': '2026-07-15T00:00:00Z', 'employeeCode': 4200, 'allowClosedJobs': True, 'timeTicketDetails': [{'timeEnd': '14:56', 'jobNumber': 'GATE3-20260715t145641-1a24a0-01', 'timeStart': '14:56', 'stepNumber': 2, 'reasonNumber': None, 'piecesFinished': 1, 'piecesScrapped': 0}]}
    /time-tickets  body={'ticketDate': '2026-07-15T00:00:00Z', 'employeeCode': 4200, 'allowClosedJobs': True, 'timeTicketDetails': [{'timeEnd': '14:56', 'jobNumber': 'GATE3-20260715t145641-1a24a0-01', 'timeStart': '14:56', 'stepNumber': 3, 'reasonNumber': None, 'piecesFinished': 1, 'piecesScrapped': 0}]}
- **CR-010/CR-018 check (zero /order-routings PATCH, only nested /time-tickets writes): PASS**

## 6. §9.1-9.4 data queryability (DD §9, MES_Design_Document.md)

### §9.1 Identity & traceability
- work_order 39b7ee23-481c-4093-84f0-50c2283470ed <-> jb2_line_item 71028578-f006-41d3-b4f4-a3d1f2ab99d4
- unit serial: GATE3-SN-20260715t145641-1a24a0  box: 69528d1f-834c-44ba-b5f3-56a9c45d33ef qr=BOX:gate3-20260715t145641-1a24a0
- instruction sets/version per op: seq1:33f72a3a-31e9-4afe-9cbb-586b7c027be3/v1, seq2:c6d74170-0aa6-488d-8de1-2777eb868d7d/v1, seq3:88178391-0eb2-4975-ae35-543029c10260/v1

### §9.2 Location & movement
- scans recorded: 3
    scan 8983249b-517b-475e-84b2-548c61485d4a: station=42e58885-0589-4b21-babf-530bd34c50e0 result=accepted override_by=None at=2026-07-15 10:56:44.898659-04:00
    scan d74e229f-1932-4286-bd62-64e89b0ffc94: station=a907a691-9cfa-4f25-9009-824f50e22bf4 result=accepted override_by=None at=2026-07-15 10:56:45.077350-04:00
    scan c08f9297-b26d-461b-a42e-01f7ff0acd05: station=6fd7b691-e5ea-485b-a42e-3712747b220b result=accepted override_by=None at=2026-07-15 10:56:45.158785-04:00
- transit hops: 2
    transit 555ef9e6-14cd-4ea4-8ff7-9664b1bc1fb6: 42e58885-0589-4b21-babf-530bd34c50e0 -> a907a691-9cfa-4f25-9009-824f50e22bf4 seconds=0 arrived=True
    transit 4053d568-d04c-485e-b9d0-083aaba1c2b8: a907a691-9cfa-4f25-9009-824f50e22bf4 -> 6fd7b691-e5ea-485b-a42e-3712747b220b seconds=0 arrived=True
- box current location (station_id): None  current_unit: ae121338-773f-4c9b-999d-edbd2583ec4e

### §9.3 Time
- work sessions: 3
    session a0d578d2-f988-494a-a028-63e007ad0b28: op=ae5fe9d0-d697-4ded-9857-861b02b7a637 kind=first_pass started=2026-07-15 10:56:44.886660-04:00 ended=2026-07-15 10:56:45.035385-04:00 close_reason=finished
    session f938d7de-971f-40b7-b29b-8de69ed0ed16: op=f648ab49-a833-4185-add5-d25d94064505 kind=first_pass started=2026-07-15 10:56:45.069608-04:00 ended=2026-07-15 10:56:45.124237-04:00 close_reason=finished
    session 9965a004-fe06-4a8b-bfb1-959873123b0f: op=8900e49b-4b1a-4309-849a-369d9c8807bc kind=first_pass started=2026-07-15 10:56:45.151150-04:00 ended=2026-07-15 10:56:45.208135-04:00 close_reason=finished
- substep executions: 6 (started=6, completed=6)

### §9.4 Quality & measurements
- measurements recorded: 1
    Verify chamber depth: value=0.500 nominal=0.5 in_tolerance=True gauge_id=None
- signoffs completed: 1
- first_pass flag: True
- scrap events: 0 (expected 0 on this happy path)

### Unit event timeline
`events.timeline(session, unit_id=unit.id)` (unit-entity-only rows, per its own docstring):
  [2026-07-15 10:56:45.025802-04:00] unit.moved             entity=unit:ae121338-773f-4c9b-999d-edbd2583ec4e after={'to_plan_operation_id': 'f648ab49-a833-4185-add5-d25d94064505', 'from_plan_operation_id': 'ae5fe9d0-d697-4ded-9857-861b02b7a637'}
  [2026-07-15 10:56:45.117787-04:00] unit.moved             entity=unit:ae121338-773f-4c9b-999d-edbd2583ec4e after={'to_plan_operation_id': '8900e49b-4b1a-4309-849a-369d9c8807bc', 'from_plan_operation_id': 'f648ab49-a833-4185-add5-d25d94064505'}
  [2026-07-15 10:56:45.200904-04:00] unit.done              entity=unit:ae121338-773f-4c9b-999d-edbd2583ec4e after={}

Full cross-entity journey timeline (unit + its scans/sessions/substeps/measurements):
  [2026-07-15 10:56:44.881443-04:00] session.opened         entity=worksession:a0d578d2-f988-494a-a028-63e007ad0b28 after={'kind': 'first_pass', 'unit_id': 'ae121338-773f-4c9b-999d-edbd2583ec4e', 'plan_operation_id': 'ae5fe9d0-d697-4ded-9857-861b02b7a637'}
  [2026-07-15 10:56:44.881443-04:00] scan.accepted          entity=scan:8983249b-517b-475e-84b2-548c61485d4a after={'unit_id': 'ae121338-773f-4c9b-999d-edbd2583ec4e', 'override': False, 'work_session_id': 'a0d578d2-f988-494a-a028-63e007ad0b28', 'plan_operation_id': 'ae5fe9d0-d697-4ded-9857-861b02b7a637'}
  [2026-07-15 10:56:44.973322-04:00] substep.done           entity=substepexecution:6f3582ce-e0ce-47cb-9031-9246a5258aec after={'type': 'action', 'unit_id': 'ae121338-773f-4c9b-999d-edbd2583ec4e'}
  [2026-07-15 10:56:45.001841-04:00] measurement.recorded   entity=measurement:341100c1-36ab-4f92-bca4-fad5c765516b after={'value': '0.500', 'unit_id': 'ae121338-773f-4c9b-999d-edbd2583ec4e', 'in_tolerance': True, 'substep_execution_id': 'b1486069-afba-4503-b48e-62e2327748eb'}
  [2026-07-15 10:56:45.001841-04:00] substep.done           entity=substepexecution:b1486069-afba-4503-b48e-62e2327748eb after={'unit_id': 'ae121338-773f-4c9b-999d-edbd2583ec4e', 'measurement_id': '341100c1-36ab-4f92-bca4-fad5c765516b'}
  [2026-07-15 10:56:45.025802-04:00] session.closed         entity=worksession:a0d578d2-f988-494a-a028-63e007ad0b28 after={'reason': 'finished'}
  [2026-07-15 10:56:45.025802-04:00] unit.moved             entity=unit:ae121338-773f-4c9b-999d-edbd2583ec4e after={'to_plan_operation_id': 'f648ab49-a833-4185-add5-d25d94064505', 'from_plan_operation_id': 'ae5fe9d0-d697-4ded-9857-861b02b7a637'}
  [2026-07-15 10:56:45.066465-04:00] session.opened         entity=worksession:f938d7de-971f-40b7-b29b-8de69ed0ed16 after={'kind': 'first_pass', 'unit_id': 'ae121338-773f-4c9b-999d-edbd2583ec4e', 'plan_operation_id': 'f648ab49-a833-4185-add5-d25d94064505'}
  [2026-07-15 10:56:45.066465-04:00] scan.accepted          entity=scan:d74e229f-1932-4286-bd62-64e89b0ffc94 after={'unit_id': 'ae121338-773f-4c9b-999d-edbd2583ec4e', 'override': False, 'work_session_id': 'f938d7de-971f-40b7-b29b-8de69ed0ed16', 'plan_operation_id': 'f648ab49-a833-4185-add5-d25d94064505'}
  [2026-07-15 10:56:45.083585-04:00] substep.done           entity=substepexecution:2264d3a4-451a-4d26-bea5-8e0decbe9013 after={'type': 'action', 'unit_id': 'ae121338-773f-4c9b-999d-edbd2583ec4e'}
  [2026-07-15 10:56:45.100672-04:00] substep.done           entity=substepexecution:4b4fffc0-14eb-454e-b5e5-baba4a08a7b7 after={'type': 'action', 'unit_id': 'ae121338-773f-4c9b-999d-edbd2583ec4e'}
  [2026-07-15 10:56:45.117787-04:00] session.closed         entity=worksession:f938d7de-971f-40b7-b29b-8de69ed0ed16 after={'reason': 'finished'}
  [2026-07-15 10:56:45.117787-04:00] unit.moved             entity=unit:ae121338-773f-4c9b-999d-edbd2583ec4e after={'to_plan_operation_id': '8900e49b-4b1a-4309-849a-369d9c8807bc', 'from_plan_operation_id': 'f648ab49-a833-4185-add5-d25d94064505'}
  [2026-07-15 10:56:45.147007-04:00] session.opened         entity=worksession:9965a004-fe06-4a8b-bfb1-959873123b0f after={'kind': 'first_pass', 'unit_id': 'ae121338-773f-4c9b-999d-edbd2583ec4e', 'plan_operation_id': '8900e49b-4b1a-4309-849a-369d9c8807bc'}
  [2026-07-15 10:56:45.147007-04:00] scan.accepted          entity=scan:c08f9297-b26d-461b-a42e-01f7ff0acd05 after={'unit_id': 'ae121338-773f-4c9b-999d-edbd2583ec4e', 'override': False, 'work_session_id': '9965a004-fe06-4a8b-bfb1-959873123b0f', 'plan_operation_id': '8900e49b-4b1a-4309-849a-369d9c8807bc'}
  [2026-07-15 10:56:45.164723-04:00] substep.done           entity=substepexecution:afdfd7fd-e1c7-4a49-b479-9fd436d5d500 after={'type': 'inspection', 'unit_id': 'ae121338-773f-4c9b-999d-edbd2583ec4e'}
  [2026-07-15 10:56:45.181683-04:00] substep.done           entity=substepexecution:74af1183-7fde-4f6f-8fda-7082f27ea04f after={'unit_id': 'ae121338-773f-4c9b-999d-edbd2583ec4e', 'signoff_role': 'lead', 'acting_operator_id': 'f0ba782c-ef8e-4e76-acaf-5a255643995c'}
  [2026-07-15 10:56:45.200904-04:00] session.closed         entity=worksession:9965a004-fe06-4a8b-bfb1-959873123b0f after={'reason': 'finished'}
  [2026-07-15 10:56:45.200904-04:00] unit.done              entity=unit:ae121338-773f-4c9b-999d-edbd2583ec4e after={}

## GATE 3 WALK: PASS
