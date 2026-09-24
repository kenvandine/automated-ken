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
from snap_dashboard.db.models import Snap
from snap_dashboard.db.session import get_session
from snap_dashboard.github.secrets_sync import sync_snapcraft_credentials

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
        token = getattr(uc, "bot_github_token", "") or getattr(uc, "github_token", "") or ""
        if not token:
            return "no GitHub token configured"

        with get_session() as session:
            repos = sorted(
                {
                    s.packaging_repo
                    for s in session.query(Snap).filter_by(user_id=self.user_id).all()
                    if s.packaging_repo
                }
            )

        if not repos:
            return "no packaging repos found"

        self._report(f"Syncing Snapcraft credential to {len(repos)} repos")
        results = sync_snapcraft_credentials(repos, credential, token)
        ok_count = sum(1 for r in results if r["ok"])
        failed = [r["repo"] for r in results if not r["ok"]]
        summary = f"synced {ok_count}/{len(repos)} repos"
        if failed:
            summary += f"; failed: {', '.join(failed[:10])}"
            if len(failed) > 10:
                summary += f" (+{len(failed) - 10} more)"
        return summary
