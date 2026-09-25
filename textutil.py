"""Terminal output → plain text the agent can read. Pure; no host, no PTY.

The replay buffer holds the RAW stream: colour codes, cursor moves, OSC titles and
hyperlinks, carriage-return redraws (progress bars, spinners), backspaces. ``clean``
turns it into what a person would read off the screen for line-oriented output:
escape sequences dropped, each ``\\r`` redraw collapsed to its final state, ``\\b``
applied. (A full-screen program's raw stream can't be reconstructed this way — callers
say so rather than pretend; see ``Session.alt_screen``.)
"""

from __future__ import annotations

import re

# CSI (colours, cursor moves, modes) · OSC (titles, hyperlinks, cwd reports) terminated
# by BEL or ST · DCS/SOS/PM/APC strings · two-char escapes (charset selection, keypad…).
_ANSI = re.compile(
    r"""
    \x1b\[[0-?]*[ -/]*[@-~]              # CSI
  | \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?   # OSC … BEL|ST
  | \x1b[PX^_][^\x1b]*(?:\x1b\\)?        # DCS / SOS / PM / APC … ST
  | \x1b[()*+][0-9A-Za-z]                # charset designation
  | \x1b[@-Z\\-_=>78]                    # other two-char escapes
    """,
    re.VERBOSE,
)
_CTRL = re.compile(r"[\x00-\x07\x0b\x0c\x0e-\x1f\x7f]")  # keep \b \t \n \r (handled below)


def _overstrike(line: str) -> str:
    """Apply ``\\r`` (back to column 0, later text overwrites) and ``\\b`` (back one)."""
    out: list[str] = []
    col = 0
    for ch in line:
        if ch == "\r":
            col = 0
        elif ch == "\b":
            col = max(0, col - 1)
        else:
            if col < len(out):
                out[col] = ch
            else:
                out.append(ch)
            col += 1
    return "".join(out).rstrip()


def clean(raw: str) -> str:
    """Raw terminal output → readable plain text."""
    text = _ANSI.sub("", raw)
    text = _CTRL.sub("", text)
    return "\n".join(_overstrike(line) for line in text.replace("\r\n", "\n").split("\n"))


def tail_lines(text: str, n: int) -> str:
    """The last ``n`` lines, with trailing blank lines dropped first."""
    lines = text.rstrip("\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines[-max(1, n) :])


# Alt-screen switches (vim, less, htop, most TUIs): 1049/1047/47, set (h) or reset (l).
_ALT = re.compile(r"\x1b\[\?(?:1049|1047|47)([hl])")


def alt_screen_after(raw: str, current: bool) -> bool:
    """Whether the terminal is on the alternate screen after ``raw``, given ``current``."""
    last = None
    for m in _ALT.finditer(raw):
        last = m.group(1)
    return current if last is None else last == "h"
