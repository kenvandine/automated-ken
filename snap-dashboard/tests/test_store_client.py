"""Tests for Snap Store API response parsing (channel maps, repo URLs)."""

from __future__ import annotations

from snap_dashboard.store.client import extract_channel_map, extract_repo_urls

SNAP_INFO = {
    "channel-map": [
        {
            "channel": {
                "name": "latest/stable",
                "architecture": "amd64",
                "released-at": "2024-01-01T00:00:00.000000+00:00",
            },
            "revision": 100,
            "version": "1.0.0",
        },
        {
            "channel": {
                "name": "latest/candidate",
                "architecture": "amd64",
                "released-at": "2024-02-01T00:00:00.000000+00:00",
            },
            "revision": 101,
            "version": "1.1.0",
        },
        {
            "channel": {
                "name": "latest/edge",
                "architecture": "arm64",
                "released-at": "2024-02-15T00:00:00.000000+00:00",
            },
            "revision": 55,
            "version": "1.1.0",
        },
        {
            # A track other than "latest" should still be normalised to its risk level
            "channel": {
                "name": "22.04/stable",
                "architecture": "amd64",
                "released-at": "2023-06-01T00:00:00.000000+00:00",
            },
            "revision": 40,
            "version": "0.9.0",
        },
    ],
}


def test_extract_channel_map_normalises_track_and_risk() -> None:
    entries = extract_channel_map(SNAP_INFO)
    channels = {(e["channel"], e["architecture"]) for e in entries}
    assert ("stable", "amd64") in channels
    assert ("candidate", "amd64") in channels
    assert ("edge", "arm64") in channels
    # 22.04/stable is normalised down to "stable" too
    assert len([e for e in entries if e["channel"] == "stable"]) == 2


def test_extract_channel_map_ignores_untracked_risk_levels() -> None:
    info = {
        "channel-map": [
            {
                "channel": {"name": "latest/some-branch", "architecture": "amd64"},
                "revision": 1,
                "version": "1.0",
            }
        ]
    }
    assert extract_channel_map(info) == []


def test_extract_repo_urls_prefers_issues_link_for_packaging() -> None:
    info = {
        "links": {
            "issues": ["https://github.com/kenvandine/gedit-snap/issues"],
            "source": ["https://gitlab.com/GNOME/gedit"],
        }
    }
    urls = extract_repo_urls(info)
    assert urls["packaging_repo"] == "https://github.com/kenvandine/gedit-snap"
    assert urls["upstream_repo"] == "https://gitlab.com/GNOME/gedit"


def test_extract_repo_urls_falls_back_to_website_when_nothing_else() -> None:
    info = {"links": {"website": ["https://github.com/kenvandine/testit"]}}
    urls = extract_repo_urls(info)
    assert urls["packaging_repo"] == "https://github.com/kenvandine/testit"
    assert urls["upstream_repo"] is None


def test_extract_repo_urls_dedupes_when_same_repo() -> None:
    info = {
        "links": {
            "issues": ["https://github.com/kenvandine/foo/issues"],
            "source": ["https://github.com/kenvandine/foo"],
        }
    }
    urls = extract_repo_urls(info)
    assert urls["packaging_repo"] == "https://github.com/kenvandine/foo"
    assert urls["upstream_repo"] is None
