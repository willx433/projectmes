"""Unit tests for app/domain/conditions.py (P2-07 conditional content, DD §7.2)."""
from __future__ import annotations

import pytest

from app.domain.conditions import ConditionError, evaluate, validate

SCHEMA = {"caliber": ["9mm", ".45"], "finish": ["black", "fde"]}


# --------------------------------------------------------------------
# evaluate
# --------------------------------------------------------------------


def test_evaluate_none_or_empty_is_unconditional():
    assert evaluate(None, {"caliber": "9mm"}) is True
    assert evaluate({}, {"caliber": "9mm"}) is True


def test_evaluate_basic_in_true_and_false():
    cond = {"field": "caliber", "in": ["9mm"]}
    assert evaluate(cond, {"caliber": "9mm"}) is True
    assert evaluate(cond, {"caliber": ".45"}) is False


def test_evaluate_all_nesting():
    cond = {
        "all": [
            {"field": "caliber", "in": ["9mm"]},
            {"field": "finish", "in": ["black"]},
        ]
    }
    assert evaluate(cond, {"caliber": "9mm", "finish": "black"}) is True
    assert evaluate(cond, {"caliber": "9mm", "finish": "fde"}) is False


def test_evaluate_any_nesting():
    cond = {
        "any": [
            {"field": "caliber", "in": ["9mm"]},
            {"field": "caliber", "in": [".45"]},
        ]
    }
    assert evaluate(cond, {"caliber": "9mm"}) is True
    assert evaluate(cond, {"caliber": ".45"}) is True
    assert evaluate(cond, {"caliber": "10mm"}) is False


def test_evaluate_not_nesting():
    cond = {"not": {"field": "caliber", "in": ["9mm"]}}
    assert evaluate(cond, {"caliber": "9mm"}) is False
    assert evaluate(cond, {"caliber": ".45"}) is True


def test_evaluate_deep_nesting():
    cond = {
        "all": [
            {"field": "caliber", "in": ["9mm"]},
            {"not": {"field": "finish", "in": ["fde"]}},
        ]
    }
    assert evaluate(cond, {"caliber": "9mm", "finish": "black"}) is True
    assert evaluate(cond, {"caliber": "9mm", "finish": "fde"}) is False


def test_evaluate_unknown_field_is_false_not_raise(caplog):
    cond = {"field": "grip_size", "in": ["large"]}
    assert evaluate(cond, {"caliber": "9mm"}) is False


def test_evaluate_never_raises_on_malformed_shape():
    # missing "in", unexpected keys, garbage nesting -- must not raise
    assert evaluate({"field": "caliber"}, {"caliber": "9mm"}) is False
    assert evaluate({"bogus": "shape"}, {"caliber": "9mm"}) is False
    assert evaluate({"all": "not-a-list"}, {"caliber": "9mm"}) is False
    assert evaluate({"not": {"bogus": "x"}}, {"caliber": "9mm"}) is False


# --------------------------------------------------------------------
# validate
# --------------------------------------------------------------------


def test_validate_none_is_ok():
    validate(None, SCHEMA)
    validate({}, SCHEMA)


def test_validate_ok_basic_and_nested():
    validate({"field": "caliber", "in": ["9mm"]}, SCHEMA)
    validate(
        {"all": [{"field": "caliber", "in": ["9mm"]}, {"field": "finish", "in": ["black"]}]},
        SCHEMA,
    )
    validate({"not": {"field": "caliber", "in": ["9mm"]}}, SCHEMA)


def test_validate_unknown_field_raises():
    with pytest.raises(ConditionError, match="unknown variant field"):
        validate({"field": "grip_size", "in": ["large"]}, SCHEMA)


def test_validate_unknown_value_raises():
    with pytest.raises(ConditionError, match="unknown value"):
        validate({"field": "caliber", "in": ["10mm"]}, SCHEMA)


def test_validate_malformed_shape_raises():
    with pytest.raises(ConditionError):
        validate({"field": "caliber"}, SCHEMA)  # missing "in"
    with pytest.raises(ConditionError):
        validate({"bogus": "shape"}, SCHEMA)
    with pytest.raises(ConditionError):
        validate({"all": []}, SCHEMA)  # empty list
    with pytest.raises(ConditionError):
        validate({"all": "not-a-list"}, SCHEMA)


def test_validate_recurses_into_nested_errors():
    cond = {
        "all": [
            {"field": "caliber", "in": ["9mm"]},
            {"field": "grip_size", "in": ["x"]},
        ]
    }
    with pytest.raises(ConditionError, match="unknown variant field"):
        validate(cond, SCHEMA)
