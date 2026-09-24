"""Database session management for snap-dashboard."""

from __future__ import annotations

import functools
import logging
import os
import random
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Generator, TypeVar

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from snap_dashboard.db.models import Base

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable)

# How many times to retry a whole db-touching operation that fails with
# "database is locked" before giving up. This remains as a defense-in-depth
# backstop; the primary fix is _db_lock below.
_MAX_LOCK_RETRIES = 5

# sqlite only allows one writer at a time; even with WAL + busy_timeout,
# concurrent commits from different threads in this process were racing
# and losing (see the retry_on_db_lock() callers). Since snap-dashboard
# runs as a single process (one `serve` app, no multi-worker uvicorn), we
# can fully avoid that race by serializing all get_session() usage
# in-process with a lock, rather than reactively retrying after a
# collision at the sqlite level. This also closes a check-then-insert
# TOCTOU race between concurrent sessions (e.g. two release_scanner
# workers both seeing "not found" before either inserts).
_db_lock = threading.RLock()


def get_db_path() -> Path:
    """Return the SQLite database file path.

    Uses $SNAP_COMMON (shared across snap revisions), not $SNAP_DATA
    (per-revision) — see snap_dashboard.config for why.
    """
    snap_common = os.environ.get("SNAP_COMMON")
    if snap_common:
        return Path(snap_common) / "snap-dashboard.db"
    db_env = os.environ.get("SNAP_DASHBOARD_DB")
    if db_env:
        return Path(db_env)
    default_dir = Path.home() / ".local" / "share" / "snap-dashboard"
    default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir / "snap-dashboard.db"


def _make_engine():
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        # `timeout` sets sqlite3's busy_timeout (seconds) for this connection
        # so concurrent writers (background collection, agent scheduling,
        # request handlers) block-and-retry instead of immediately raising
        # "database is locked".
        connect_args={"check_same_thread": False, "timeout": 30},
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        # WAL lets readers and a single writer proceed concurrently instead
        # of the default rollback-journal mode, which takes a whole-database
        # lock for any write and is why multiple background agents writing
        # at once produced "database is locked" errors.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    """Create all database tables if they do not exist, and run lightweight migrations."""
    Base.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """Apply additive schema changes that create_all cannot handle on existing tables."""
    import sqlalchemy

    migrations = [
        # Phase 1 (original)
        "ALTER TABLE test_runs ADD COLUMN architecture VARCHAR(32)",
        # Multi-tenant phase: user_id columns on existing tables
        "ALTER TABLE snaps ADD COLUMN user_id INTEGER REFERENCES users(id)",
        "ALTER TABLE collection_runs ADD COLUMN user_id INTEGER REFERENCES users(id)",
        "ALTER TABLE test_runs ADD COLUMN user_id INTEGER REFERENCES users(id)",
        # Agentic phase: new UserConfig columns
        "ALTER TABLE user_configs ADD COLUMN lemonade_server_url VARCHAR(500)",
        "ALTER TABLE user_configs ADD COLUMN lemonade_model VARCHAR(255)",
        "ALTER TABLE user_configs ADD COLUMN bot_github_token TEXT",
        "ALTER TABLE user_configs ADD COLUMN bot_github_login VARCHAR(255)",
        "ALTER TABLE user_configs ADD COLUMN agent_interval_hours INTEGER DEFAULT 4",
        "ALTER TABLE user_configs ADD COLUMN auto_merge BOOLEAN DEFAULT 0",
        "ALTER TABLE user_configs ADD COLUMN auto_promote BOOLEAN DEFAULT 0",
        "ALTER TABLE user_configs ADD COLUMN auto_promote_confidence FLOAT DEFAULT 0.85",
        # Stale rebuild settings
        "ALTER TABLE user_configs ADD COLUMN auto_rebuild_stale BOOLEAN DEFAULT 0",
        "ALTER TABLE user_configs ADD COLUMN stale_build_days INTEGER DEFAULT 30",
        # Remote runner phase (R1+)
        "ALTER TABLE user_configs ADD COLUMN prefer_remote_runner BOOLEAN DEFAULT 0",
        "ALTER TABLE user_configs ADD COLUMN runner_job_timeout_minutes INTEGER DEFAULT 10",
        "ALTER TABLE test_runs ADD COLUMN dispatch_target VARCHAR(32) DEFAULT 'github_actions'",
        "ALTER TABLE test_runs ADD COLUMN runner_id INTEGER REFERENCES runners(id)",
        "ALTER TABLE test_runs ADD COLUMN priority INTEGER DEFAULT 0",
        "ALTER TABLE test_runs ADD COLUMN cancel_requested BOOLEAN DEFAULT 0",
        # Copilot cloud agent delegation (CI-fix, upstream maintenance, fleet normalize)
        "ALTER TABLE user_configs ADD COLUMN auto_fix_ci_failures BOOLEAN DEFAULT 0",
        "ALTER TABLE user_configs ADD COLUMN auto_maintain_upstream BOOLEAN DEFAULT 0",
        "ALTER TABLE user_configs ADD COLUMN fleet_normalization_enabled BOOLEAN DEFAULT 0",
        # Pluggable coding-task backend selection — see agents/coding_backend.py.
        "ALTER TABLE user_configs ADD COLUMN coding_task_backend VARCHAR(32) DEFAULT 'copilot_cloud_agent'",
        "ALTER TABLE user_configs ADD COLUMN external_coding_api_key TEXT",
        "ALTER TABLE user_configs ADD COLUMN external_coding_api_base_url VARCHAR(500)",
        "ALTER TABLE user_configs ADD COLUMN external_coding_api_model VARCHAR(255)",
        # Explicit Lemonade backend selection (embedded default vs. system
        # lemonade-server) — see lemonade/client.py.
        "ALTER TABLE user_configs ADD COLUMN lemonade_backend VARCHAR(16) DEFAULT 'embedded'",
        "ALTER TABLE user_configs ADD COLUMN lemonade_api_key TEXT",
        # Records which repo a test run was actually dispatched against — the
        # snap's own packaging repo when it has colocated YARF tests, or the
        # legacy shared testing repo as a fallback. See testing/orchestrator.py.
        "ALTER TABLE test_runs ADD COLUMN repo VARCHAR(500)",
        # Captured runner-side stdout/stderr/traceback so failures can be
        # debugged from the web UI. See automated_ken_runner.runner._execute_job.
        "ALTER TABLE test_runs ADD COLUMN log_output TEXT",
        # LLM vision-review outcome, always recorded independent of whether a
        # VersionBumpPR exists — see agents/test_run_auto_promoter.py.
        "ALTER TABLE test_runs ADD COLUMN review_decision VARCHAR(32)",
        "ALTER TABLE test_runs ADD COLUMN review_confidence FLOAT",
        "ALTER TABLE test_runs ADD COLUMN review_reasoning TEXT",
        # Multi-architecture testing: links sibling per-arch TestRuns to the
        # same VersionBumpPR so all architectures can be gated and promoted
        # together. See agents/pr_monitor.py and agents/stable_promoter.py.
        "ALTER TABLE test_runs ADD COLUMN version_bump_pr_id INTEGER REFERENCES version_bump_prs(id)",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(sqlalchemy.text(sql))
                conn.commit()
            except Exception:
                pass  # column already exists — safe to ignore


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager that yields a SQLAlchemy session.

    Serializes on `_db_lock` for the lifetime of the session — see the
    comment on `_db_lock` for why: this process is single-threaded from
    sqlite's point of view even though Python has many threads, so we
    make that explicit instead of racing at the sqlite layer.
    """
    with _db_lock:
        session = SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _is_db_locked(exc: OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


def retry_on_db_lock(max_attempts: int = _MAX_LOCK_RETRIES, base_delay: float = 0.2):
    """Retry a whole function from scratch if it fails with sqlite's
    "database is locked" error.

    Important: this re-runs the *entire* decorated function (including
    opening a brand-new `get_session()`), not just a failed commit. A
    failed flush/commit leaves the SQLAlchemy Session's pending objects
    expunged, so retrying only `session.commit()` after a `rollback()`
    would silently drop the write; re-running the whole operation from a
    fresh session is the only way to retry that is actually correct. Only
    use this to decorate idempotent operations (e.g. a single UPDATE, or a
    check-then-insert-if-missing) where re-running on retry is safe.
    """

    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except OperationalError as exc:
                    if not _is_db_locked(exc) or attempt == max_attempts - 1:
                        raise
                    delay = (base_delay * (2**attempt)) + random.uniform(0, base_delay)
                    logger.warning(
                        "%s: hit 'database is locked', retrying (attempt %d/%d) after %.2fs",
                        fn.__qualname__,
                        attempt + 1,
                        max_attempts,
                        delay,
                    )
                    time.sleep(delay)

        return wrapper  # type: ignore[return-value]

    return decorator
