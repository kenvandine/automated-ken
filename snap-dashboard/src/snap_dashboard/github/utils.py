"""Shared helpers for working with GitHub repo URLs.

Several agents and routes need to turn a stored ``packaging_repo`` value
(either a full ``https://github.com/owner/repo`` URL or a bare
``owner/repo`` string) into a canonical ``owner/repo`` slug. This used to be
reimplemented independently in five different modules, four of which used
``str.rstrip(".git")`` instead of ``str.removesuffix(".git")``.

``rstrip`` strips any trailing characters found in its argument *as a set*,
not the literal suffix — so ``"kenvandine/gedit".rstrip(".git")`` incorrectly
returns ``"kenvandine/ged"`` (it keeps stripping trailing characters that are
any of ``.``, ``g``, ``i``, ``t``). That silently corrupted the repo slug for
any packaging repo whose name happened to end in a run of those letters,
breaking the version bumper, PR monitor, and promoter agents for those snaps.

This module provides a single, correct implementation to use everywhere.
"""

from __future__ import annotations

_GITHUB_PREFIXES = ("https://github.com/", "http://github.com/", "git@github.com:")


def parse_repo_slug(repo: str) -> str:
    """Normalise a GitHub repo URL or ``owner/repo`` string to ``owner/repo``.

    Strips a leading GitHub host prefix (https, http, or ssh), a trailing
    slash, and a literal trailing ``.git`` suffix. Does not validate that the
    result actually contains an ``owner/repo`` pair — use
    :func:`parse_owner_repo` when you need that guarantee.
    """
    repo = (repo or "").strip()
    for prefix in _GITHUB_PREFIXES:
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
            break
    repo = repo.rstrip("/")
    repo = repo.removesuffix(".git")
    return repo


def parse_owner_repo(repo: str) -> tuple[str, str] | None:
    """Return ``(owner, repo)`` parsed from a GitHub URL, or ``None``.

    Returns ``None`` if the input doesn't look like it contains an
    ``owner/repo`` pair (e.g. empty, or missing a ``/``).
    """
    slug = parse_repo_slug(repo)
    if not slug or "/" not in slug:
        return None
    owner, _, name = slug.partition("/")
    if not owner or not name:
        return None
    return owner, name
