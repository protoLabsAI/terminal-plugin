"""SessionManager / Session unit tests — the replay buffer, UTF-8 decoding across
reads, viewer handoff, and the detached-session reaper. Real PTYs, no host."""

from __future__ import annotations

import asyncio

import pytest

from terminal.sessions import Session, SessionLimitError, SessionManager


class _FakePty:
    """Feeds scripted chunks to the pump, then EOF."""

    shell, cwd = "/bin/fake", "/tmp"

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    async def read(self):
        await asyncio.sleep(0)
        return self._chunks.pop(0) if self._chunks else b""

    def poll(self):
        return 0

    async def aclose(self):
        self.closed = True


async def _run(sess):
    done = asyncio.Event()
    sess.start_pump(lambda _s: done.set())
    await asyncio.wait_for(done.wait(), 5)


async def test_a_multibyte_char_split_across_reads_decodes_cleanly():
    snow = "☃".encode()  # 3 bytes
    sess = Session("s", _FakePty([b"a" + snow[:1], snow[1:] + b"b"]))
    await _run(sess)
    assert sess.replay() == "a☃b"


async def test_the_replay_buffer_is_bounded_and_starts_on_a_line():
    lines = [f"line{i:04d}\n".encode() for i in range(1000)]
    sess = Session("s", _FakePty(lines), buffer_chars=1024)
    await _run(sess)
    out = sess.replay()
    assert len(out) <= 1024 and out.startswith("line") and out.endswith("line0999\n")


async def test_an_oversized_chunk_keeps_its_tail():
    sess = Session("s", _FakePty([b"x" * 5000 + b"\nEND"]), buffer_chars=1024)
    await _run(sess)
    assert sess.replay() == "END"


async def test_attach_replays_then_streams_and_a_takeover_kicks_the_old_viewer():
    sess = Session("s", _FakePty([]))
    sess._emit("earlier")
    a, b = asyncio.Queue(), asyncio.Queue()
    sess.attach(a, resumed=False)
    assert a.get_nowait()["type"] == "connected"
    assert a.get_nowait() == {"type": "data", "data": "earlier"}
    sess._emit("live")
    assert a.get_nowait() == {"type": "data", "data": "live"}
    sess.attach(b, resumed=True)
    assert a.get_nowait() == {"type": "detached", "reason": "attached elsewhere"}
    assert sess.detach(a) is False  # a takeover already replaced it — leave the session alone
    assert sess.detach(b) is True and not sess.attached


async def test_reaper_ends_only_long_detached_sessions():
    mgr = SessionManager()
    try:
        idle = mgr.create(shell="/bin/cat")
        watched = mgr.create(shell="/bin/cat")
        watched.attach(asyncio.Queue(), resumed=False)
        idle.detached_at -= 120  # detached two minutes ago
        assert await mgr.reap_detached(60) == 1
        assert mgr.get(idle.id) is None and mgr.get(watched.id) is not None
    finally:
        await mgr.close_all()


async def test_the_limit_refuses_another_shell():
    mgr = SessionManager()
    try:
        mgr.create(shell="/bin/cat", max_sessions=1)
        with pytest.raises(SessionLimitError):
            mgr.create(shell="/bin/cat", max_sessions=1)
    finally:
        await mgr.close_all()
    assert len(mgr) == 0


class _SizePty(_FakePty):
    def __init__(self):
        super().__init__([])
        self.sizes = []

    def resize(self, cols, rows):
        self.sizes.append((cols, rows))


async def test_a_resumed_viewer_nudges_the_size_so_tuis_repaint():
    pty = _SizePty()
    sess = Session("s", pty)
    sess._emit("\x1b[?1049h")  # vim / htop / a TUI agent is up (alternate screen)
    sess.attach(asyncio.Queue(), resumed=True)
    sess.resize(100, 30)
    await asyncio.sleep(0.15)
    # two changes → SIGWINCH even though the final size is unchanged
    assert pty.sizes == [(100, 29), (100, 30)]
    sess.resize(100, 30)  # only the first resize after a resume nudges
    assert pty.sizes[-1] == (100, 30) and len(pty.sizes) == 3


async def test_a_resume_at_a_plain_prompt_does_not_nudge():
    pty = _SizePty()
    sess = Session("s", pty)
    sess._emit("user@host ~ % ")
    sess.attach(asyncio.Queue(), resumed=True)
    sess.resize(100, 30)
    await asyncio.sleep(0.1)
    assert pty.sizes == [(100, 30)]  # no extra SIGWINCH → no reprinted prompt


async def test_a_fresh_viewer_does_not_nudge():
    pty = _SizePty()
    sess = Session("s", pty)
    sess.attach(asyncio.Queue(), resumed=False)
    sess.resize(100, 30)
    await asyncio.sleep(0.1)
    assert pty.sizes == [(100, 30)]


class _EndlessPty(_FakePty):
    """Output as fast as the pump will read it — a `yes`."""

    def __init__(self):
        super().__init__([])
        self.reads = 0

    async def read(self):
        self.reads += 1
        await asyncio.sleep(0)
        return b"y\n" * 100


async def test_a_slow_viewer_applies_backpressure_to_the_pty():
    from terminal.sessions import HIGH_WATER

    pty = _EndlessPty()
    sess = Session("s", pty)
    q = asyncio.Queue()
    sess.attach(q, resumed=False)
    sess.start_pump(lambda _s: None)
    try:
        await asyncio.sleep(0.3)  # nobody drains the queue
        stalled = pty.reads
        assert q.qsize() <= HIGH_WATER + 4  # bounded, not growing without limit
        await asyncio.sleep(0.2)
        assert pty.reads == stalled  # the pump stopped reading
        while not q.empty():  # the viewer catches up…
            q.get_nowait()
        await asyncio.sleep(0.1)
        assert pty.reads > stalled  # …and reading resumes
    finally:
        await sess.aclose()


async def test_a_detached_session_keeps_reading_into_its_buffer():
    pty = _EndlessPty()
    sess = Session("s", pty, buffer_chars=4096)
    sess.start_pump(lambda _s: None)
    try:
        await asyncio.sleep(0.1)
        assert pty.reads > 50  # no viewer → no backpressure; the ring buffer bounds memory
        assert len(sess.replay()) <= 4096
    finally:
        await sess.aclose()
