"""Database session management for snap-dashboard."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from snap_dashboard.db.models import Base


def get_db_path() -> Path:
    """Return the SQLite database file path."""
    snap_data = os.environ.get("SNAP_DATA")
    if snap_data:
        return Path(snap_data) / "snap-dashboard.db"
    db_env = os.environ.get("SNAP_DASHBOARD_DB")
    if db_env:
        return Path(db_env)
    default_dir = Path.home() / ".local" / "share" / "snap-dashboard"
    default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir / "snap-dashboard.db"


def _make_engine():
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
    )


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    """Create all database tables if they do not exist, and run lightweight migrations."""
    Base.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """Apply additive schema changes that create_all cannot handle on existing tables."""
    migrations = [
        "ALTER TABLE test_runs ADD COLUMN architecture VARCHAR(32)",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(__import__("sqlalchemy").text(sql))
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
