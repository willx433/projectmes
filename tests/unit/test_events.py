"""Unit tests for app/domain/events.py (P3-13 append-only event/audit)."""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.domain import events
from app.domain.models_floor import Event


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    # ponytail: Event.id is BigInteger for Postgres bigserial parity, but
    # sqlite only aliases a PK column to rowid (autoincrement) when its
    # declared type is exactly INTEGER -- BIGINT doesn't qualify. Swap the
    # type for this CREATE TABLE only (sqlite doesn't enforce the events->
    # operators/stations FKs anyway), then restore it -- production model
    # untouched.
    original_type = Event.__table__.c.id.type
    Event.__table__.c.id.type = Integer()
    try:
        Event.__table__.create(eng)
    finally:
        Event.__table__.c.id.type = original_type
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def test_emit_writes_row_with_actor_verb_before_after(session):
    actor_id = uuid.uuid4()
    station_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    row = events.emit(
        session,
        "unit.moved",
        entity=("unit", entity_id),
        actor_id=actor_id,
        station_id=station_id,
        before={"status": "at_station"},
        after={"status": "in_transit"},
    )
    session.flush()

    fetched = session.get(Event, row.id)
    assert fetched.verb == "unit.moved"
    assert fetched.entity_kind == "unit"
    assert fetched.entity_id == entity_id
    assert fetched.actor_id == actor_id
    assert fetched.station_id == station_id
    assert fetched.before == {"status": "at_station"}
    assert fetched.after == {"status": "in_transit"}


def test_emit_does_not_commit(session):
    events.emit(session, "auth.login", entity=("operator", uuid.uuid4()))
    assert session.new  # still pending, caller owns the transaction


def test_emit_unknown_verb_raises(session):
    with pytest.raises(ValueError):
        events.emit(session, "unit.teleported", entity=("unit", uuid.uuid4()))


def test_emit_serializes_uuid_and_datetime(session):
    nested_id = uuid.uuid4()
    when = dt.datetime(2026, 7, 14, 12, 0, 0)
    row = events.emit(
        session,
        "measurement.recorded",
        entity=("measurement", uuid.uuid4()),
        after={"recorded_at": when, "ref": nested_id, "nested": {"id": nested_id}},
    )
    session.flush()

    assert row.after["recorded_at"] == str(when)
    assert row.after["ref"] == str(nested_id)
    assert row.after["nested"]["id"] == str(nested_id)


def test_emit_entity_from_orm_object(session):
    class Unit:
        def __init__(self, id_):
            self.id = id_

    unit_id = uuid.uuid4()
    row = events.emit(session, "unit.done", entity=Unit(unit_id))
    assert row.entity_kind == "unit"
    assert row.entity_id == unit_id


def test_timeline_orders_chronologically_and_filters(session):
    unit_a = uuid.uuid4()
    unit_b = uuid.uuid4()
    events.emit(session, "unit.moved", entity=("unit", unit_a))
    events.emit(session, "unit.moved", entity=("unit", unit_b))
    events.emit(session, "unit.done", entity=("unit", unit_a))
    session.flush()

    all_rows = events.timeline(session)
    assert [r.id for r in all_rows] == sorted(r.id for r in all_rows)

    unit_a_rows = events.timeline(session, unit_id=unit_a)
    assert len(unit_a_rows) == 2
    assert all(r.entity_id == unit_a for r in unit_a_rows)

    kind_rows = events.timeline(session, entity_kind="unit", limit=1)
    assert len(kind_rows) == 1


def test_timeline_rejects_entity_id_and_unit_id_both(session):
    with pytest.raises(ValueError):
        events.timeline(session, entity_id=uuid.uuid4(), unit_id=uuid.uuid4())


def test_module_exposes_no_mutation_of_existing_rows():
    # events.py is append-only by construction: no update/delete function.
    assert not hasattr(events, "update_event")
    assert not hasattr(events, "delete_event")
    # Only functions *defined in* events.py count as its public API --
    # imported names (Event, Session, select, ...) aren't writers of it.
    public_functions = {
        name
        for name in dir(events)
        if not name.startswith("_")
        and callable(getattr(events, name))
        and getattr(getattr(events, name), "__module__", None) == events.__name__
    }
    assert public_functions == {"emit", "timeline"}
