"""Admin routes — allowlist management."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.auth import get_current_user
from snap_dashboard.db.models import AllowlistedUser, User
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _require_admin(request: Request) -> dict | RedirectResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)
    if not user["is_admin"]:
        return RedirectResponse(url="/", status_code=302)
    return user


@router.get("/admin", response_class=HTMLResponse)
async def admin_index(request: Request) -> HTMLResponse:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    with get_session() as session:
        allowlist = (
            session.query(AllowlistedUser)
            .order_by(AllowlistedUser.added_at.desc())
            .all()
        )
        allowlist_data = [
            {
                "id": a.id,
                "github_login": a.github_login,
                "added_by": a.added_by,
                "added_at": a.added_at,
                "note": a.note,
            }
            for a in allowlist
        ]

        all_users = session.query(User).order_by(User.created_at).all()
        users_data = [
            {
                "id": u.id,
                "github_login": u.github_login,
                "display_name": u.display_name,
                "avatar_url": u.avatar_url,
                "is_admin": u.is_admin,
                "created_at": u.created_at,
                "last_login": u.last_login,
            }
            for u in all_users
        ]

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "current_user": user,
            "allowlist": allowlist_data,
            "users": users_data,
            "last_run": None,
        },
    )


@router.post("/admin/allowlist/add")
async def allowlist_add(
    request: Request,
    github_login: str = Form(...),
    note: str = Form(default=""),
) -> RedirectResponse:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    github_login = github_login.strip().lower()
    if github_login:
        with get_session() as session:
            existing = session.query(AllowlistedUser).filter_by(github_login=github_login).first()
            if not existing:
                entry = AllowlistedUser(
                    github_login=github_login,
                    added_by=user["github_login"],
                    note=note.strip() or None,
                )
                session.add(entry)

    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/allowlist/remove/{entry_id}")
async def allowlist_remove(request: Request, entry_id: int) -> RedirectResponse:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    with get_session() as session:
        entry = session.query(AllowlistedUser).get(entry_id)
        if entry:
            session.delete(entry)

    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/users/{user_id}/toggle-admin")
async def toggle_admin(request: Request, user_id: int) -> RedirectResponse:
    current = _require_admin(request)
    if isinstance(current, RedirectResponse):
        return current

    # Don't allow self-demotion
    if user_id == current["id"]:
        return RedirectResponse(url="/admin", status_code=303)

    with get_session() as session:
        target = session.query(User).get(user_id)
        if target:
            target.is_admin = not target.is_admin

    return RedirectResponse(url="/admin", status_code=303)
