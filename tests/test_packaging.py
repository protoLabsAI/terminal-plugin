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


def _app_js():
    return (ROOT / "web" / "terminal.js").read_text() + (ROOT / "web" / "logic.js").read_text()


def test_view_page_pulls_in_the_protoagent_theme_and_four_rules():
    from terminal.view import PAGE

    app = _app_js()
    # rule 3 (slug base) + rule 4 (DS kit) — the gated channel is the WS, not apiFetch.
    assert 'location.pathname.split("/plugins/")' in PAGE
    assert "/_ds/plugin-kit.css" in PAGE and "/_ds/plugin-kit.js" in PAGE
    # VENDORED xterm + the app modules, all self-served (offline — no CDN)
    assert "/plugins/terminal/static/" in PAGE and "xterm.js" in PAGE and 'import(ST + "terminal.js")' in PAGE
    for src in (PAGE, app):
        assert "cdn.jsdelivr" not in src and "https://" not in src
    assert "new Terminal(" in app
    # WebGL first, canvas fallback (on context loss too) + customGlyphs → flush block art
    assert "WebglAddon" in app and "onContextLoss" in app and "CanvasAddon" in app and "customGlyphs" in app
    # the bearer rides the socket's first frame — never the URL (logs, history, proxies)
    assert "/plugins/terminal/ws" in app and "ws?token=" not in app
    assert 'type: "auth", token: tok' in app
    # THE theme requirement: every colour from protoAgent's --pl-* tokens, re-applied live
    for tok in ("--pl-color-bg", "--pl-color-fg", "--pl-color-accent", "--pl-color-status-info"):
        assert tok in app
    assert "buildTheme(" in app and "MutationObserver" in app and "options.theme" in app


def test_view_page_has_multi_session_tabs():
    from terminal.view import PAGE

    app = _app_js()
    assert 'id="tabs"' in PAGE and 'id="newtab"' in PAGE
    assert "newTab" in app and "closeTab" in app and "switchTab" in app
    assert "const panes = new Map()" in app  # the pane registry (each pane = one shell)
    # split panes: a layout tree per tab, persisted by session id
    assert "splitPane" in app and "closePane" in app and "renderLayout" in app and "mapPanes" in app


def test_view_page_persists_sessions():
    from terminal.view import PAGE

    app = _app_js()
    # stays mounted while hidden (bridge background opt-in) so switching views keeps shells
    assert 'type: "protoagent:subscribe", patterns: ["terminal.#"], background: true' in app
    # remembers tabs + their server session ids across reloads, and reattaches by id
    assert "localStorage" in app and "session: p.sessionId" in app
    # the tab's × ends the shell explicitly (a bare disconnect only detaches)
    assert 'type: "close"' in app
    # config comes from the server, not hardcoded
    assert "__TERMINAL_CONFIG__" in PAGE and "scrollback: CFG.scrollback" in app and "CFG.fontSize" in app
    assert "prompt(" not in app  # inline rename — a sandboxed iframe can't rely on prompt()
    # a hidden pane is never fitted (it would shrink the shell to one row)
    assert "if (!p.el.offsetWidth || !p.el.offsetHeight) return;" in app


def test_renaming_a_tab_cannot_change_the_layout():
    """Regression (reported in the desktop app): double-click-to-rename grew the bar a few
    px, resizing every terminal; with WebKit's always-on scrollbars the page's 1px overflow
    then flickered a scrollbar in a refit loop."""
    from terminal.view import PAGE

    assert "html,body{margin:0;height:100%;" in PAGE and "overflow:hidden}" in PAGE.split("html,body{", 1)[1].split("}", 1)[0] + "}"
    assert "overflow-x:auto;overflow-y:hidden" in PAGE  # the tab strip never scrolls vertically
    assert ".tab{display:flex;align-items:center;gap:6px;height:22px;" in PAGE  # fixed tab height
    assert "height:16px;box-sizing:border-box" in PAGE  # the rename input fits inside it
    assert "#terms{position:relative;flex:1 1 auto;min-height:0;overflow:hidden}" in PAGE


def test_view_integrates_with_the_console():
    from terminal.view import PAGE

    app = _app_js()
    # unhandled chords go to the console (⌘⇧K palette, ⌘, settings…) via the bridge
    assert 'type: "protoagent:keydown"' in app and "attachCustomKeyEventHandler" in app
    # right-click opens the console's own context menu (ADR 0036)
    assert "protoagent:contextmenu:open" in app and "protoagent:contextmenu:action" in app
    # find bar + search addon; emoji/CJK widths via unicode11
    assert 'id="find"' in PAGE and "SearchAddon" in app and "findNext" in app
    assert 'activeVersion = "11"' in app
    # tabs follow program titles unless renamed
    assert "onTitleChange" in app


def test_every_static_file_the_page_loads_is_served():
    from terminal import api
    from terminal.view import PAGE

    import re

    names = set(re.findall(r'"([\w.-]+\.(?:js|css))"', PAGE)) - {"plugin-kit.js", "plugin-kit.css"}
    names |= {"logic.js", "layout.js"}  # imported by terminal.js
    assert names <= set(api._STATIC), names - set(api._STATIC)
    for name, (folder, _media) in api._STATIC.items():
        assert (folder / name).is_file(), name


def test_the_vendored_assets_are_auth_exempt():
    from terminal.view import PAGE

    # the page loads xterm from /plugins/terminal/static/ with no bearer — a token-gated
    # host must exempt that prefix or every asset 401s
    assert "/plugins/terminal/static/" in PAGE
    assert "/plugins/terminal/static/" in _manifest()["public_paths"]


def test_manifest_config_matches_the_code_defaults():
    from terminal.api import DEFAULTS

    assert _manifest()["config"] == DEFAULTS


def test_every_setting_is_a_config_key():
    m = _manifest()
    keys = [s["key"] for s in m["settings"]]
    assert set(keys) == set(m["config"])  # every knob is editable in Settings
    for s in m["settings"]:
        assert s["type"] in ("string", "number", "bool", "select") and s["label"] and s["description"]
        if s["type"] == "select":
            assert m["config"][s["key"]] in s["options"]


def test_register_mounts_the_public_router(registry):
    import terminal

    terminal.register(registry)
    assert "/plugins/terminal" in registry.routers  # the public view + WS router
    assert "terminal-sessions" in registry.surfaces  # shells are ended on shutdown
