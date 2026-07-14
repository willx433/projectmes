"""Phase 5 — Quality / NCR. QA-1..QA-7."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..deps import get_conn, render
from ..services import quality

router = APIRouter()


@router.get("/quality")
def ncr_list(request: Request, conn=Depends(get_conn), error: str = "", msg: str = ""):
    rows = db.query(conn,
        "SELECT n.*, j.jb2_job_number FROM ncrs n JOIN jobs j ON j.id=n.job_id ORDER BY n.id DESC")
    return render(request, "quality.html", nav="quality", ncrs=rows,
                  dispositions=quality.DISPOSITIONS, error=error, msg=msg)


@router.post("/jobs/{job_id}/ncr")
def create(job_id: int, step_number: int = Form(...), qty: float = Form(...),
           defect_type: str = Form(...), reported_by: str = Form(...),
           detail: str = Form(""), part_number: str = Form(""),
           redirect: str = Form(""), conn=Depends(get_conn)):
    try:
        quality.create_ncr(conn, job_id, step_number, qty, defect_type, reported_by,
                           part_number or None, detail or None)
    except ValueError as e:
        url = redirect or f"/jobs/{job_id}"
        return RedirectResponse(f"{url}{'&' if '?' in url else '?'}error={str(e).replace(' ', '+')}",
                                status_code=303)
    return RedirectResponse(redirect or f"/jobs/{job_id}", status_code=303)


@router.post("/ncr/{ncr_id}/review")
def review(ncr_id: int, conn=Depends(get_conn)):
    try:
        quality.set_state(conn, ncr_id, "under_review")
    except ValueError as e:
        return RedirectResponse(f"/quality?error={str(e).replace(' ', '+')}", status_code=303)
    return RedirectResponse("/quality", status_code=303)


@router.post("/ncr/{ncr_id}/disposition")
def disposition(ncr_id: int, disposition: str = Form(...), reviewer: str = Form(...),
                justification: str = Form(...), rework_to_step: str = Form(""),
                conn=Depends(get_conn)):
    try:
        quality.disposition(conn, ncr_id, disposition, reviewer, justification,
                            int(rework_to_step) if rework_to_step else None)
    except ValueError as e:
        return RedirectResponse(f"/quality?error={str(e).replace(' ', '+')}", status_code=303)
    return RedirectResponse("/quality?msg=Dispositioned", status_code=303)


@router.post("/ncr/{ncr_id}/correct")
def correct(ncr_id: int, qty: float = Form(...), defect_type: str = Form(...),
            reported_by: str = Form(...), detail: str = Form(""), conn=Depends(get_conn)):
    quality.correct(conn, ncr_id, qty=qty, defect_type=defect_type, reported_by=reported_by,
                    detail=detail or None)
    return RedirectResponse("/quality?msg=Correction+filed", status_code=303)
