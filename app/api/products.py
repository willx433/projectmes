"""Products + part-map + variant CRUD (P2-02) — API + admin UI, DD §4.6, §5.

A `Product` groups one or more JB2 part numbers (aliases, e.g.
`APOLLO-9-BLK` / `APOLLO-9-FDE`) under one name, with an optional
`variant_schema` (jsonb, e.g. `{"caliber": ["9mm", ".45"], "finish": ["BLK",
"FDE"]}`) that instructions can condition on (P2-07). `ProductPartMap` is the
many-to-one join: each row is one JB2 part number -> one product, optionally
tagged with the concrete `variant_values` that part number represents.

Unmapped part numbers (seen on real `jb2_order_line_items` rows but absent
from `product_part_map`) and unresolved `mapping_exceptions` rows are
never a sync failure (DD §4.6) — they surface here as an admin to-do list.
"""
from __future__ import annotations

import json
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import REPO_ROOT
from app.db import get_session
from app.domain.models_jb2 import JB2OrderLineItem, MappingException
from app.domain.models_library import Product, ProductPartMap

router = APIRouter()
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))


# -- request bodies ------------------------------------------------------------

class ProductIn(BaseModel):
    name: str
    description: str | None = None
    variant_schema: dict | None = None
    active: bool = True


class ProductPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    variant_schema: dict | None = None
    active: bool | None = None


class PartMapIn(BaseModel):
    jb2_part_number: str
    variant_values: dict | None = None


# -- shared helpers -------------------------------------------------------------

def _get_product_or_404(session: Session, product_id: uuid.UUID) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    return product


def _validate_variant_values(product: Product, variant_values: dict | None) -> None:
    if not variant_values:
        return
    allowed = set((product.variant_schema or {}).keys())
    bad = sorted(set(variant_values) - allowed)
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"variant_values key(s) not in product variant_schema: {bad}",
        )


def _part_maps_for(session: Session, product_id: uuid.UUID) -> list[ProductPartMap]:
    return list(
        session.execute(
            select(ProductPartMap)
            .where(ProductPartMap.product_id == product_id)
            .order_by(ProductPartMap.jb2_part_number)
        ).scalars()
    )


def _unmapped_part_numbers(session: Session) -> list[str]:
    mapped = select(ProductPartMap.jb2_part_number)
    return list(
        session.execute(
            select(JB2OrderLineItem.part_number)
            .where(JB2OrderLineItem.part_number.is_not(None))
            .where(JB2OrderLineItem.part_number.not_in(mapped))
            .distinct()
            .order_by(JB2OrderLineItem.part_number)
        ).scalars()
    )


def _unresolved_mapping_exceptions(session: Session, limit: int = 50) -> list[MappingException]:
    return list(
        session.execute(
            select(MappingException)
            .where(MappingException.resolved.is_(False))
            .order_by(MappingException.created_at.desc())
            .limit(limit)
        ).scalars()
    )


def _product_dict(product: Product, part_maps: list[ProductPartMap] | None = None) -> dict:
    return {
        "id": str(product.id),
        "name": product.name,
        "description": product.description,
        "variant_schema": product.variant_schema,
        "active": product.active,
        "part_map": [_map_dict(m) for m in part_maps] if part_maps is not None else None,
    }


def _map_dict(mapping: ProductPartMap) -> dict:
    return {
        "id": str(mapping.id),
        "product_id": str(mapping.product_id),
        "jb2_part_number": mapping.jb2_part_number,
        "variant_values": mapping.variant_values,
    }


# -- JSON API -------------------------------------------------------------------

@router.get("/api/v1/products")
def list_products(session: Session = Depends(get_session)) -> list[dict]:
    products = session.execute(select(Product).order_by(Product.name)).scalars().all()
    return [_product_dict(p, _part_maps_for(session, p.id)) for p in products]


@router.post("/api/v1/products", status_code=201)
def create_product(body: ProductIn, session: Session = Depends(get_session)) -> dict:
    product = Product(**body.model_dump())
    session.add(product)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"product name '{body.name}' already exists")
    return _product_dict(product, [])


@router.get("/api/v1/products/{product_id}")
def get_product(product_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    product = _get_product_or_404(session, product_id)
    return _product_dict(product, _part_maps_for(session, product_id))


@router.patch("/api/v1/products/{product_id}")
def update_product(
    product_id: uuid.UUID, body: ProductPatch, session: Session = Depends(get_session)
) -> dict:
    product = _get_product_or_404(session, product_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(product, key, value)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"product name '{body.name}' already exists")
    return _product_dict(product, _part_maps_for(session, product_id))


@router.delete("/api/v1/products/{product_id}", status_code=204)
def delete_product(
    product_id: uuid.UUID, force: bool = False, session: Session = Depends(get_session)
) -> None:
    product = _get_product_or_404(session, product_id)
    mapping_count = session.execute(
        select(func.count()).select_from(ProductPartMap).where(
            ProductPartMap.product_id == product_id
        )
    ).scalar_one()
    if mapping_count and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"product has {mapping_count} part mapping(s); "
                "pass ?force=true to delete it and its mappings"
            ),
        )
    session.execute(delete(ProductPartMap).where(ProductPartMap.product_id == product_id))
    session.delete(product)
    session.commit()


@router.post("/api/v1/products/{product_id}/part-map", status_code=201)
def create_part_map(
    product_id: uuid.UUID, body: PartMapIn, session: Session = Depends(get_session)
) -> dict:
    product = _get_product_or_404(session, product_id)
    _validate_variant_values(product, body.variant_values)
    mapping = ProductPartMap(
        product_id=product_id,
        jb2_part_number=body.jb2_part_number,
        variant_values=body.variant_values,
    )
    session.add(mapping)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409, detail=f"part number '{body.jb2_part_number}' already mapped"
        )
    return _map_dict(mapping)


@router.delete("/api/v1/part-map/{map_id}", status_code=204)
def delete_part_map(map_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    mapping = session.get(ProductPartMap, map_id)
    if mapping is None:
        raise HTTPException(status_code=404, detail="mapping not found")
    session.delete(mapping)
    session.commit()


@router.get("/api/v1/unmapped-parts")
def unmapped_parts(session: Session = Depends(get_session)) -> dict:
    return {
        "part_numbers": _unmapped_part_numbers(session),
        "mapping_exceptions": [
            {
                "id": str(exc.id),
                "kind": exc.kind,
                "value": exc.value,
                "created_at": exc.created_at.isoformat(),
            }
            for exc in _unresolved_mapping_exceptions(session)
        ],
    }


# -- admin UI (server-rendered, POST-redirect-GET like /admin/health) ----------

def _parse_json_field(raw: str, label: str) -> dict | None:
    """Admin forms carry variant_schema/variant_values as a JSON textarea
    (there's no native HTML control for an arbitrary jsonb object) — parse
    it here so both admin POST handlers raise the same 400 on bad input."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label}: invalid JSON ({exc})")
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail=f"{label}: must be a JSON object")
    return value


@router.get("/admin/products")
def admin_products(request: Request, session: Session = Depends(get_session)):
    products = session.execute(select(Product).order_by(Product.name)).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin/products.html",
        {
            "products": products,
            "unmapped_parts": _unmapped_part_numbers(session),
            "mapping_exceptions": _unresolved_mapping_exceptions(session),
            "error": request.query_params.get("error"),
            "prefill_part_number": request.query_params.get("part_number"),
        },
    )


@router.post("/admin/products")
def admin_create_product(
    name: str = Form(...),
    description: str = Form(""),
    variant_schema: str = Form(""),
    active: bool = Form(False),
    session: Session = Depends(get_session),
):
    try:
        schema = _parse_json_field(variant_schema, "variant_schema")
    except HTTPException as exc:
        return RedirectResponse(f"/admin/products?error={quote(exc.detail)}", status_code=303)

    product = Product(
        name=name, description=description or None, variant_schema=schema, active=active
    )
    session.add(product)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return RedirectResponse(
            f"/admin/products?error={quote(f'product name {name!r} already exists')}",
            status_code=303,
        )
    return RedirectResponse("/admin/products", status_code=303)


@router.post("/admin/products/{product_id}/delete")
def admin_delete_product(
    product_id: uuid.UUID, force: bool = Form(False), session: Session = Depends(get_session)
):
    try:
        delete_product(product_id, force=force, session=session)
    except HTTPException as exc:
        return RedirectResponse(f"/admin/products?error={quote(exc.detail)}", status_code=303)
    return RedirectResponse("/admin/products", status_code=303)


@router.get("/admin/products/{product_id}")
def admin_product_detail(
    product_id: uuid.UUID, request: Request, session: Session = Depends(get_session)
):
    product = _get_product_or_404(session, product_id)
    return templates.TemplateResponse(
        request,
        "admin/product_detail.html",
        {
            "product": product,
            "part_maps": _part_maps_for(session, product_id),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/admin/products/{product_id}/part-map")
def admin_create_part_map(
    product_id: uuid.UUID,
    jb2_part_number: str = Form(...),
    variant_values: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        values = _parse_json_field(variant_values, "variant_values")
        create_part_map(
            product_id, PartMapIn(jb2_part_number=jb2_part_number, variant_values=values), session
        )
    except HTTPException as exc:
        return RedirectResponse(
            f"/admin/products/{product_id}?error={quote(exc.detail)}", status_code=303
        )
    return RedirectResponse(f"/admin/products/{product_id}", status_code=303)


@router.post("/admin/part-map/{map_id}/delete")
def admin_delete_part_map(map_id: uuid.UUID, session: Session = Depends(get_session)):
    mapping = session.get(ProductPartMap, map_id)
    product_id = mapping.product_id if mapping is not None else None
    try:
        delete_part_map(map_id, session=session)
    except HTTPException as exc:
        redirect_to = f"/admin/products/{product_id}" if product_id else "/admin/products"
        return RedirectResponse(f"{redirect_to}?error={quote(exc.detail)}", status_code=303)
    return RedirectResponse(f"/admin/products/{product_id}", status_code=303)
