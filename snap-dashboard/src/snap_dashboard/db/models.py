"""SQLAlchemy ORM models for snap-dashboard."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
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


# ---------------------------------------------------------------------------
# Multi-tenant auth models
# ---------------------------------------------------------------------------


class User(Base):
    """A GitHub-authenticated user of the dashboard."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    github_login = Column(String(255), unique=True, nullable=False)
    github_id = Column(Integer, unique=True, nullable=False)
    display_name = Column(String(255), nullable=True)
    avatar_url = Column(Text, nullable=True)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    last_login = Column(DateTime, default=_now, nullable=False)

    config = relationship(
        "UserConfig", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    snaps = relationship("Snap", back_populates="user", cascade="all, delete-orphan")
    collection_runs = relationship(
        "CollectionRun", back_populates="user", cascade="all, delete-orphan"
    )
    test_runs = relationship(
        "TestRun", back_populates="user", cascade="all, delete-orphan"
    )
    agent_runs = relationship(
        "AgentRun", back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User login={self.github_login!r} admin={self.is_admin}>"


class UserConfig(Base):
    """Per-user settings (publisher, tokens, testing repo, etc.)."""

    __tablename__ = "user_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    publisher = Column(String(255), nullable=True)
    github_token = Column(Text, nullable=True)
    testing_repo = Column(String(500), nullable=True)  # "owner/repo"
    snapcraft_macaroon = Column(Text, nullable=True)
    auto_test = Column(Boolean, default=False, nullable=False)
    collect_interval_hours = Column(Integer, default=6, nullable=False)

    # Agent / AI settings
    lemonade_server_url = Column(String(500), nullable=True)
    lemonade_model = Column(String(255), nullable=True)
    bot_github_token = Column(Text, nullable=True)
    bot_github_login = Column(String(255), nullable=True)
    agent_interval_hours = Column(Integer, default=4, nullable=False)
    auto_merge = Column(Boolean, default=False, nullable=False)
    auto_promote = Column(Boolean, default=False, nullable=False)
    auto_promote_confidence = Column(Float, default=0.85, nullable=False)
    # Stale rebuild settings
    auto_rebuild_stale = Column(Boolean, default=False, nullable=False)
    stale_build_days = Column(Integer, default=30, nullable=False)

    user = relationship("User", back_populates="config")

    def __repr__(self) -> str:
        return f"<UserConfig user_id={self.user_id}>"


class AllowlistedUser(Base):
    """GitHub logins that are permitted to log in (admin-managed)."""

    __tablename__ = "allowlisted_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    github_login = Column(String(255), unique=True, nullable=False)
    added_by = Column(String(255), nullable=True)  # admin's github_login
    added_at = Column(DateTime, default=_now, nullable=False)
    note = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<AllowlistedUser login={self.github_login!r}>"


# ---------------------------------------------------------------------------
# Core data models
# ---------------------------------------------------------------------------


class Snap(Base):
    __tablename__ = "snaps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name = Column(String(255), nullable=False)
    publisher = Column(String(255), nullable=True)
    manually_added = Column(Boolean, default=False, nullable=False)
    packaging_repo = Column(Text, nullable=True)
    upstream_repo = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)

    user = relationship("User", back_populates="snaps")
    channel_map = relationship(
        "ChannelMap", back_populates="snap", cascade="all, delete-orphan"
    )
    issues = relationship(
        "Issue", back_populates="snap", cascade="all, delete-orphan"
    )
    upstream_releases = relationship(
        "UpstreamRelease", back_populates="snap", cascade="all, delete-orphan"
    )
    version_bump_prs = relationship(
        "VersionBumpPR", back_populates="snap", cascade="all, delete-orphan"
    )
    stale_build_triggers = relationship(
        "StaleBuildTrigger", back_populates="snap", cascade="all, delete-orphan"
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
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    started_at = Column(DateTime, default=_now, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(32), nullable=False, default="running")
    error_msg = Column(Text, nullable=True)

    user = relationship("User", back_populates="collection_runs")

    def __repr__(self) -> str:
        return f"<CollectionRun id={self.id} status={self.status!r}>"


class TestRun(Base):
    """Tracks a YARF test run triggered from the dashboard."""

    __tablename__ = "test_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    snap_name = Column(String(255), nullable=False)
    architecture = Column(String(32), nullable=True)  # 'amd64', 'arm64', etc.
    from_channel = Column(String(64), nullable=False)  # 'candidate', 'edge'
    version = Column(String(128), nullable=True)
    revision = Column(Integer, nullable=True)
    # statuses: pending, triggered, running, reviewing, passed, failed, error, promoted
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

    user = relationship("User", back_populates="test_runs")

    def __repr__(self) -> str:
        return f"<TestRun id={self.id} snap={self.snap_name!r} status={self.status!r}>"


# ---------------------------------------------------------------------------
# Agentic models
# ---------------------------------------------------------------------------


class StableScreenshotBaseline(Base):
    """Durable last-known-good screenshots from the last promoted stable run."""

    __tablename__ = "stable_screenshot_baselines"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "snap_name",
            "architecture",
            "image_name",
            name="uq_stable_baseline_image",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    snap_name = Column(String(255), nullable=False)
    architecture = Column(String(32), nullable=False, default="amd64")
    image_name = Column(String(500), nullable=False)
    image_url = Column(Text, nullable=True)
    image_b64 = Column(Text, nullable=False)
    source_test_run_id = Column(
        Integer, ForeignKey("test_runs.id", ondelete="SET NULL"), nullable=True
    )
    source_version = Column(String(128), nullable=True)
    source_revision = Column(Integer, nullable=True)
    promoted_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)

    user = relationship("User")
    source_test_run = relationship("TestRun")

    def __repr__(self) -> str:
        return (
            f"<StableScreenshotBaseline snap={self.snap_name!r}"
            f" arch={self.architecture!r} image={self.image_name!r}>"
        )


class AgentRun(Base):
    """Tracks a single execution of a background agent."""

    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    # release_scanner | version_bumper | pr_monitor | screenshot_reviewer
    agent_type = Column(String(64), nullable=False)
    snap_name = Column(String(255), nullable=True)
    # idle | running | done | error
    status = Column(String(32), nullable=False, default="running")
    result_summary = Column(Text, nullable=True)
    error_msg = Column(Text, nullable=True)
    started_at = Column(DateTime, default=_now, nullable=False)
    finished_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="agent_runs")

    def __repr__(self) -> str:
        return f"<AgentRun id={self.id} type={self.agent_type!r} status={self.status!r}>"


class UpstreamRelease(Base):
    """A new version discovered in an upstream project for a snap part."""

    __tablename__ = "upstream_releases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snap_id = Column(Integer, ForeignKey("snaps.id", ondelete="CASCADE"), nullable=False)
    part_name = Column(String(255), nullable=False)
    # git | pypi | launchpad | gitlab
    source_type = Column(String(32), nullable=True)
    source_url = Column(Text, nullable=True)
    current_version = Column(String(128), nullable=True)
    latest_version = Column(String(128), nullable=False)
    release_url = Column(Text, nullable=True)
    release_notes = Column(Text, nullable=True)
    discovered_at = Column(DateTime, default=_now, nullable=False)
    acted_on = Column(Boolean, default=False, nullable=False)
    acted_at = Column(DateTime, nullable=True)

    snap = relationship("Snap", back_populates="upstream_releases")

    def __repr__(self) -> str:
        return (
            f"<UpstreamRelease snap_id={self.snap_id} part={self.part_name!r}"
            f" latest={self.latest_version!r}>"
        )


class VersionBumpPR(Base):
    """A PR opened by the bot account to bump a snap's upstream version."""

    __tablename__ = "version_bump_prs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snap_id = Column(Integer, ForeignKey("snaps.id", ondelete="CASCADE"), nullable=False)
    upstream_release_id = Column(
        Integer, ForeignKey("upstream_releases.id", ondelete="SET NULL"), nullable=True
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    bot_pr_url = Column(Text, nullable=True)
    bot_pr_number = Column(Integer, nullable=True)
    packaging_repo = Column(String(500), nullable=True)
    branch_name = Column(String(255), nullable=True)
    old_version = Column(String(128), nullable=True)
    new_version = Column(String(128), nullable=True)
    # open | ci_pending | ci_passed | ci_failed | yarf_running | yarf_passed |
    # yarf_failed | agent_approved | agent_rejected | needs_review | merged | closed
    status = Column(String(64), nullable=False, default="open")
    test_run_id = Column(
        Integer, ForeignKey("test_runs.id", ondelete="SET NULL"), nullable=True
    )
    agent_decision = Column(String(32), nullable=True)  # approve | reject | needs_review
    agent_confidence = Column(Float, nullable=True)
    agent_reasoning = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    merged_at = Column(DateTime, nullable=True)

    snap = relationship("Snap", back_populates="version_bump_prs")
    upstream_release = relationship("UpstreamRelease")
    test_run = relationship("TestRun")
    user = relationship("User")

    def __repr__(self) -> str:
        return (
            f"<VersionBumpPR id={self.id} snap_id={self.snap_id}"
            f" {self.old_version!r}→{self.new_version!r} status={self.status!r}>"
        )


class StaleBuildTrigger(Base):
    """Records a workflow_dispatch trigger sent for a snap that hasn't published in N days."""

    __tablename__ = "stale_build_triggers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snap_id = Column(Integer, ForeignKey("snaps.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    packaging_repo = Column(String(500), nullable=True)
    channel = Column(String(64), nullable=False, default="edge")
    days_since_publish = Column(Integer, nullable=True)
    # triggered | skipped (workflow missing) | failed
    status = Column(String(32), nullable=False, default="triggered")
    error_msg = Column(Text, nullable=True)
    triggered_at = Column(DateTime, default=_now, nullable=False)

    snap = relationship("Snap", back_populates="stale_build_triggers")
    user = relationship("User")

    def __repr__(self) -> str:
        return (
            f"<StaleBuildTrigger id={self.id} snap_id={self.snap_id}"
            f" status={self.status!r}>"
        )


class ScreenshotComparison(Base):
    """LLM vision analysis comparing before/after screenshots for a version bump."""

    __tablename__ = "screenshot_comparisons"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version_bump_pr_id = Column(
        Integer, ForeignKey("version_bump_prs.id", ondelete="CASCADE"), nullable=False
    )
    test_run_id = Column(
        Integer, ForeignKey("test_runs.id", ondelete="SET NULL"), nullable=True
    )
    baseline_url = Column(Text, nullable=True)
    baseline_image_b64 = Column(Text, nullable=True)
    new_url = Column(Text, nullable=True)
    new_image_b64 = Column(Text, nullable=True)
    llm_prompt = Column(Text, nullable=True)
    llm_response = Column(Text, nullable=True)
    # approve | reject | needs_review
    decision = Column(String(32), nullable=True)
    confidence = Column(Float, nullable=True)
    reasoning = Column(Text, nullable=True)
    analyzed_at = Column(DateTime, default=_now, nullable=False)

    version_bump_pr = relationship("VersionBumpPR")
    test_run = relationship("TestRun")

    def __repr__(self) -> str:
        return (
            f"<ScreenshotComparison id={self.id}"
            f" pr_id={self.version_bump_pr_id} decision={self.decision!r}>"
        )
