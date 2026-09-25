"""Regression tests for inference-driven upstream version checking.

``choose_latest_version`` (and ``get_latest_version`` above it) used to
decide "is there a newer upstream release" purely via a regex tag-strip
plus ``packaging.version.Version()`` compare. That's now only a fallback —
a local model is the primary decision-maker so oddly-tagged repos (date
tags, component-prefixed tags, pre-releases mixed in) get handled
correctly instead of silently mis-comparing.
"""

from __future__ import annotations

from snap_dashboard.snapcraft.upstream import (
    _Candidate,
    choose_latest_version,
    is_newer,
)


class _FakeLemonadeClient:
    def __init__(self, reply: str | None):
        self._reply = reply

    def is_available(self) -> bool:
        return True

    def chat(self, prompt, **kwargs):
        return self._reply


def _candidates() -> list[_Candidate]:
    return [
        _Candidate(tag="v2.1.0", version="2.1.0"),
        _Candidate(tag="v2.1.0-rc1", version="2.1.0-rc1", prerelease=True),
        _Candidate(tag="v2.0.0", version="2.0.0"),
    ]


def test_choose_latest_version_uses_model_when_available(monkeypatch):
    """The model's pick (by exact tag) wins over the heuristic when available."""
    fake_client = _FakeLemonadeClient('{"is_newer": true, "tag": "v2.1.0"}')
    monkeypatch.setattr(
        "snap_dashboard.lemonade.client.get_lemonade_client", lambda uc, **kw: fake_client
    )

    chosen = choose_latest_version(_candidates(), "2.0.0", user_config=object())
    assert chosen is not None
    assert chosen.tag == "v2.1.0"
    assert chosen.version == "2.1.0"


def test_choose_latest_version_model_says_not_newer(monkeypatch):
    fake_client = _FakeLemonadeClient('{"is_newer": false, "tag": ""}')
    monkeypatch.setattr(
        "snap_dashboard.lemonade.client.get_lemonade_client", lambda uc, **kw: fake_client
    )

    chosen = choose_latest_version(_candidates(), "2.1.0", user_config=object())
    assert chosen is None


def test_choose_latest_version_falls_back_when_no_model(monkeypatch):
    monkeypatch.setattr(
        "snap_dashboard.lemonade.client.get_lemonade_client", lambda uc, **kw: None
    )

    chosen = choose_latest_version(_candidates(), "2.0.0", user_config=object())
    # Heuristic skips the prerelease and picks the first newer stable tag.
    assert chosen is not None
    assert chosen.tag == "v2.1.0"


def test_choose_latest_version_falls_back_on_unrecognized_model_tag(monkeypatch):
    """A model reply naming a tag that isn't in the candidate list is untrusted."""
    fake_client = _FakeLemonadeClient('{"is_newer": true, "tag": "v9.9.9"}')
    monkeypatch.setattr(
        "snap_dashboard.lemonade.client.get_lemonade_client", lambda uc, **kw: fake_client
    )

    chosen = choose_latest_version(_candidates(), "2.0.0", user_config=object())
    assert chosen is not None
    assert chosen.tag == "v2.1.0"  # heuristic fallback, not a hallucinated tag


def test_choose_latest_version_falls_back_when_model_reply_unparseable(monkeypatch):
    fake_client = _FakeLemonadeClient("not json at all")
    monkeypatch.setattr(
        "snap_dashboard.lemonade.client.get_lemonade_client", lambda uc, **kw: fake_client
    )

    chosen = choose_latest_version(_candidates(), "2.0.0", user_config=object())
    assert chosen is not None
    assert chosen.tag == "v2.1.0"


def test_choose_latest_version_no_candidates_returns_none():
    assert choose_latest_version([], "1.0.0", user_config=None) is None


def test_is_newer_still_works_as_deterministic_fallback():
    assert is_newer("2.1.0", "2.0.0") is True
    assert is_newer("2.0.0", "2.1.0") is False
    assert is_newer("1.0.0", "") is True
    assert is_newer("", "1.0.0") is False
