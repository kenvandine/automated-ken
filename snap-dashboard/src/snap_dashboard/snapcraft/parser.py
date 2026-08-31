"""Parse snapcraft.yaml to extract source parts and current versions."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SourcePart:
    """A part in snapcraft.yaml that has an upstream source."""

    part_name: str
    source: str = ""
    source_type: str = ""   # git | pypi | launchpad | tarball | local
    source_tag: str = ""
    source_branch: str = ""
    source_commit: str = ""
    current_version: str = ""  # from top-level `version:` or source_tag


def parse_snapcraft_yaml(content: str) -> list[SourcePart]:
    """Return SourcePart entries for all parts that have a trackable upstream.

    This is a best-effort YAML parser that works without importing PyYAML so
    it can run in environments where only the stdlib is available.  It handles
    the common snapcraft.yaml patterns used in canonical snap packaging.
    """
    try:
        import yaml  # type: ignore[import]
        data = yaml.safe_load(content)
        return _parse_from_dict(data)
    except Exception:
        pass

    # Fallback: regex-based extraction (handles most real-world snapcraft.yamls)
    return _parse_regex(content)


def _parse_from_dict(data: dict) -> list[SourcePart]:
    if not isinstance(data, dict):
        return []
    top_version = str(data.get("version", ""))
    parts_section = data.get("parts", {})
    if not isinstance(parts_section, dict):
        return []

    results: list[SourcePart] = []
    for part_name, part_data in parts_section.items():
        if not isinstance(part_data, dict):
            continue
        source = str(part_data.get("source", "")).strip()
        if not source or source in (".", "./"):
            continue

        source_type = str(part_data.get("source-type", "")).strip()
        source_tag = str(part_data.get("source-tag", "")).strip()
        source_branch = str(part_data.get("source-branch", "")).strip()
        source_commit = str(part_data.get("source-commit", "")).strip()

        # Infer source-type when not explicit
        if not source_type:
            source_type = _infer_source_type(source)

        # Derive current version: prefer source-tag, fall back to top-level version
        current_version = source_tag or top_version

        results.append(
            SourcePart(
                part_name=part_name,
                source=source,
                source_type=source_type,
                source_tag=source_tag,
                source_branch=source_branch,
                source_commit=source_commit,
                current_version=current_version,
            )
        )
    return results


def _parse_regex(content: str) -> list[SourcePart]:
    """Fallback regex parser for when PyYAML is unavailable."""
    results: list[SourcePart] = []

    top_version_m = re.search(r"^version:\s*['\"]?([^\s'\"]+)['\"]?", content, re.MULTILINE)
    top_version = top_version_m.group(1) if top_version_m else ""

    # Simpler approach: just match source: lines following a part header
    part_pattern = re.compile(r"^  ([A-Za-z0-9_\-]+):\s*$", re.MULTILINE)
    key_pattern = re.compile(r"^    (source(?:-type|-tag|-branch|-commit)?):\s*(.+)$")

    parts = list(part_pattern.finditer(content))
    in_parts_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "parts:":
            in_parts_section = True
            break

    if not in_parts_section:
        return results

    for i, m in enumerate(parts):
        end = parts[i + 1].start() if i + 1 < len(parts) else len(content)
        block = content[m.start():end]
        keys: dict[str, str] = {}
        for line in block.splitlines()[1:]:
            km = key_pattern.match(line)
            if km:
                keys[km.group(1)] = km.group(2).strip().strip("'\"")

        source = keys.get("source", "")
        if not source or source in (".", "./"):
            continue

        source_type = keys.get("source-type") or _infer_source_type(source)
        source_tag = keys.get("source-tag", "")
        current_version = source_tag or top_version

        results.append(
            SourcePart(
                part_name=m.group(1),
                source=source,
                source_type=source_type,
                source_tag=source_tag,
                source_branch=keys.get("source-branch", ""),
                source_commit=keys.get("source-commit", ""),
                current_version=current_version,
            )
        )
    return results


def _infer_source_type(source: str) -> str:
    if "github.com" in source or "gitlab.com" in source or source.endswith(".git"):
        return "git"
    if "launchpad.net" in source:
        return "launchpad"
    if "pypi.org" in source or source.startswith("pypi:"):
        return "pypi"
    if source.startswith("http") and any(
        source.endswith(ext) for ext in (".tar.gz", ".tar.xz", ".tar.bz2", ".zip")
    ):
        return "tarball"
    return "git"  # default assumption for bare URLs
