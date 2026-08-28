"""Manual E2E: opencode serve SSE integration.

Requires a real ``opencode`` binary on PATH.
Run manually with:

    pytest tests/manual_e2e_opencode_sse.py -v --no-skip

Verifies that:
1. ``opencode serve --port 0`` starts and the port is discovered.
2. A ``POST /v1/chat`` with ``Accept: text/event-stream`` returns SSE
   frames that are translated into SPI events.
3. The event stream contains at least one content-bearing event
   (TEXT_DELTA or TEXT) and the terminal events.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind
from orchestratord_opencode.session import OpenCodeSession


pytestmark = [
    pytest.mark.skip(reason="manual E2E — requires real opencode binary on PATH"),
    # The conftest PATH guard otherwise blocks every backend CLI;
    # tests here intentionally need the real binary.
    pytest.mark.uses_real_cli,
]


def _has_opencode() -> bool:
    return shutil.which("opencode") is not None


@pytest.mark.skipif(not _has_opencode(), reason="opencode not on PATH")
def test_opencode_sse_send_receives_events() -> None:
    """Send a trivial prompt and assert the event stream contains at least
    one content event and the terminal pair."""
    session = OpenCodeSession(SessionSpec(cwd="/tmp"))

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


@pytest.mark.skipif(not _has_opencode(), reason="opencode not on PATH")
def test_opencode_sse_server_cleanup() -> None:
    """close() must terminate the opencode serve subprocess."""
    session = OpenCodeSession(SessionSpec(cwd="/tmp"))

    async def run() -> None:
        await session.send("echo hi")
        async for _ in session.events():
            pass
        await session.close()
        assert session._proc is None or session._proc.returncode is not None

    asyncio.run(run())