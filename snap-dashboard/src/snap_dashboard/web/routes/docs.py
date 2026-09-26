"""Help / documentation route."""

from __future__ import annotations


from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from snap_dashboard.auth import get_current_user
from snap_dashboard.web.templating import templates

router = APIRouter()


@router.get("/docs", response_class=HTMLResponse)
async def docs_index(request: Request, section: str = "") -> HTMLResponse:
    user = get_current_user(request)
    return templates.TemplateResponse(
        request,
        "docs.html",
        {
            "current_user": user,
            "last_run": None,
            "active_section": section or "overview",
        },
    )
