"""Database session management for snap-dashboard."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from snap_dashboard.db.models import Base


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
    """Context manager that yields a SQLAlchemy session."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
