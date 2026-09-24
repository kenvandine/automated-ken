"""Propagate one centrally-stored Snapcraft Store credential out to every
managed packaging repo's GitHub Actions secrets.

Ken creates a single ``snapcraft export-login`` credential that works across
his whole snap fleet and pastes it once into the dashboard's Settings page
(``UserConfig.snapcraft_macaroon``). Rather than requiring him to also paste
it into every packaging repo's GitHub Actions secrets by hand, this module
pushes it out to each repo automatically using the (standard, unauthenticated
beyond the token) GitHub Actions secrets API, which requires the value to be
sealed with the target repo's own public key (libsodium/NaCl sealed box —
see https://docs.github.com/en/rest/actions/secrets).

Existing packaging repos were bootstrapped independently over time and don't
all reference the credential under the same secret name — most use the
"official" ``SNAPCRAFT_STORE_CREDENTIALS`` name, but some legacy workflows
use ``STORE_LOGIN`` instead. ``_detect_secret_names`` scans each repo's
workflow files for either so both keep working without needing their YAML
rewritten, and repos with no existing reference default to
``SNAPCRAFT_STORE_CREDENTIALS`` (the name ``snapcraft`` itself expects).
"""

from __future__ import annotations

import base64
import logging
import re

import httpx
from nacl import encoding, public

from snap_dashboard.github.utils import parse_owner_repo

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"

#: Secret name used by ``snapcraft`` itself (the env var it reads credentials
#: from) — the default for repos with no pre-existing secret reference.
DEFAULT_SECRET_NAME = "SNAPCRAFT_STORE_CREDENTIALS"

#: Secret names we recognize as "this is the snapcraft store credential"
#: when scanning existing workflow files, so legacy naming keeps working.
_KNOWN_SECRET_NAME_PATTERN = re.compile(
    r"SNAPCRAFT_STORE_CREDENTIALS\s*:\s*\$\{\{\s*secrets\.(\w+)\s*\}\}"
)


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


def encrypt_secret(public_key_b64: str, secret_value: str) -> str:
    """Seal *secret_value* with a repo's Actions public key (base64-encoded).

    GitHub requires secret values to be encrypted client-side with the
    repo's public key (libsodium sealed box) before they're PUT to the
    secrets API — see the GitHub REST API docs for
    "Create or update a repository secret".
    """
    public_key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def _get_repo_public_key(owner: str, repo: str, token: str) -> tuple[str, str] | None:
    """Return ``(key_id, key_base64)`` for a repo's Actions secrets, or None."""
    url = f"{_GH_API}/repos/{owner}/{repo}/actions/secrets/public-key"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers=_headers(token))
        if resp.status_code != 200:
            logger.warning(
                "get public key for %s/%s failed: %s", owner, repo, resp.status_code
            )
            return None
        data = resp.json()
        return data["key_id"], data["key"]
    except Exception as exc:
        logger.warning("get public key for %s/%s errored: %s", owner, repo, exc)
        return None


def _detect_secret_names(owner: str, repo: str, token: str) -> set[str]:
    """Scan a repo's workflow files for ``secrets.<NAME>`` used alongside
    ``SNAPCRAFT_STORE_CREDENTIALS:``, to find any legacy secret name already
    in use (e.g. ``STORE_LOGIN``). Returns an empty set if none found."""
    names: set[str] = set()
    list_url = f"{_GH_API}/repos/{owner}/{repo}/contents/.github/workflows"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(list_url, headers=_headers(token))
            if resp.status_code != 200:
                return names
            entries = resp.json()
            if not isinstance(entries, list):
                return names
            for entry in entries:
                if entry.get("type") != "file":
                    continue
                name = entry.get("name", "")
                if not name.endswith((".yml", ".yaml")):
                    continue
                file_resp = client.get(entry["url"], headers=_headers(token))
                if file_resp.status_code != 200:
                    continue
                content_b64 = file_resp.json().get("content", "")
                try:
                    text = base64.b64decode(content_b64).decode("utf-8", errors="ignore")
                except Exception:
                    continue
                for match in _KNOWN_SECRET_NAME_PATTERN.finditer(text):
                    names.add(match.group(1))
    except Exception as exc:
        logger.warning("scanning workflows for %s/%s failed: %s", owner, repo, exc)
    return names


def set_repo_secret(
    owner: str, repo: str, secret_name: str, secret_value: str, token: str
) -> bool:
    """Create or update a single Actions secret on *owner/repo*."""
    key_info = _get_repo_public_key(owner, repo, token)
    if not key_info:
        return False
    key_id, public_key_b64 = key_info
    encrypted_value = encrypt_secret(public_key_b64, secret_value)
    url = f"{_GH_API}/repos/{owner}/{repo}/actions/secrets/{secret_name}"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.put(
                url,
                json={"encrypted_value": encrypted_value, "key_id": key_id},
                headers=_headers(token),
            )
        return resp.status_code in (201, 204)
    except Exception as exc:
        logger.warning(
            "set secret %s for %s/%s failed: %s", secret_name, owner, repo, exc
        )
        return False


def sync_snapcraft_credentials(
    repos: list[str], credential_value: str, token: str
) -> list[dict]:
    """Push *credential_value* out to every repo in *repos* as an Actions secret.

    Detects any legacy secret name already referenced by that repo's own
    workflows (in addition to the default ``SNAPCRAFT_STORE_CREDENTIALS``)
    and updates all of them, so existing publish workflows don't need to be
    edited.

    Returns one ``{"repo", "secret_names", "ok"}`` dict per repo.
    """
    results: list[dict] = []
    for repo in repos:
        owner_repo = parse_owner_repo(repo)
        if not owner_repo:
            results.append({"repo": repo, "secret_names": [], "ok": False})
            continue
        owner, name = owner_repo
        secret_names = _detect_secret_names(owner, name, token) or {DEFAULT_SECRET_NAME}
        ok = True
        for secret_name in secret_names:
            if not set_repo_secret(owner, name, secret_name, credential_value, token):
                ok = False
        results.append({"repo": repo, "secret_names": sorted(secret_names), "ok": ok})
    return results
