"""Tests for snapcraft.yaml parsing (upstream release detection)."""

from __future__ import annotations

from snap_dashboard.snapcraft.parser import parse_snapcraft_yaml

SIMPLE_YAML = """\
name: gedit
version: git
summary: A text editor
description: |
  gedit is a text editor.
grade: stable
confinement: strict

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

NO_TAG_YAML = """\
name: foo
version: '1.2.3'

parts:
  foo:
    plugin: dump
    source: https://example.com/downloads/foo-1.2.3.tar.gz
"""


def test_parse_snapcraft_yaml_extracts_source_tag() -> None:
    parts = parse_snapcraft_yaml(SIMPLE_YAML)
    named = {p.part_name: p for p in parts}

    assert "gedit" in named
    gedit = named["gedit"]
    assert gedit.source_type == "git"
    assert gedit.source_tag == "46.0"
    assert gedit.current_version == "46.0"


def test_parse_snapcraft_yaml_skips_local_parts() -> None:
    parts = parse_snapcraft_yaml(SIMPLE_YAML)
    names = {p.part_name for p in parts}
    # the `plugins` part has source: . and should be skipped entirely
    assert "plugins" not in names


def test_parse_snapcraft_yaml_falls_back_to_top_level_version() -> None:
    parts = parse_snapcraft_yaml(NO_TAG_YAML)
    named = {p.part_name: p for p in parts}
    assert named["foo"].current_version == "1.2.3"
    assert named["foo"].source_type == "tarball"


def test_parse_snapcraft_yaml_handles_garbage_gracefully() -> None:
    assert parse_snapcraft_yaml("not: valid: yaml: at: all: [") == [] or isinstance(
        parse_snapcraft_yaml("not: valid: yaml: at: all: ["), list
    )
