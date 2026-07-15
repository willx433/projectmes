"""Binding resolver: JB2 routing step -> published InstructionSet (DD §6.2).

Rung order, first hit wins:
    (a) product, exact JB2 operation code
    (b) product, work center
    (c) product, fuzzy op-description match against published set titles
    (d) unbound -> blocked; caller (P2-10) renders the "no instructions —
        see lead" placeholder step and flags the work order.

Rungs (a)+(b) are ``library.resolve_published``, which already applies the
product-scope-before-global-scope precedence and prefers an exact op-code
hit over a work-center hit within a scope. Rung (c) applies that same
product-then-global scope order for consistency.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import library
from app.domain.models_library import InstructionSet

FUZZY_THRESHOLD = 0.75


@dataclass
class BindingResult:
    instruction_set: InstructionSet | None
    rung: str  # "op_code" | "work_center" | "fuzzy" | "unbound"
    fuzzy: bool = False
    blocked: bool = False
    score: float | None = None  # fuzzy match ratio, else None


@dataclass
class RoutingBindingResult:
    results: list[BindingResult] = field(default_factory=list)

    @property
    def any_blocked(self) -> bool:
        return any(r.blocked for r in self.results)

    @property
    def any_fuzzy(self) -> bool:
        return any(r.fuzzy for r in self.results)


def _fuzzy_match(
    session: Session, product_id: uuid.UUID | None, description: str
) -> tuple[InstructionSet, float] | None:
    scopes = [product_id] if product_id is None else [product_id, None]
    for scope in scopes:
        candidates = session.scalars(
            select(InstructionSet).where(
                InstructionSet.product_id == scope,
                InstructionSet.state == "published",
            )
        ).all()
        best: tuple[InstructionSet, float] | None = None
        for c in candidates:
            ratio = SequenceMatcher(None, description, c.title).ratio()
            if ratio >= FUZZY_THRESHOLD and (best is None or ratio > best[1]):
                best = (c, ratio)
        if best is not None:
            return best
    return None


def bind_routing_step(
    session: Session, product_id: uuid.UUID | None, routing_step
) -> BindingResult:
    """``routing_step`` is a ``JB2OrderRouting`` (or anything with the same
    ``operation_code``/``work_center_code``/``description`` attributes)."""
    op_code = routing_step.operation_code
    work_center = routing_step.work_center_code
    description = routing_step.description

    iset = library.resolve_published(
        session, product_id, op_code=op_code, work_center=work_center
    )
    if iset is not None:
        op_codes = iset.operation_match.get("op_codes") or []
        rung = "op_code" if op_code in op_codes else "work_center"
        return BindingResult(instruction_set=iset, rung=rung)

    if description:
        match = _fuzzy_match(session, product_id, description)
        if match is not None:
            iset, ratio = match
            return BindingResult(instruction_set=iset, rung="fuzzy", fuzzy=True, score=ratio)

    return BindingResult(instruction_set=None, rung="unbound", blocked=True)


def bind_full_routing(
    session: Session, product_id: uuid.UUID | None, routing_steps
) -> RoutingBindingResult:
    results = [bind_routing_step(session, product_id, step) for step in routing_steps]
    return RoutingBindingResult(results=results)
