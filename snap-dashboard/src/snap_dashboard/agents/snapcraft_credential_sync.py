"""One-shot agent: push the stored Snapcraft Store credential out to every
managed packaging repo's GitHub Actions secrets.

Triggered manually from Settings after Ken pastes/updates
``UserConfig.snapcraft_macaroon`` — not a periodic agent, since there's
nothing to re-sync unless the credential actually changes or a new repo is
added to the fleet.
"""

from __future__ import annotations

import logging

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import Snap, User
from snap_dashboard.db.session import get_session
from snap_dashboard.github.secrets_sync import sync_snapcraft_credentials
from snap_dashboard.github.utils import is_owned_by

logger = logging.getLogger(__name__)


class SnapcraftCredentialSyncAgent(BaseAgent):
    """Pushes the stored Snapcraft Store credential to all packaging repos."""

    agent_type = "snapcraft_credential_sync"

    def __init__(self, user_id: int | None = None) -> None:
        super().__init__(user_id=user_id)

    def _run(self) -> str:
        if not self.user_id:
            return "no user_id — skipped"
        uc = get_user_config(self.user_id)
        if not uc:
            return "no user config"
        credential = getattr(uc, "snapcraft_macaroon", "") or ""
        if not credential:
            return "no Snapcraft Store credential configured"
        # Managing a repo's Actions secrets requires admin-level access to
        # that *exact* repo (forking doesn't help — secrets never propagate
        # across a fork). These are all Ken's own packaging repos, so his
        # personal token (which has owner/admin rights on them) must be used
        # here, not the separate bot account's token, which typically only
        # has read/write collaborator access and will 403 on the secrets API.
        token = getattr(uc, "github_token", "") or getattr(uc, "bot_github_token", "") or ""
        if not token:
            return "no GitHub token configured"

        with get_session() as session:
            user = session.query(User).get(self.user_id)
            login = (user.github_login or "") if user else ""
            repos = sorted(
                {
                    s.packaging_repo
                    for s in session.query(Snap).filter_by(user_id=self.user_id).all()
                    if s.packaging_repo
                }
            )

        if not repos:
            return "no packaging repos found"

        owned_repos = [r for r in repos if is_owned_by(r, login)]
        not_owned = [r for r in repos if r not in owned_repos]
        if not_owned:
            logger.warning(
                "snapcraft_credential_sync: skipping %d packaging_repo(s) not "
                "owned by %s (secrets can only be set on a repo you actually "
                "own/admin): %s",
                len(not_owned),
                login or "(unknown user)",
                ", ".join(not_owned),
            )
        repos = owned_repos
        if not repos:
            return f"no owned packaging repos found ({len(not_owned)} skipped as not owned)"

        self._report(f"Syncing Snapcraft credential to {len(repos)} repos")
        results = sync_snapcraft_credentials(repos, credential, token)
        ok_count = sum(1 for r in results if r["ok"])
        failed = [r["repo"] for r in results if not r["ok"]]
        summary = f"synced {ok_count}/{len(repos)} repos"
        if failed:
            summary += f"; failed: {', '.join(failed[:10])}"
            if len(failed) > 10:
                summary += f" (+{len(failed) - 10} more)"
        if not_owned:
            summary += f"; {len(not_owned)} skipped as not owned"
        return summary
