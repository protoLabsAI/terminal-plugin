"""Packaging + contract tests — manifest shape, version coherence, the view page's
four-rules + theme contract, and that register() mounts the router host-free."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _manifest():
    return yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())


def test_manifest_shape():
    m = _manifest()
    assert m["id"] == "terminal"
    assert m["enabled"] is True  # on by default — the WS bearer gate is the protection
    assert m["config_section"] == "terminal"
    for key in ("shell", "cwd"):
        assert key in m["config"]


def test_manifest_and_pyproject_versions_agree():
    m = _manifest()
    pp = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert m["version"] == pp["project"]["version"]


def test_view_path_is_public_and_base_safe():
    view = _manifest()["views"][0]
    assert view["id"] == "terminal"
    assert view["path"] == "/plugins/terminal/view"  # public, not /api/plugins/…
    assert view["path"].split("/plugins/")[0] == ""


def test_view_page_pulls_in_the_protoagent_theme_and_four_rules():
    from terminal.view import PAGE

    # rule 3 (slug base) + rule 4 (DS kit) — the gated channel is the WS, not apiFetch.
    assert 'location.pathname.split("/plugins/")' in PAGE
    assert "/_ds/plugin-kit.css" in PAGE and "/_ds/plugin-kit.js" in PAGE
    # the terminal itself: VENDORED xterm (offline — no CDN) + the WS to the PTY bridge.
    assert "/plugins/terminal/static/" in PAGE and "xterm.js" in PAGE and "new Terminal(" in PAGE
    assert "cdn.jsdelivr" not in PAGE and "https://" not in PAGE  # fully self-served, no CDN
    assert "/plugins/terminal/ws?token=" in PAGE
    # THE theme requirement: xterm's theme is built from protoAgent's --pl-* tokens,
    # and re-applied live on a re-theme (MutationObserver on :root).
    assert "--pl-color-bg" in PAGE and "--pl-color-fg" in PAGE and "--pl-color-accent" in PAGE
    assert "xtermTheme()" in PAGE and "MutationObserver" in PAGE
    assert "options.theme" in PAGE  # re-applies the theme, not just on first paint


def test_register_mounts_the_public_router(registry):
    import terminal

    terminal.register(registry)
    assert "/plugins/terminal" in registry.routers  # the public view + WS router
