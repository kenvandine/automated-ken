"""SQLAlchemy ORM models for snap-dashboard."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Snap(Base):
    __tablename__ = "snaps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    publisher = Column(String(255), nullable=True)
    manually_added = Column(Boolean, default=False, nullable=False)
    packaging_repo = Column(Text, nullable=True)
    upstream_repo = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)

    channel_map = relationship(
        "ChannelMap", back_populates="snap", cascade="all, delete-orphan"
    )
    issues = relationship(
        "Issue", back_populates="snap", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Snap name={self.name!r}>"


class ChannelMap(Base):
    __tablename__ = "channel_map"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snap_id = Column(Integer, ForeignKey("snaps.id", ondelete="CASCADE"), nullable=False)
    channel = Column(String(64), nullable=False)
    architecture = Column(String(64), nullable=False)
    revision = Column(Integer, nullable=True)
    version = Column(String(128), nullable=True)
    released_at = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime, default=_now, nullable=False)

    snap = relationship("Snap", back_populates="channel_map")

    def __repr__(self) -> str:
        return f"<ChannelMap snap_id={self.snap_id} channel={self.channel!r} arch={self.architecture!r}>"


class Issue(Base):
    __tablename__ = "issues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snap_id = Column(Integer, ForeignKey("snaps.id", ondelete="CASCADE"), nullable=False)
    repo_url = Column(Text, nullable=False)
    issue_number = Column(Integer, nullable=False)
    title = Column(Text, nullable=True)
    state = Column(String(32), nullable=True)
    type = Column(String(16), nullable=False)  # 'issue' or 'pr'
    url = Column(Text, nullable=True)
    author = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime, default=_now, nullable=False)

    snap = relationship("Snap", back_populates="issues")

    def __repr__(self) -> str:
        return f"<Issue snap_id={self.snap_id} #{self.issue_number} type={self.type!r}>"


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, default=_now, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(32), nullable=False, default="running")
    error_msg = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<CollectionRun id={self.id} status={self.status!r}>"


class TestRun(Base):
    """Tracks a YARF test run triggered from the dashboard."""

    __tablename__ = "test_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snap_name = Column(String(255), nullable=False)
    from_channel = Column(String(64), nullable=False)  # 'candidate', 'beta', 'edge'
    version = Column(String(128), nullable=True)
    revision = Column(Integer, nullable=True)
    # statuses: pending, triggered, running, passed, failed, error, promoted
    status = Column(String(32), nullable=False, default="pending")
    gh_run_id = Column(String(128), nullable=True)  # GitHub Actions run ID
    pr_number = Column(Integer, nullable=True)
    pr_url = Column(Text, nullable=True)
    pr_body = Column(Text, nullable=True)
    triggered_by = Column(String(64), nullable=True)  # 'auto', 'manual', or 'external'
    started_at = Column(DateTime, default=_now, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    promoted = Column(Boolean, default=False, nullable=False)
    promoted_at = Column(DateTime, nullable=True)
    error_msg = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<TestRun id={self.id} snap={self.snap_name!r} status={self.status!r}>"
