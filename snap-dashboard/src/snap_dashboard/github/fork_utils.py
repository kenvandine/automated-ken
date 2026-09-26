"""Shared helper: make sure the bot account has a pushable fork of a repo.

Every "bot commits files + opens a PR" code path in this app —
``bot_client.BotGitHubClient`` (the mechanical version-bump fallback) and
``tree_commit.GitTreeClient`` (the local Lemonade coding backend) — needs
somewhere it can actually push a branch to. In practice the bot account
(e.g. ``automated-ken``) is *not* a collaborator on the packaging repos it
maintains — they're normal repos owned by a human or a third party (e.g.
``kenvandine/*`` or an upstream project like ``avojak/warble``) — so
writing directly to ``owner/repo`` via the git-data write endpoints
reliably 404s (GitHub returns 404, not 403, when a token lacks write
access to those endpoints — the read endpoints "work" regardless because
the repos are public).

The standard fix used by essentially every external-contribution bot:
fork the repo under the bot's own account, push the commit there, and open
the PR cross-repo (``head="bot_login:branch"``). This module only handles
the "make sure the fork exists and is ready" half of that.
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"

# Forking is asynchronous on GitHub's side — right after the POST, the new
# repo often isn't queryable/pushable for a few seconds — so this polls
# briefly rather than assuming it's instantly ready.
_POLL_ATTEMPTS = 15
_POLL_DELAY_SECONDS = 2


def ensure_fork(client: httpx.Client, headers: dict, owner: str, repo: str, fork_owner: str) -> bool:
    """Ensure ``fork_owner/repo`` exists (as a fork of ``owner/repo``) and is
    ready to receive pushes, forking it if necessary.

    Returns False if the fork couldn't be created/confirmed within a
    reasonable time — callers should treat that as "give up on this repo",
    not retry indefinitely.
    """
    check = client.get(f"{_GH_API}/repos/{fork_owner}/{repo}", headers=headers)
    if check.status_code == 200:
        return True

    create = client.post(f"{_GH_API}/repos/{owner}/{repo}/forks", headers=headers, json={})
    if create.status_code not in (200, 202):
        logger.warning(
            "fork %s/%s -> %s failed: %s %s",
            owner, repo, fork_owner, create.status_code, create.text[:200],
        )
        return False

    for _ in range(_POLL_ATTEMPTS):
        time.sleep(_POLL_DELAY_SECONDS)
        check = client.get(f"{_GH_API}/repos/{fork_owner}/{repo}", headers=headers)
        if check.status_code == 200:
            return True
    logger.warning("fork of %s/%s -> %s never became ready", owner, repo, fork_owner)
    return False
