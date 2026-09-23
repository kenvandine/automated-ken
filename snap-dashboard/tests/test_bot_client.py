"""Tests for snapcraft.yaml patching used by the version bumper agent."""

from __future__ import annotations

from snap_dashboard.github.bot_client import patch_snapcraft_yaml

SNAPCRAFT_YAML = """\
name: gedit
version: git
summary: A text editor

parts:
  gedit:
    plugin: meson
    source: https://gitlab.gnome.org/GNOME/gedit.git
    source-type: git
    source-tag: '46.0'
  plugins:
    plugin: nil
    source: .
"""


def test_patch_snapcraft_yaml_updates_source_tag() -> None:
    patched = patch_snapcraft_yaml(SNAPCRAFT_YAML, "gedit", "47.0")
    assert "source-tag: '47.0'" in patched
    assert "source-tag: '46.0'" not in patched


def test_patch_snapcraft_yaml_only_touches_target_part() -> None:
    two_part_yaml = SNAPCRAFT_YAML.replace(
        "  plugins:\n    plugin: nil\n    source: .\n",
        "  other:\n    plugin: dump\n    source: https://example.com/x.tar.gz\n"
        "    source-tag: '1.0'\n",
    )
    patched = patch_snapcraft_yaml(two_part_yaml, "gedit", "47.0")
    # gedit's tag is bumped...
    assert "source-tag: '47.0'" in patched
    # ...but the unrelated part's tag is untouched
    assert "source-tag: '1.0'" in patched


def test_patch_snapcraft_yaml_bumps_top_level_version() -> None:
    patched = patch_snapcraft_yaml(SNAPCRAFT_YAML, "gedit", "47.0")
    assert patched.splitlines()[1] == "version: 47.0"


def test_patch_snapcraft_yaml_no_matching_part_is_noop() -> None:
    patched = patch_snapcraft_yaml(SNAPCRAFT_YAML, "does-not-exist", "47.0")
    # Top-level version still gets bumped (existing behaviour), but the
    # source-tag for the (nonexistent) part is obviously untouched.
    assert "source-tag: '46.0'" in patched


def test_patch_snapcraft_yaml_preserves_double_quote_style() -> None:
    double_quoted = SNAPCRAFT_YAML.replace("source-tag: '46.0'", 'source-tag: "46.0"')
    patched = patch_snapcraft_yaml(double_quoted, "gedit", "47.0")
    assert 'source-tag: "47.0"' in patched
