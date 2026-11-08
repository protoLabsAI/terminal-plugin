"""First mount must not run WebGL init on the console's main thread (stress test of
protoAgent v0.231.0: the Terminal view's first mount blocked the console ~670 ms).

The view is a same-origin iframe, so it shares the console's event loop. Profiling
(Playwright, Chromium trace) put the whole stall inside the WebGL addon's activate —
context creation, shader compile/link — and blocking addon-webgl.js alone took the
mount's max main-thread lag from ~140-200 ms to ~3 ms. So the bootstrap must not wait for
the WebGL addon, and a new pane must open on the canvas renderer and reach WebGL only
through the deferred upgrade queue (web/logic.js createUpgradeQueue, node-tested in
tests/js/renderer.test.mjs).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _bootstrap() -> str:
    from terminal.view import PAGE

    return PAGE[PAGE.index('<script type="module">') :]


def test_bootstrap_does_not_wait_for_the_webgl_addon():
    boot = _bootstrap()
    # every `await` in the bootstrap — none of them may name the WebGL addon
    for line in boot.splitlines():
        if "await" in line:
            assert "addon-webgl" not in line, line
    # it is still loaded (served, offline) and handed to the app as a promise
    assert 'loadScript(ST + "addon-webgl.js")' in boot
    assert re.search(r"\bwebgl,", boot)
    # the cheap renderer and the behaviour addons are still ready before boot
    assert "addon-canvas.js" in boot and "addon-unicode11.js" in boot and "addon-search.js" in boot


def test_a_new_pane_opens_on_canvas_and_reaches_webgl_only_via_the_queue():
    app = (ROOT / "web" / "terminal.js").read_text()
    new_pane = app[app.index("function newPane(") : app.index("function disposePane(")]
    assert "WebglAddon" not in new_pane
    assert "attachRenderer(p)" in new_pane
    attach = app[app.index("function attachRenderer(") :]
    attach = attach[: attach.index("\n  }\n")]
    assert "attachCanvas(p)" in attach and "upgrades.add(p)" in attach
    # the one place a WebglAddon is constructed is the upgrade the queue runs
    assert app.count("new WebglAddon(") == 1
    upgrade = app[app.index("function upgradeToWebgl(") : app.index("const upgrades = createUpgradeQueue(")]
    assert "new WebglAddon(" in upgrade
    assert "upgrade: upgradeToWebgl" in app and "schedule: afterPaint" in app
    # context loss still falls back to canvas
    assert "onContextLoss" in upgrade and "attachCanvas(p)" in upgrade
