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

    def register_tool(self, t):
        self.tools.append(t)

    def register_router(self, router, prefix=""):
        self.routers.append(prefix)

    def register_surface(self, start, stop=None, name=None):
        self.surfaces.append(name)


@pytest.fixture
def registry():
    return _Registry()
