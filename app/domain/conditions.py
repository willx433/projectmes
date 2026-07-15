"""Conditional-content evaluation (DD §7.2, §4.6).

Substeps carry an optional ``condition`` jsonb column (see
``app.domain.models_library.Substep.condition``) evaluated against a work
order's variant values (e.g. ``{"caliber": "9mm"}``) at plan generation.

Condition shape (recursive):
    {"field": "caliber", "in": ["9mm"]}
    {"all": [cond, ...]}
    {"any": [cond, ...]}
    {"not": cond}
``None``/``{}`` is unconditional (always true).

Two entry points:
- ``validate`` — author-time (builder UI, P2-04). Raises ``ConditionError``
  with a human message on unknown field/value or malformed shape.
- ``evaluate`` — floor-time (plan generation). Never raises (PDD P1: fail
  closed, no crash) — an unknown field or malformed condition just logs a
  warning and evaluates to ``False``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("app.domain.conditions")


class ConditionError(Exception):
    """Malformed condition or unknown field/value, raised at author time."""


def _sub_conditions(condition: dict, key: str) -> list:
    sub = condition[key]
    if not isinstance(sub, list) or not sub:
        raise ConditionError(f"'{key}' must be a non-empty list of conditions")
    return sub


def validate(condition: dict | None, variant_schema: dict | None) -> None:
    """Raise ``ConditionError`` if ``condition`` is malformed or references a
    field/value not in ``variant_schema`` (``{field: [allowed values]}``)."""
    if not condition:
        return
    if not isinstance(condition, dict):
        raise ConditionError(f"condition must be an object, got {type(condition).__name__}")

    keys = set(condition.keys())

    if keys == {"field", "in"}:
        field = condition["field"]
        values = condition["in"]
        if not isinstance(field, str) or not field:
            raise ConditionError("'field' must be a non-empty string")
        if not isinstance(values, list) or not values:
            raise ConditionError("'in' must be a non-empty list")
        schema = variant_schema or {}
        if field not in schema:
            raise ConditionError(f"unknown variant field '{field}'")
        allowed = schema[field] or []
        unknown = [v for v in values if v not in allowed]
        if unknown:
            raise ConditionError(f"unknown value(s) {unknown} for field '{field}'")
        return

    if keys == {"all"} or keys == {"any"}:
        for sub in _sub_conditions(condition, next(iter(keys))):
            validate(sub, variant_schema)
        return

    if keys == {"not"}:
        validate(condition["not"], variant_schema)
        return

    raise ConditionError(f"malformed condition: unexpected keys {sorted(keys)}")


def _eval(condition: dict, variant_values: dict) -> bool:
    keys = set(condition.keys())

    if keys == {"field", "in"}:
        field = condition["field"]
        values = condition["in"]
        if field not in variant_values:
            logger.warning(
                "condition_unknown_field", extra={"field": field, "condition": condition}
            )
            return False
        return variant_values[field] in values

    if keys == {"all"}:
        return all(_eval(sub, variant_values) for sub in _sub_conditions(condition, "all"))

    if keys == {"any"}:
        return any(_eval(sub, variant_values) for sub in _sub_conditions(condition, "any"))

    if keys == {"not"}:
        return not _eval(condition["not"], variant_values)

    raise ConditionError(f"malformed condition: unexpected keys {sorted(keys)}")


def evaluate(condition: dict | None, variant_values: dict) -> bool:
    """True/False for whether ``condition`` holds against ``variant_values``.
    ``None``/empty condition is unconditional (True). Never raises — any
    malformed shape or eval-time surprise is logged and treated as False
    (fail closed rather than blocking the floor)."""
    if not condition:
        return True
    try:
        return _eval(condition, variant_values or {})
    except Exception:
        logger.warning(
            "condition_eval_failed", extra={"condition": condition}, exc_info=True
        )
        return False
