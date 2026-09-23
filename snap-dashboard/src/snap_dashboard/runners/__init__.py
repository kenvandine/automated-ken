"""Shared helpers for remote-runner enrollment/auth tokens."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

HEARTBEAT_STALE_SECONDS = 90


def generate_token(nbytes: int = 32) -> str:
    """Return a new random URL-safe token."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Return the sha256 hex digest of a token, for at-rest storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def effective_status(runner) -> str:
    """Return a Runner's live status, downgrading to 'offline' if its heartbeat is stale.

    Computed lazily at read time rather than persisted, so there's no
    extra background job needed just to notice a runner vanished.
    """
    if runner.revoked_at is not None:
        return "revoked"
    if runner.last_heartbeat_at is None:
        return runner.status or "enrolling"
    last = runner.last_heartbeat_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - last
    if age > timedelta(seconds=HEARTBEAT_STALE_SECONDS):
        return "offline"
    return runner.status or "idle"
