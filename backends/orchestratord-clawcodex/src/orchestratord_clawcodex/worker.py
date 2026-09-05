"""Private stdio host for one ClawCodex conversation. No provider calls at boot."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import threading
from typing import Any

import psutil

from orchestratord.process_control import ProcessTree
from orchestratord.spi.approval import ApprovalDecision, ApprovalPolicy
from orchestratord.spi.backend import SessionSpec


async def run(wire: Any) -> None:
    from orchestratord_clawcodex.session import ClawcodexSession

    inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    session = None
    forwarding = None

    def write(message: dict[str, Any]) -> None:
        wire.write(json.dumps(message) + "\n")
        wire.flush()

    def read() -> None:
        try:
            for line in sys.stdin:
                loop.call_soon_threadsafe(inbox.put_nowait, json.loads(line))
        finally:
            # A vanished daemon must not leave provider threads or tools alive.
            # Do not depend on the query's event loop: native startup may block it.
            try:
                for child in psutil.Process(os.getpid()).children():
                    try:
                        ProcessTree(child.pid).kill()
                    except psutil.NoSuchProcess:
                        pass
            finally:
                os._exit(1)

    async def forward() -> None:
        try:
            async for event in session.events():
                write({"type": "event", "seq": event.seq, "timestamp": event.timestamp,
                       "kind": event.kind.value, "payload": event.payload})
        finally:
            write({"type": "turn_end"})

    threading.Thread(target=read, daemon=True, name="clawcodex-control").start()
    try:
        while (message := await inbox.get()) is not None:
            command = message.get("command")
            response = {"type": "reply", "id": message["id"], "result": None}
            try:
                if command == "init":
                    if session is not None:
                        raise RuntimeError("worker already initialized")
                    values = message["spec"]
                    if values.get("approval") is not None:
                        values["approval"] = ApprovalPolicy(**values["approval"])
                    session = ClawcodexSession(SessionSpec(**values))
                elif session is None:
                    raise RuntimeError("worker is not initialized")
                elif command == "send":
                    if forwarding is not None and not forwarding.done():
                        raise RuntimeError("previous turn is still running")
                    await session.send(message["content"])
                    forwarding = asyncio.create_task(forward())
                elif command == "approve":
                    await session.approve(message["request_id"], ApprovalDecision(message["decision"]))
                elif command == "probe_resume":
                    response["result"] = (await session.probe_resume()).value
                elif command == "close":
                    await session.close()
                else:
                    raise ValueError(f"Unknown ClawCodex worker command: {command}")
            except (RuntimeError, ValueError, TypeError, ImportError) as exc:
                response["error"] = str(exc)
            write(response)
            if command == "close":
                break
    finally:
        if session is not None:
            await session.close()
        if forwarding is not None:
            await forwarding


def main() -> None:
    wire = sys.stdout
    # Runtime diagnostics cannot corrupt the private protocol stream.
    with contextlib.redirect_stdout(sys.stderr):
        asyncio.run(run(wire))


if __name__ == "__main__":
    main()
