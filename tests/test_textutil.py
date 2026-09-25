"""textutil — raw terminal output → the text a person reads off the screen."""

from __future__ import annotations

from terminal.textutil import alt_screen_after, clean, tail_lines


def test_colours_and_cursor_codes_are_stripped():
    assert clean("\x1b[31mred\x1b[0m \x1b[1;32mbold green\x1b[m") == "red bold green"
    assert clean("a\x1b[2Kb\x1b[10;5Hc") == "abc"


def test_osc_titles_and_hyperlinks_are_stripped():
    assert clean("\x1b]0;my title\x07prompt$ ") == "prompt$"
    assert clean("\x1b]8;;https://x.test\x1b\\link\x1b]8;;\x1b\\ done") == "link done"


def test_carriage_return_redraws_collapse_to_the_final_state():
    # a progress bar redrawn in place
    assert clean("[#   ] 25%\r[##  ] 50%\r[####] 100%\n") == "[####] 100%\n"
    # CRLF is just a newline
    assert clean("one\r\ntwo\r\n") == "one\ntwo\n"


def test_backspace_overstrikes():
    assert clean("abc\b\bXY") == "aXY"


def test_tail_lines_drops_trailing_blanks():
    assert tail_lines("a\nb\nc\n\n\n", 2) == "b\nc"


def test_alt_screen_tracking():
    assert alt_screen_after("\x1b[?1049h", False) is True
    assert alt_screen_after("\x1b[?1049hvim…\x1b[?1049l", False) is False
    assert alt_screen_after("plain output", True) is True
