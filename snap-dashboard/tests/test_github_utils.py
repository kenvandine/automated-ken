"""Tests for snap_dashboard.github.utils repo slug parsing.

Regression coverage for a bug where ``str.rstrip(".git")`` (which strips a
*set* of trailing characters, not a literal suffix) corrupted repo names
ending in a run of ``.``, ``g``, ``i``, or ``t`` (e.g. ``gedit`` -> ``ged``).
"""

from __future__ import annotations

import pytest

from snap_dashboard.github.utils import parse_owner_repo, parse_repo_slug


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/kenvandine/gedit", "kenvandine/gedit"),
        ("https://github.com/kenvandine/gedit.git", "kenvandine/gedit"),
        ("https://github.com/kenvandine/gedit/", "kenvandine/gedit"),
        ("http://github.com/kenvandine/testit.git", "kenvandine/testit"),
        ("git@github.com:kenvandine/foo.git", "kenvandine/foo"),
        ("kenvandine/bare-repo", "kenvandine/bare-repo"),
        ("kenvandine/bare-repo.git", "kenvandine/bare-repo"),
        ("", ""),
    ],
)
def test_parse_repo_slug(url: str, expected: str) -> None:
    assert parse_repo_slug(url) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/kenvandine/gedit.git", ("kenvandine", "gedit")),
        ("https://github.com/kenvandine/testit", ("kenvandine", "testit")),
        ("kenvandine/foo", ("kenvandine", "foo")),
        ("", None),
        ("no-slash-here", None),
        ("https://github.com/", None),
    ],
)
def test_parse_owner_repo(url: str, expected) -> None:
    assert parse_owner_repo(url) == expected


def test_parse_repo_slug_does_not_over_strip_trailing_letters() -> None:
    """Names ending in '.', 'g', 'i', or 't' must survive intact."""
    for name in ("gedit", "testit", "git-it", "ubiquiti"):
        slug = f"kenvandine/{name}"
        assert parse_repo_slug(f"https://github.com/{slug}.git") == slug
