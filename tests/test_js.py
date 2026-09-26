"""Run the view's JS unit tests (web/logic.js) under node's built-in runner, so a plain
`pytest` covers them too. Skipped when node isn't installed; CI installs it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_unit_tests_pass():
    files = sorted(str(p) for p in (ROOT / "tests" / "js").glob("*.test.mjs"))
    assert files
    r = subprocess.run(["node", "--test", *files], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_modules_parse():
    for name in ("terminal.js", "logic.js"):
        r = subprocess.run(["node", "--check", str(ROOT / "web" / name)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
