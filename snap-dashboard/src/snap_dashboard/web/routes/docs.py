"""Help / documentation route."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.auth import get_current_user

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/docs", response_class=HTMLResponse)
async def docs_index(request: Request, section: str = "") -> HTMLResponse:
    user = get_current_user(request)
    return templates.TemplateResponse(
        "docs.html",
        {
            "request": request,
            "current_user": user,
            "last_run": None,
            "active_section": section or "overview",
        },
    )
