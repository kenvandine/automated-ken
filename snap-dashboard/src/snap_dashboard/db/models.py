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
    # Which Lemonade instance to talk to: "embedded" (default — our private,
    # bundled Embedded Lemonade, see lemonade/embedded.py) or "system" (the
    # user's own self-managed lemonade-server at lemonade_server_url,
    # optionally protected by lemonade_api_key).
    lemonade_backend = Column(String(16), default="embedded", nullable=False)
    lemonade_api_key = Column(Text, nullable=True)
    bot_github_token = Column(Text, nullable=True)
    bot_github_login = Column(String(255), nullable=True)
    agent_interval_hours = Column(Integer, default=4, nullable=False)
    auto_merge = Column(Boolean, default=False, nullable=False)
    auto_promote = Column(Boolean, default=False, nullable=False)
    auto_promote_confidence = Column(Float, default=0.85, nullable=False)
    # Stale rebuild settings
    auto_rebuild_stale = Column(Boolean, default=False, nullable=False)
    stale_build_days = Column(Integer, default=30, nullable=False)
    # Remote runner settings
    prefer_remote_runner = Column(Boolean, default=False, nullable=False)
    runner_job_timeout_minutes = Column(Integer, default=10, nullable=False)
    # Delegating "capable coding" work to GitHub Copilot cloud agent — see
    # agents/pr_monitor.py, agents/upstream_maintainer.py, agents/repo_normalizer.py.
    # All default off: these open real PRs/issues-fixes against real repos,
    # so they're opt-in until a user has reviewed a first run.
    auto_fix_ci_failures = Column(Boolean, default=False, nullable=False)
    auto_maintain_upstream = Column(Boolean, default=False, nullable=False)
    fleet_normalization_enabled = Column(Boolean, default=False, nullable=False)

    # Which backend handles "capable coding" tasks (CI fixes, dep upgrades,
    # issue fixes, fleet normalization). Long-term goal is to run this
    # locally as capable local models become available; "copilot_cloud_agent"
    # is today's practical default since it needs no extra credentials.
    # "external_api" is a forward-looking escalation path for a
    # user-supplied API key to a hosted coding model, not yet implemented.
    coding_task_backend = Column(String(32), default="copilot_cloud_agent", nullable=False)
    external_coding_api_key = Column(Text, nullable=True)
    external_coding_api_base_url = Column(String(500), nullable=True)
    external_coding_api_model = Column(String(255), nullable=True)

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
    # Every snap defaults to the generic graphical desktop smoke test
    # (launch on the real desktop, screenshot, done). Console apps have
    # nothing to screenshot when run headless, so this opts a snap into
    # being launched inside a terminal emulator instead — see
    # automated_ken_runner.runner._run_desktop_smoke_test.
    is_console_app = Column(Boolean, default=False, nullable=False)
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
    # "owner/repo" this run was actually dispatched against — the snap's own
    # packaging repo when it has colocated YARF tests (tests/suite/), or the
    # legacy shared testing repo as a fallback for snaps not yet migrated.
    repo = Column(String(500), nullable=True)
    # statuses: pending, triggered, running, reviewing, passed, failed, error, promoted
    status = Column(String(32), nullable=False, default="pending")
    gh_run_id = Column(String(128), nullable=True)  # GitHub Actions run ID
    pr_number = Column(Integer, nullable=True)
    pr_url = Column(Text, nullable=True)
    pr_body = Column(Text, nullable=True)
    triggered_by = Column(String(64), nullable=True)  # 'auto', 'manual', or 'external'
    # 'github_actions' (default) or 'remote_runner' — see Runner model.
    dispatch_target = Column(String(32), nullable=False, default="github_actions")
    runner_id = Column(
        Integer, ForeignKey("runners.id", ondelete="SET NULL"), nullable=True
    )
    # Manual queue ordering for remote-runner jobs — higher runs first;
    # ties broken by started_at (FIFO). Adjustable from the /runners page.
    priority = Column(Integer, nullable=False, default=0)
    # Set from the dashboard to ask an in-flight remote-runner job to stop;
    # the runner checks this on its next heartbeat/status report.
    cancel_requested = Column(Boolean, nullable=False, default=False)
    started_at = Column(DateTime, default=_now, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    promoted = Column(Boolean, default=False, nullable=False)
    promoted_at = Column(DateTime, nullable=True)
    error_msg = Column(Text, nullable=True)
    # Combined stdout/stderr/traceback captured by automated-ken-runner while
    # executing this job (snap install/refresh + yarf output) — lets users
    # debug a failure from the web UI instead of SSHing into the runner and
    # grepping journalctl. Only populated for dispatch_target=remote_runner.
    log_output = Column(Text, nullable=True)
    # LLM vision-review outcome — always populated by TestRunAutoPromoterAgent
    # for any "passed" candidate run once it's reviewed (see
    # agents/test_run_auto_promoter.py), independent of whether the run has
    # an associated VersionBumpPR. decision: approve | reject | needs_review,
    # or None if review hasn't run / was skipped (see review_reasoning for why).
    review_decision = Column(String(32), nullable=True)
    review_confidence = Column(Float, nullable=True)
    review_reasoning = Column(Text, nullable=True)
    # LLM-inferred plain-English root cause for a failed/errored run,
    # derived from log_output/error_msg — see agents/test_failure_analyzer.py.
    # Purely informational today; a future agent could use this to attempt
    # an automated fix PR. Only populated for dispatch_target=remote_runner
    # (the only path with a real captured log to analyze).
    failure_analysis = Column(Text, nullable=True)
    # Links sibling per-architecture runs of the same version bump together
    # (one TestRun per arch — see agents/pr_monitor.py:_trigger_yarf) so the
    # PR monitor / screenshot reviewer / stable promoter can wait for every
    # arch to finish and promote the whole release set as one unit, the way
    # `snapcraft promote` treats a version's revisions across architectures.
    # VersionBumpPR.test_run_id still points at one representative run for
    # older single-run displays/links.
    version_bump_pr_id = Column(
        Integer, ForeignKey("version_bump_prs.id", ondelete="SET NULL"), nullable=True
    )

    user = relationship("User", back_populates="test_runs")
    runner = relationship("Runner", foreign_keys=[runner_id])

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


class TestRunScreenshot(Base):
    """Persistent, source-agnostic screenshot storage for a test run.

    Populated from either dispatch target:
    - GitHub Actions: :func:`snap_dashboard.testing.orchestrator.ingest_run_screenshots`
      downloads the ``yarf-results-*`` artifact via the Actions API.
    - Remote runner: uploaded directly by ``automated-ken-runner`` via
      ``POST /api/runners/{id}/jobs/{job_id}/screenshots``.

    :func:`snap_dashboard.testing.baselines.load_test_run_screenshots` reads
    this table first for any ``TestRun``, regardless of ``dispatch_target``.
    """

    __tablename__ = "test_run_screenshots"
    __table_args__ = (
        UniqueConstraint(
            "test_run_id",
            "image_name",
            name="uq_test_run_screenshot_image",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    test_run_id = Column(
        Integer, ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False
    )
    image_name = Column(String(500), nullable=False)
    image_b64 = Column(Text, nullable=False)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    brightness_mean = Column(Float, nullable=True)
    is_valid = Column(Boolean, nullable=True)
    captured_at = Column(DateTime, default=_now, nullable=False)

    test_run = relationship("TestRun")

    def __repr__(self) -> str:
        return f"<TestRunScreenshot test_run_id={self.test_run_id} image={self.image_name!r}>"


class Runner(Base):
    """A registered remote test-runner machine (real desktop, physical hardware).

    Runners poll the dashboard for queued jobs (outbound-only HTTPS, so they
    work from behind NAT/home routers with no inbound port needed) rather
    than being dispatched to directly, mirroring a self-hosted CI runner.
    """

    __tablename__ = "runners"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name = Column(String(255), nullable=False)
    # sha256 hex digest of the long-lived bearer secret issued at enrollment.
    secret_hash = Column(String(64), nullable=True)
    # sha256 hex digest of the short-lived, one-time enrollment token.
    enrollment_token_hash = Column(String(64), nullable=True)
    enrollment_expires_at = Column(DateTime, nullable=True)
    arch = Column(String(32), nullable=True)
    os_name = Column(String(255), nullable=True)
    desktop_env = Column(String(64), nullable=True)
    # enrolling | idle | locked | busy | offline
    status = Column(String(32), nullable=False, default="enrolling")
    idle_seconds = Column(Integer, nullable=True)
    idle_threshold_seconds = Column(Integer, nullable=False, default=120)
    last_heartbeat_at = Column(DateTime, nullable=True)
    current_test_run_id = Column(
        Integer, ForeignKey("test_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime, default=_now, nullable=False)
    revoked_at = Column(DateTime, nullable=True)

    user = relationship("User")
    current_test_run = relationship("TestRun", foreign_keys=[current_test_run_id])

    def __repr__(self) -> str:
        return f"<Runner id={self.id} name={self.name!r} status={self.status!r}>"


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
    # dispatched | open | ci_pending | ci_passed | ci_failed | yarf_running |
    # yarf_passed | yarf_failed | agent_approved | agent_rejected |
    # needs_review | merged | closed | awaiting_release | candidate_testing |
    # stable_promoted | stable_promoted_partial (manual override)
    # — see agents/pr_monitor.py. "dispatched" is the initial state when the
    # version bump was delegated to an async coding backend (GitHub Copilot
    # cloud agent) and no PR exists yet; pr_monitor.py polls it and advances
    # to "open" once the agent's PR appears (or "closed" if it never does).
    status = Column(String(64), nullable=False, default="open")
    # GitHub Copilot cloud agent's task id, set only while status=="dispatched"
    # — see coding_backend.get_coding_dispatcher()/CopilotAgentClient.
    external_task_id = Column(String(128), nullable=True)
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
    # Explicit foreign_keys: TestRun also has a version_bump_pr_id column
    # (the reverse, one-per-architecture link — see TestRun above), so two
    # FK paths now connect these tables and SQLAlchemy can't infer which
    # one this relationship means without being told.
    test_run = relationship("TestRun", foreign_keys=[test_run_id])
    user = relationship("User")

    def __repr__(self) -> str:
        return (
            f"<VersionBumpPR id={self.id} snap_id={self.snap_id}"
            f" {self.old_version!r}→{self.new_version!r} status={self.status!r}>"
        )


class PromotionDismissal(Base):
    """A "not now" on a Testing page Pending Promotion card.

    Keyed by ``(user_id, snap_name, version)`` rather than a run/set id
    since a release set has no single row of its own — it's just every
    ``TestRun`` sharing a ``(snap_name, version)`` (see
    ``testing/release_set.py``). Scoped to keep matching the *current*
    candidate version only: once a snap ships a newer candidate version,
    its card reappears, since a dismissal was about "not this build" not
    "never tell me about this snap again".
    """

    __tablename__ = "promotion_dismissals"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "snap_name", "version", name="uq_promotion_dismissal"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    snap_name = Column(String(255), nullable=False)
    version = Column(String(128), nullable=False)
    dismissed_at = Column(DateTime, default=_now, nullable=False)

    user = relationship("User")

    def __repr__(self) -> str:
        return f"<PromotionDismissal snap={self.snap_name!r} version={self.version!r}>"


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
    # Which workflow file was actually dispatched (or None if never determined
    # — e.g. status="skipped"). See agents/stale_build_scanner._infer_build_workflow.
    workflow_file = Column(String(255), nullable=True)
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


class ModelUsage(Base):
    """Tracks input/output token usage for one LLM call.

    Populated by every LLM invocation in the system — local models via
    lemonade-server (``provider="lemonade"``) and, when configured, GitHub
    Copilot cloud agent (``provider="copilot"``) — so the ``/stats`` page
    can show how much of the platform's AI work is running locally vs. in
    the cloud, broken down by model and in aggregate. Counts are exact when
    the backend's API reports real usage (e.g. lemonade's OpenAI-compatible
    ``usage`` field); otherwise they are a rough ~4-chars/token estimate,
    flagged via ``estimated`` (Copilot's cloud-agent task API never reports
    usage, so its rows are always estimated, input-only).
    """

    __tablename__ = "model_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # "lemonade" (local) | "copilot" (cloud)
    provider = Column(String(32), nullable=False)
    model = Column(String(255), nullable=False)
    # chat | vision_compare | coding_agent
    task = Column(String(32), nullable=False)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    estimated = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=_now, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<ModelUsage id={self.id} provider={self.provider!r} model={self.model!r}"
            f" in={self.input_tokens} out={self.output_tokens} estimated={self.estimated}>"
        )


class CopilotTask(Base):
    """Tracks a task dispatched to GitHub Copilot cloud agent.

    Covers all the "capable coding" work delegated out rather than done with
    a local/small model: fixing a failing CI check on a bot PR (``ci_fix``),
    dependency upgrades on a repo the user maintains upstream (``dep_update``),
    attempting a fix for a filed issue (``issue_fix``), and the one-time
    fleet-normalization campaign (``fleet_normalize``).
    """

    __tablename__ = "copilot_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    snap_id = Column(Integer, ForeignKey("snaps.id", ondelete="SET NULL"), nullable=True)
    # ci_fix | dep_update | issue_fix | fleet_normalize
    kind = Column(String(32), nullable=False)
    owner_repo = Column(String(500), nullable=False)  # "owner/repo" the task targets
    # GitHub's own agent-task id, e.g. for GET /agents/repos/{o}/{r}/tasks/{id}.
    external_task_id = Column(String(128), nullable=True)
    prompt = Column(Text, nullable=True)
    # queued | in_progress | completed | failed | idle | waiting_for_user |
    # timed_out | cancelled | dispatch_failed (our own sentinel if the POST itself failed)
    status = Column(String(32), nullable=False, default="queued")
    pr_url = Column(Text, nullable=True)
    issue_number = Column(Integer, nullable=True)
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)

    user = relationship("User")
    snap = relationship("Snap")

    def __repr__(self) -> str:
        return (
            f"<CopilotTask id={self.id} kind={self.kind!r}"
            f" repo={self.owner_repo!r} status={self.status!r}>"
        )
