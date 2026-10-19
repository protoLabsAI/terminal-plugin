"""Test bootstrap — register the repo as the ``terminal`` package so the modules'
relative imports (``from .pty_session import …``) resolve with no protoAgent host.
Executing ``__init__.py`` is safe: its host-only imports live inside ``register()``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = "terminal"

if PKG not in sys.modules:
    _spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert _spec and _spec.loader
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[PKG] = _mod
    _spec.loader.exec_module(_mod)


class _Registry:
    """A fake registry — records what register() contributes, with no host."""

    def __init__(self):
        self.config = {}
        self.tools, self.routers, self.surfaces = [], [], []
        self.emitted, self.navigations = [], []

    def register_tool(self, t):
        self.tools.append(t)

    def register_tools(self, ts):
        self.tools.extend(ts)

    def emit(self, topic, data=None):
        self.emitted.append((topic, data or {}))

    def navigate(self, view=""):
        self.navigations.append(view)

    def register_router(self, router, prefix=""):
        self.routers.append(prefix)

    def register_surface(self, start, stop=None, name=None):
        self.surfaces.append(name)


@pytest.fixture
def registry():
    return _Registry()


@pytest.fixture(autouse=True)
def socket_guard_host(monkeypatch):
    """Host-free implementation of the core 0.210.0 ticket binding helpers."""
    import types

    host = types.ModuleType("a2a_impl")
    guard = types.ModuleType("a2a_impl.socket_guard")

    def request_device_id(request):
        did = getattr(request.state, "device_id", None)
        return did if isinstance(did, str) and did else None

    def bind_device(conn, device_id):
        if device_id:
            conn.scope.setdefault("state", {}).setdefault("device_id", str(device_id))

    guard.request_device_id = request_device_id
    guard.bind_device = bind_device
    host.socket_guard = guard
    monkeypatch.setitem(sys.modules, "a2a_impl", host)
    monkeypatch.setitem(sys.modules, "a2a_impl.socket_guard", guard)
    return guard
