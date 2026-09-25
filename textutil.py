"""Terminal output helpers. Pure; no host, no PTY."""

from __future__ import annotations

import re

# Alt-screen switches (vim, less, htop, most TUIs): 1049/1047/47, set (h) or reset (l).
_ALT = re.compile(r"\x1b\[\?(?:1049|1047|47)([hl])")


def alt_screen_after(raw: str, current: bool) -> bool:
    """Whether the terminal is on the alternate screen after ``raw``, given ``current``."""
    last = None
    for m in _ALT.finditer(raw):
        last = m.group(1)
    return current if last is None else last == "h"
