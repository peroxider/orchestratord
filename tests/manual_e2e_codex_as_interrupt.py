"""Manual E2E: codex AppServer interrupt.

Requires a real ``codex`` binary with ``app-server`` subcommand on PATH.
Run manually with:

    pytest tests/manual_e2e_codex_as_interrupt.py -v --no-skip

Verifies that ``interrupt()`` calls ``WorkerManager.session_cancel`` without
crashing. The test does not assert that the agent actually stops mid-turn
(that depends on the codex runtime's cancellation semantics); it only
asserts that the SPI call does not raise.
"""

from __future__ import annotations

import asyncio

import pytest

from orchestratord.spi.backend import SessionSpec
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
    reason="codex app-server not available",
)
def test_codex_as_interrupt_does_not_raise() -> None:
    """Call interrupt() while a send is in-flight and assert it does not
    raise. The worker may not actually cancel (depends on codex runtime),
    but the SPI call itself must be safe."""
    session = CodexAppServerSession(
        SessionSpec(cwd="/tmp", model="claude-sonnet-4-6")
    )

    async def run() -> None:
        send_task = asyncio.create_task(session.send("write a haiku about testing"))
        await asyncio.sleep(1.0)
        await session.interrupt()
        try:
            await asyncio.wait_for(send_task, timeout=30.0)
        except asyncio.TimeoutError:
            pass
        async for _ in session.events():
            pass
        await session.close()

    asyncio.run(run())