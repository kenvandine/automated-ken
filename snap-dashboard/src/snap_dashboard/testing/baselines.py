"""Helpers for durable stable screenshot baselines."""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx

from snap_dashboard.db.models import StableScreenshotBaseline, TestRun
from snap_dashboard.db.session import get_session
from snap_dashboard.github.pr_viewer import get_pr_details


@dataclass
class ScreenshotAsset:
    """A screenshot captured by a YARF test run."""

    image_name: str
    image_url: str
    image_b64: str

    @property
    def image_bytes(self) -> bytes:
        return base64.b64decode(self.image_b64)


def load_test_run_screenshots(
    testing_repo: str,
    pr_number: int | None,
    token: str = "",
) -> list[ScreenshotAsset]:
    """Load screenshots for a test PR and return them with durable image bytes."""
    if not testing_repo or not pr_number:
        return []

    pr_data = get_pr_details(testing_repo, pr_number, token)
    pr = pr_data.get("pr", {})
    files = pr_data.get("files", [])
    head_sha = pr.get("head", {}).get("sha", "")
    if not head_sha:
        return []

    owner, _, repo = testing_repo.partition("/")
    assets: list[ScreenshotAsset] = []
    for file_data in files:
        filename = file_data.get("filename", "")
        if not filename.lower().endswith(".png") or "results/" not in filename:
            continue
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{head_sha}/{filename}"
        image_bytes = _download_image(raw_url, token)
        if not image_bytes:
            continue
        assets.append(
            ScreenshotAsset(
                image_name=filename.rsplit("/", 1)[-1],
                image_url=raw_url,
                image_b64=base64.b64encode(image_bytes).decode(),
            )
        )

    return assets


def persist_stable_baseline_for_run(
    test_run_id: int,
    testing_repo: str,
    token: str = "",
) -> int:
    """Persist the promoted run's screenshots as the durable stable baseline."""
    with get_session() as session:
        run = session.query(TestRun).get(test_run_id)
        if not run:
            return 0
        snap_name = run.snap_name
        architecture = run.architecture or "amd64"
        user_id = run.user_id
        version = run.version or ""
        revision = run.revision
        pr_number = run.pr_number

    assets = load_test_run_screenshots(testing_repo, pr_number, token)
    if not assets:
        return 0

    with get_session() as session:
        session.query(StableScreenshotBaseline).filter_by(
            user_id=user_id,
            snap_name=snap_name,
            architecture=architecture,
        ).delete()

        for asset in assets:
            session.add(
                StableScreenshotBaseline(
                    user_id=user_id,
                    snap_name=snap_name,
                    architecture=architecture,
                    image_name=asset.image_name,
                    image_url=asset.image_url,
                    image_b64=asset.image_b64,
                    source_test_run_id=test_run_id,
                    source_version=version,
                    source_revision=revision,
                )
            )

    return len(assets)


def get_stable_baseline_assets(
    user_id: int | None,
    snap_name: str,
    architecture: str,
) -> list[ScreenshotAsset]:
    """Return the stored stable baseline screenshots for a snap/architecture."""
    with get_session() as session:
        rows = (
            session.query(StableScreenshotBaseline)
            .filter_by(
                user_id=user_id,
                snap_name=snap_name,
                architecture=architecture or "amd64",
            )
            .order_by(StableScreenshotBaseline.image_name.asc())
            .all()
        )
        return [
            ScreenshotAsset(
                image_name=row.image_name,
                image_url=row.image_url or "",
                image_b64=row.image_b64,
            )
            for row in rows
        ]


def get_or_build_stable_baseline_assets(
    user_id: int | None,
    snap_name: str,
    architecture: str,
    testing_repo: str,
    token: str = "",
) -> list[ScreenshotAsset]:
    """Return the stored baseline, backfilling from the latest promoted run if needed."""
    assets = get_stable_baseline_assets(user_id, snap_name, architecture)
    if assets:
        return assets

    with get_session() as session:
        run = (
            session.query(TestRun)
            .filter_by(
                user_id=user_id,
                snap_name=snap_name,
                architecture=architecture or "amd64",
                promoted=True,
            )
            .order_by(TestRun.promoted_at.desc(), TestRun.finished_at.desc())
            .first()
        )
        test_run_id = run.id if run else None

    if not test_run_id:
        return []

    persist_stable_baseline_for_run(test_run_id, testing_repo, token)
    return get_stable_baseline_assets(user_id, snap_name, architecture)


def pair_screenshots(
    baseline_assets: list[ScreenshotAsset],
    new_assets: list[ScreenshotAsset],
) -> list[tuple[ScreenshotAsset, ScreenshotAsset]]:
    """Pair screenshots by file name, falling back to the first screenshot in each set."""
    baseline_by_name = {asset.image_name: asset for asset in baseline_assets}
    new_by_name = {asset.image_name: asset for asset in new_assets}

    pairs = [
        (baseline_by_name[name], new_by_name[name])
        for name in sorted(set(baseline_by_name) & set(new_by_name))
    ]
    if pairs:
        return pairs
    if baseline_assets and new_assets:
        return [(baseline_assets[0], new_assets[0])]
    return []


def _download_image(url: str, token: str = "") -> bytes | None:
    headers: dict[str, str] = {}
    if token and "github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.content
    except Exception:
        return None
    return None
