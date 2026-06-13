"""terminal — a full terminal (xterm.js + a real PTY over WebSocket) in the console.

``register()`` mounts ONE router under the PUBLIC ``/plugins/terminal`` prefix: the
view page (an iframe page-load can't carry a bearer, so the page must be public) and
the WebSocket, which verifies the operator bearer itself from a ``?token=`` query
param (a browser WS can't set an Authorization header). No tools — it's a view + a
PTY bridge. Enabled by default — the WS bearer gate is the protection, and an un-gated
shell is only ever loopback-local (protoAgent requires a token to bind non-loopback).
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.terminal")


def register(registry) -> None:
    cfg = registry.config or {}
    try:
        from .api import build_router

        registry.register_router(build_router(cfg), prefix="/plugins/terminal")
    except Exception:  # noqa: BLE001 — the router is best-effort
        log.exception("[terminal] mounting the terminal router failed")
    log.info("[terminal] registered (shell=%s)", cfg.get("shell") or "$SHELL")
