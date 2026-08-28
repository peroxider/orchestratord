"""Manual E2E: codex AppServer session — smoke test.

Requires a real ``codex`` binary with ``app-server`` subcommand on PATH.
Run manually with:

    pytest tests/manual_e2e_codex_as_session.py -v --no-skip

Verifies that ``CodexAppServerSession``:
1. Starts a ``codex app-server --listen stdio://`` worker.
2. Sends a prompt and receives ``TEXT`` or ``TEXT_DELTA`` events.
3. Emits ``TURN_COMPLETE`` and ``SESSION_COMPLETE`` terminal events.
"""

from __future__ import annotations

import asyncio

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind
from orchestratord_codex.backend import _detect_runtime
from orchestratord_codex.app_server_session import CodexAppServerSession


pytestmark = [
    pytest.mark.skip(reason="manual E2E — requires real codex binary with app-server support"),
    pytest.mark.uses_real_cli,
]


def _has_codex_as() -> bool:
    return _detect_runtime() == "as"


@pytest.mark.skipif(
    not _has_codex_as(),
    reason="codex app-server not available (probe returned cli or no codex on PATH)",
)
def test_codex_as_send_receives_events() -> None:
    """Send a trivial prompt and assert the event stream contains at least
    TEXT/TEXT_DELTA and the terminal events."""
    session = CodexAppServerSession(SessionSpec(cwd="/tmp", model="claude-haiku-4-5"))

    async def run() -> None:
        await session.send("echo hello")
        kinds: list[EventKind] = []
        async for ev in session.events():
            kinds.append(ev.kind)
        await session.close()

        content_kinds = {EventKind.TEXT, EventKind.TEXT_DELTA}
        assert any(k in content_kinds for k in kinds), (
            f"expected TEXT or TEXT_DELTA, got {kinds}"
        )
        assert EventKind.TURN_COMPLETE in kinds
        assert EventKind.SESSION_COMPLETE in kinds

    asyncio.run(run())


@pytest.mark.skipif(
    not _has_codex_as(),
    reason="codex app-server not available",
)
def test_codex_as_session_close_cleans_up_worker() -> None:
    """close() must terminate the worker process without hanging."""
    session = CodexAppServerSession(SessionSpec(cwd="/tmp"))

    async def run() -> None:
        await session.send("echo hi")
        async for _ in session.events():
            pass
        await session.close()
        # Worker should be cleaned up after close.
        assert session._worker is None or not session._worker._running

    asyncio.run(run())