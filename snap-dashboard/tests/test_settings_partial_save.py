"""Regression tests for the settings page's multi-form save bug.

The settings page is split into several independent ``<form>`` cards
(Publisher, GitHub Token, Snapcraft Credential, Testing, Agents & AI), each
posting to the same ``/settings`` endpoint with only its own subset of
fields. Two bugs combined to make saves from any one card appear to silently
do nothing (or reset other cards):

1. ``settings_post`` applied every field unconditionally, so submitting one
   card's form reset every *other* card's checkboxes/fields back to their
   ``Form(default=...)`` values (e.g. saving "Enable automatic testing"
   would silently flip "Enable fleet normalization campaign" back off).
2. ``UserConfigView``/``get_user_config()`` didn't expose several newer
   fields at all (fleet_normalization_enabled, auto_rebuild_stale, etc.),
   so even when a save *did* persist correctly, the re-rendered page always
   showed those checkboxes unchecked regardless of the real DB value.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.auth import UserConfigView, get_user_config
from snap_dashboard.db.models import Base, User, UserConfig
from snap_dashboard.web.routes import settings as settings_module


@pytest.fixture
def isolated_session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    @contextmanager
    def _fake_get_session():
        session = session_local()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(settings_module, "get_session", _fake_get_session)
    # auth.get_user_config uses its own module-level get_session reference.
    import snap_dashboard.auth as auth_module

    monkeypatch.setattr(auth_module, "get_session", _fake_get_session)
    return session_local


def _make_user(session_local) -> int:
    session = session_local()
    user = User(github_login="ken", github_id=1)
    session.add(user)
    session.commit()
    user_id = user.id
    session.close()
    return user_id


class _FakeRequest:
    """Minimal stand-in for a Request exposing only what settings_post needs."""

    def __init__(self, user_id: int) -> None:
        self.session = {"user_id": user_id}


async def _post(user_id: int, **fields):
    """Call settings_post() with sensible defaults for all Form(...) params,
    overridden by **fields, mimicking a real per-card partial submission.
    """
    request = _FakeRequest(user_id)
    defaults = dict(
        section="",
        publisher="",
        github_token="",
        snapcraft_macaroon="",
        interval=6,
        auto_test="",
        runner_job_timeout_minutes=10,
        lemonade_server_url="",
        lemonade_model="",
        lemonade_backend="embedded",
        lemonade_api_key="",
        bot_github_token="",
        bot_github_login="",
        agent_interval_hours=4,
        auto_merge="",
        auto_promote="",
        auto_promote_confidence=0.85,
        auto_rebuild_stale="",
        stale_build_days=30,
        auto_fix_ci_failures="",
        auto_maintain_upstream="",
        fleet_normalization_enabled="",
        coding_task_backend="copilot_cloud_agent",
        external_coding_api_key="",
        external_coding_api_base_url="",
        external_coding_api_model="",
    )
    defaults.update(fields)
    return await settings_module.settings_post(request=request, **defaults)


@pytest.mark.anyio
async def test_saving_agents_ai_section_does_not_reset_auto_test(isolated_session):
    user_id = _make_user(isolated_session)
    # Enable auto_test via its own "testing" section first.
    await _post(user_id, section="testing", auto_test="true")

    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    assert uc.auto_test is True
    session.close()

    # Now save the "Agents & AI" card (e.g. toggling fleet normalization) --
    # this must NOT reset auto_test back to False, since that field belongs
    # to a different card and was never part of this submission.
    await _post(user_id, section="agents_ai", fleet_normalization_enabled="true")

    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    assert uc.auto_test is True
    assert uc.fleet_normalization_enabled is True
    session.close()


@pytest.mark.anyio
async def test_saving_testing_section_does_not_reset_agents_ai_fields(isolated_session):
    user_id = _make_user(isolated_session)
    await _post(
        user_id,
        section="agents_ai",
        fleet_normalization_enabled="true",
        auto_merge="true",
        agent_interval_hours=12,
        coding_task_backend="local_lemonade",
    )

    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    assert uc.fleet_normalization_enabled is True
    assert uc.auto_merge is True
    assert uc.agent_interval_hours == 12
    assert uc.coding_task_backend == "local_lemonade"
    session.close()

    # Saving just the "Testing" card afterwards must not clobber any of the
    # Agents & AI settings above.
    await _post(user_id, section="testing", auto_test="true")

    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    assert uc.auto_test is True
    assert uc.fleet_normalization_enabled is True
    assert uc.auto_merge is True
    assert uc.agent_interval_hours == 12
    assert uc.coding_task_backend == "local_lemonade"
    session.close()


@pytest.mark.anyio
async def test_unchecking_a_checkbox_in_its_own_section_still_works(isolated_session):
    user_id = _make_user(isolated_session)
    await _post(user_id, section="agents_ai", fleet_normalization_enabled="true")
    session = isolated_session()
    assert session.query(UserConfig).filter_by(user_id=user_id).first().fleet_normalization_enabled is True
    session.close()

    # Unchecking sends no value for the checkbox at all -- but it's still
    # part of the *same* section's submission, so it must be turned off.
    await _post(user_id, section="agents_ai")
    session = isolated_session()
    assert session.query(UserConfig).filter_by(user_id=user_id).first().fleet_normalization_enabled is False
    session.close()


def test_user_config_view_exposes_all_fields_saved_by_settings_post(monkeypatch, isolated_session):
    """Every field settings_post() can write must round-trip through
    get_user_config() so the template re-renders the real saved state
    (previously several fields were silently dropped, always rendering
    checkboxes as unchecked regardless of the DB value)."""
    user_id = _make_user(isolated_session)
    session = isolated_session()
    uc = UserConfig(
        user_id=user_id,
        fleet_normalization_enabled=True,
        auto_rebuild_stale=True,
        stale_build_days=14,
        auto_fix_ci_failures=True,
        auto_maintain_upstream=True,
        coding_task_backend="local_lemonade",
        external_coding_api_base_url="https://example.test",
        external_coding_api_model="gpt-x",
    )
    session.add(uc)
    session.commit()
    session.close()

    view = get_user_config(user_id)
    assert isinstance(view, UserConfigView)
    assert view.fleet_normalization_enabled is True
    assert view.auto_rebuild_stale is True
    assert view.stale_build_days == 14
    assert view.auto_fix_ci_failures is True
    assert view.auto_maintain_upstream is True
    assert view.coding_task_backend == "local_lemonade"
    assert view.external_coding_api_base_url == "https://example.test"
    assert view.external_coding_api_model == "gpt-x"
