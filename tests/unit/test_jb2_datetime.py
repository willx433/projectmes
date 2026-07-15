"""Regression guard for G3-D1 (Gate 3): to_utc must CONVERT aware non-UTC
datetimes to UTC, not just relabel them, or format_jb2_datetime stamps 'Z'
on a non-UTC wall-clock and JB2 time tickets carry phantom labor hours.

The bug only showed on Postgres (returns timestamptz in the session zone);
SQLite round-trips naive, which masked it in every other test.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.sync.engine import format_jb2_datetime, to_utc

EDT = timezone(timedelta(hours=-4))


def test_to_utc_converts_aware_non_utc():
    aware_edt = datetime(2026, 7, 15, 8, 10, 6, tzinfo=EDT)  # == 12:10:06 UTC
    got = to_utc(aware_edt)
    assert got.utcoffset() == timedelta(0)
    assert (got.hour, got.minute) == (12, 10)


def test_to_utc_stamps_naive_as_utc():
    naive = datetime(2026, 7, 15, 12, 10, 6)
    assert to_utc(naive) == datetime(2026, 7, 15, 12, 10, 6, tzinfo=timezone.utc)


def test_format_same_instant_same_string_regardless_of_input_zone():
    utc = datetime(2026, 7, 15, 12, 10, 6, tzinfo=timezone.utc)
    edt = utc.astimezone(EDT)  # same instant, different tz label
    assert format_jb2_datetime(utc) == format_jb2_datetime(edt) == "2026-07-15T12:10:06Z"


if __name__ == "__main__":
    test_to_utc_converts_aware_non_utc()
    test_to_utc_stamps_naive_as_utc()
    test_format_same_instant_same_string_regardless_of_input_zone()
    print("OK")
