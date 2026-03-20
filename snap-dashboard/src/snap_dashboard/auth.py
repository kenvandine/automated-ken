"""Authentication helpers for snap-dashboard.

User identity is stored in a signed cookie session (via starlette SessionMiddleware).
GitHub OAuth is used for login.
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import RedirectResponse

from snap_dashboard.config import Config
from snap_dashboard.db.models import User, UserConfig
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)


def get_current_user(request: Request) -> dict | None:
    """Return the logged-in user as a plain dict, or None if not authenticated."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    with get_session() as session:
        user = session.query(User).get(user_id)
        if not user:
            request.session.clear()
            return None
        return {
            "id": user.id,
            "github_login": user.github_login,
            "display_name": user.display_name or user.github_login,
            "avatar_url": user.avatar_url,
            "is_admin": user.is_admin,
        }


def login_required(request: Request) -> dict | RedirectResponse:
    """Return user dict or a redirect to /auth/login."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)
    return user


def get_user_config(user_id: int) -> "UserConfigView":
    """Return a UserConfigView for the given user_id."""
    with get_session() as session:
        uc = session.query(UserConfig).filter_by(user_id=user_id).first()
        if uc:
            return UserConfigView(
                publisher=uc.publisher or "",
                github_token=uc.github_token or "",
                testing_repo=uc.testing_repo or "",
                snapcraft_macaroon=uc.snapcraft_macaroon or "",
                auto_test=uc.auto_test,
                collect_interval_hours=uc.collect_interval_hours,
            )
        return UserConfigView()


class UserConfigView:
    """A lightweight view of per-user settings, API-compatible with Config."""

    def __init__(
        self,
        publisher: str = "",
        github_token: str = "",
        testing_repo: str = "",
        snapcraft_macaroon: str = "",
        auto_test: bool = False,
        collect_interval_hours: int = 6,
    ) -> None:
        self.publisher = publisher
        self.github_token = github_token
        self.testing_repo = testing_repo
        self.snapcraft_macaroon = snapcraft_macaroon
        self.auto_test = auto_test
        self.collect_interval_hours = collect_interval_hours

    def to_config(self) -> Config:
        """Return a Config object populated from this user config."""
        from snap_dashboard.config import Config

        return Config(
            github_token=self.github_token,
            publisher=self.publisher,
            collect_interval_hours=self.collect_interval_hours,
            testing_repo=self.testing_repo,
            auto_test=self.auto_test,
        )
