"""GitHub OAuth authentication routes."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.config import get_config
from snap_dashboard.db.models import AllowlistedUser, User, UserConfig
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_GH_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GH_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GH_USER_URL = "https://api.github.com/user"


@router.get("/auth/login", response_class=HTMLResponse)
async def login(request: Request) -> HTMLResponse:
    """Show the login page or redirect to GitHub OAuth."""
    # Already logged in
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)

    config = get_config()
    if not config.github_client_id:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": (
                    "GitHub OAuth is not configured. "
                    "Set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET in config."
                ),
                "oauth_url": None,
            },
        )

    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    oauth_url = (
        f"{_GH_AUTHORIZE_URL}"
        f"?client_id={config.github_client_id}"
        f"&scope=read:user"
        f"&state={state}"
    )
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None, "oauth_url": oauth_url},
    )


@router.get("/auth/callback")
async def oauth_callback(request: Request) -> HTMLResponse:
    """Handle GitHub OAuth callback."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    if not code or state != request.session.get("oauth_state"):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Invalid OAuth state. Please try again.",
                "oauth_url": None,
            },
            status_code=400,
        )

    request.session.pop("oauth_state", None)
    config = get_config()

    # Exchange code for access token
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                _GH_TOKEN_URL,
                data={
                    "client_id": config.github_client_id,
                    "client_secret": config.github_client_secret,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
        token_data = resp.json()
        access_token = token_data.get("access_token", "")
        if not access_token:
            raise ValueError(f"No access_token in response: {token_data}")
    except Exception as exc:
        logger.error("OAuth token exchange failed: %s", exc)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "GitHub OAuth failed. Please try again.",
                "oauth_url": None,
            },
            status_code=500,
        )

    # Fetch GitHub user info
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                _GH_USER_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
        gh_user = resp.json()
        github_login = gh_user.get("login", "")
        github_id = gh_user.get("id", 0)
        display_name = gh_user.get("name") or github_login
        avatar_url = gh_user.get("avatar_url", "")
        if not github_login:
            raise ValueError("Empty GitHub login")
    except Exception as exc:
        logger.error("Failed to fetch GitHub user info: %s", exc)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Could not fetch your GitHub profile. Please try again.",
                "oauth_url": None,
            },
            status_code=500,
        )

    with get_session() as session:
        # Check if this is the very first user (becomes admin automatically)
        total_users = session.query(User).count()
        is_first_user = total_users == 0

        # Check allowlist (skip for first user)
        if not is_first_user:
            allowed = session.query(AllowlistedUser).filter_by(github_login=github_login).first()
            if not allowed:
                return templates.TemplateResponse(
                    "login.html",
                    {
                        "request": request,
                        "error": (
                            f"Your GitHub account ({github_login!r}) is not on the allowlist. "
                            "Ask an administrator to add you."
                        ),
                        "oauth_url": None,
                    },
                    status_code=403,
                )

        # Find or create the User record
        user = session.query(User).filter_by(github_id=github_id).first()
        if user is None:
            user = User(
                github_login=github_login,
                github_id=github_id,
                display_name=display_name,
                avatar_url=avatar_url,
                is_admin=is_first_user,
            )
            session.add(user)
            session.flush()

            # Create empty UserConfig
            uc = UserConfig(user_id=user.id)
            session.add(uc)
            session.flush()

            # First user: claim any orphaned (NULL user_id) data
            if is_first_user:
                _claim_orphaned_data(session, user.id)

            # First user: add themselves to allowlist
            if is_first_user:
                allowlist_entry = AllowlistedUser(
                    github_login=github_login,
                    added_by=github_login,
                )
                session.add(allowlist_entry)
        else:
            user.display_name = display_name
            user.avatar_url = avatar_url
            user.last_login = datetime.now(timezone.utc)

        user_id = user.id

    request.session["user_id"] = user_id
    next_url = request.session.pop("next", "/")
    return RedirectResponse(url=next_url, status_code=302)


@router.post("/auth/logout")
async def logout(request: Request) -> RedirectResponse:
    """Clear session and redirect to login."""
    request.session.clear()
    return RedirectResponse(url="/auth/login", status_code=302)


def _claim_orphaned_data(session, user_id: int) -> None:
    """Assign all NULL user_id rows to the first admin user."""
    import sqlalchemy

    tables = ["snaps", "collection_runs", "test_runs"]
    for table in tables:
        try:
            session.execute(
                sqlalchemy.text(
                    f"UPDATE {table} SET user_id = :uid WHERE user_id IS NULL"
                ),
                {"uid": user_id},
            )
        except Exception as exc:
            logger.warning("Could not claim orphaned data in %s: %s", table, exc)
