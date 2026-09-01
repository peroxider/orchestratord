"""Transcript rows must carry role + claude-style content.

Historical defects:

* transcript rows were raw broadcast frames (no ``role`` field) →
  ``issue transcript --role assistant`` matched 0 of 123 rows;
* the transcript was only written when a control-socket client was
  attached, so ``issue tail`` / ``run logs`` found no file for a run
  without a socket — and nothing was written incrementally anyway.

The notification pump makes events flow through ``_process_events`` during the
turn; combined with the frame→message conversion here, the transcript
is written incrementally with rows the readers can filter.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestratord.runner_utils import _broadcast_to_socket
from orchestratord.spi.events import EventEnvelope, EventKind


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def _session() -> SimpleNamespace:
    return SimpleNamespace(run_id="run-t1", control_socket=None)


async def _broadcast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: EventEnvelope) -> Path:
    home = _home(tmp_path, monkeypatch)
    await _broadcast_to_socket(_session(), event)
    return home / ".orchestratord" / "sessions" / "run-t1" / "transcript.jsonl"


async def test_text_event_writes_assistant_role(tmp_path, monkeypatch) -> None:
    path = await _broadcast(
        tmp_path,
        monkeypatch,
        EventEnvelope(seq=1, timestamp=0, kind=EventKind.TEXT, payload={"text": "hello"}),
    )
    assert path.exists(), "transcript must be written without a control socket"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["role"] == "assistant"
    assert rows[0]["content"][0] == {"type": "text", "text": "hello"}


async def test_tool_call_event_writes_assistant_tool_use(tmp_path, monkeypatch) -> None:
    path = await _broadcast(
        tmp_path,
        monkeypatch,
        EventEnvelope(
            seq=1,
            timestamp=0,
            kind=EventKind.TOOL_CALL,
            payload={"call_id": "c1", "name": "bash", "arguments": {"cmd": "ls"}},
        ),
    )
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["role"] == "assistant"
    block = rows[0]["content"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "c1"
    assert block["name"] == "bash"


async def test_tool_result_event_writes_user_tool_result(tmp_path, monkeypatch) -> None:
    path = await _broadcast(
        tmp_path,
        monkeypatch,
        EventEnvelope(
            seq=1,
            timestamp=0,
            kind=EventKind.TOOL_RESULT,
            payload={"call_id": "c1", "ok": True, "output": "file-a\nfile-b"},
        ),
    )
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["role"] == "user"
    block = rows[0]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "c1"
    assert block["content"] == "file-a\nfile-b"


async def test_role_filter_finds_assistant_rows(tmp_path, monkeypatch) -> None:
    """The end-to-end read contract: role filter must match assistant rows."""
    path = await _broadcast(
        tmp_path,
        monkeypatch,
        EventEnvelope(seq=1, timestamp=0, kind=EventKind.TEXT, payload={"text": "hi"}),
    )
    await _broadcast_to_socket(
        _session(),
        EventEnvelope(
            seq=2,
            timestamp=0,
            kind=EventKind.TOOL_RESULT,
            payload={"call_id": "c1", "ok": True, "output": "out"},
        ),
    )
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assistant = [r for r in rows if r.get("role") == "assistant"]
    user = [r for r in rows if r.get("role") == "user"]
    assert len(assistant) == 1
    assert len(user) == 1
