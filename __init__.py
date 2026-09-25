"""terminal — a full terminal (xterm.js + a real PTY over WebSocket) in the console.

``register()`` mounts ONE router under the PUBLIC ``/plugins/terminal`` prefix: the
view page (an iframe page-load can't carry a bearer, so the page must be public) and
the WebSocket, which verifies the operator bearer itself from the socket's first
``auth`` frame (a browser WS can't set an Authorization header, and a ``?token=`` would
leak into access logs). No tools — it's a view + a
PTY bridge, plus a stop-only surface that kills the (now socket-independent) shells on
server shutdown. Enabled by default — the WS bearer gate is the protection, and an un-gated
shell is only ever loopback-local (protoAgent requires a token to bind non-loopback).
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.terminal")


def register(registry) -> None:
    cfg = registry.config or {}
    # live_config (host >= 0.71) re-reads the section per call, so Settings edits reach
    # the next view load / new shell with no restart; older hosts get the snapshot.
    live = getattr(registry, "live_config", None) or (lambda: registry.config or {})
    try:
        from .api import build_router

        registry.register_router(build_router(live), prefix="/plugins/terminal")
    except Exception:  # noqa: BLE001 — the router is best-effort
        log.exception("[terminal] mounting the terminal router failed")
    # Shells outlive their sockets now (sessions.py), so the server's shutdown must
    # take them down — a surface with only a stop hook does exactly that.
    try:
        from .sessions import MANAGER

        registry.register_surface(lambda: None, stop=MANAGER.close_all, name="terminal-sessions")
    except Exception:  # noqa: BLE001
        log.exception("[terminal] registering the session shutdown hook failed")
    log.info("[terminal] registered (shell=%s)", cfg.get("shell") or "$SHELL")
