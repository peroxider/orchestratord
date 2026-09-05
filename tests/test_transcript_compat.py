"""Conversation boundary contracts, independent of installed agent backends."""

import copy
import json
import queue

import pytest

from orchestratord import event_tailer
from orchestratord.runner_utils import _transcript_message_from_frame
from orchestratord.transcript_compat import normalize_legacy_history


def wire(*events):
    return "".join(json.dumps(event) + "\n" for event in events)


def test_single_wire_records_between_operator_messages_keep_order():
    messages = [
        {"role": "assistant", "content": wire({"type": "thread.started"})},
        {"role": "user", "content": "Question", "origin": "followup"},
        {
            "role": "assistant",
            "content": wire(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Reply"},
                }
            ),
        },
    ]
    result = normalize_legacy_history(messages)
    assert [message["content"] for message in result] == ["Question", "Reply"]
    assert result[0]["origin"] == "followup"


def test_current_text_json_is_literal_in_persistence_history_and_evidence(tmp_path):
    text = wire(
        {"type": "thread.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "JSON example"},
        },
    )
    record = _transcript_message_from_frame(
        {"type": "TextDelta", "data": {"content": text}}
    )
    assert record["type"] == "TextDelta"
    assert normalize_legacy_history([record]) == [record]
    events = queue.Queue()
    tailer = event_tailer._SessionTailer(
        "run", "issue", tmp_path / "events.ndjson", events
    )
    tailer._process_assistant_message(record)
    event = events.get_nowait()
    assert event["event_type"] == "agent_text"
    assert event["data"]["content"] == text
    assert events.empty()


def test_legacy_chunks_preserve_tools_errors_and_source_timestamps():
    start = wire({"type": "thread.started"})
    events = wire(
        {
            "type": "item.started",
            "item": {"type": "command_execution", "id": "a", "command": "false"},
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "a",
                "command": "false",
                "aggregated_output": "failed output",
                "exit_code": 1,
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Reply"}},
        {"type": "turn.failed", "error": {"message": "Provider failed"}},
    )
    messages = [
        {"role": "assistant", "content": start, "ts": 1},
        {"role": "assistant", "content": events[:5], "ts": 2},
        {"role": "assistant", "content": events[5:], "ts": 3},
    ]
    original = copy.deepcopy(messages)
    result = normalize_legacy_history(messages)
    assert result[0]["content"][0] == {
        "type": "tool_use",
        "id": "a",
        "name": "Command",
        "input": "false",
    }
    assert result[0]["ts"] == 2
    assert result[1]["content"][0]["is_error"] is True
    assert result[1]["content"][0]["content"] == "failed output"
    assert result[2]["content"] == "Reply"
    assert result[3]["content"] == "Provider failed"
    assert result[3]["type"] == "Error"
    assert messages == original
    assert normalize_legacy_history(result) == result


@pytest.mark.parametrize("backend", ["codex", "dsh", "unregistered-agent"])
def test_common_messages_are_backend_independent(backend):
    messages = [
        {"role": "user", "type": "RunInput", "content": "Question", "backend": backend},
        {
            "role": "assistant",
            "type": "TextDelta",
            "content": wire(
                {"type": "thread.started"},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "JSON example"},
                },
            ),
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t", "name": "custom", "input": {"x": 1}}
            ],
        },
        {
            "role": "tool",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t",
                    "content": "ok",
                    "is_error": False,
                }
            ],
        },
    ]
    assert normalize_legacy_history(messages) == messages


@pytest.mark.parametrize(
    "text",
    [
        '{"type":"example","data":1}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"Example"}}',
        '{"type": []}\n{"type":{}}\n',
        '{"type":"item.completed","item":{"type":[]}}\n',
        '{"type":"unfinished',
        "Regular answer\nwith several lines",
    ],
)
def test_non_wire_text_is_lossless(text):
    messages = [{"role": "assistant", "content": text, "ts": 1}]
    assert normalize_legacy_history(messages) == messages


def test_unknown_and_malformed_lines_are_not_discarded():
    content = wire({"type": "thread.started"}, {"type": "turn.started"})
    content += '{"type":"future.event","extra":true}\nmalformed {\n'
    result = normalize_legacy_history([{"role": "assistant", "content": content}])
    assert (
        "".join(message["content"] for message in result)
        == '{"type":"future.event","extra":true}\nmalformed {\n'
    )


def test_read_history_normalizes_legacy_but_keeps_typed_text_and_disk(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(event_tailer, "SESSIONS_DIR", tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    legacy = wire(
        {"type": "thread.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Legacy reply"},
        },
    )
    records = [
        {
            "role": "user",
            "type": "RunInput",
            "content": "Task",
            "origin": "orchestrator",
            "system_prompt": "Rules",
        },
        {"role": "assistant", "content": legacy, "ts": 1},
        {"type": "TextDelta", "data": {"content": legacy}, "ts": 1},
        {
            "type": "ToolCallEvent",
            "data": {
                "tool_use_id": "x",
                "tool_name": "test",
                "params": {"arg": "value"},
            },
        },
        {
            "type": "ToolResultEvent",
            "data": {
                "tool_use_id": "x",
                "result": {"output": "bad", "exit_code": 2},
                "is_error": True,
            },
        },
    ]
    source = wire(*records)
    path = run / "transcript.jsonl"
    path.write_text(source)
    history = event_tailer.read_history_direct("run")
    assert history[0]["system_prompt"] == "Rules"
    assert history[1]["content"] == "Legacy reply"
    assert history[2]["content"] == legacy
    assert history[3]["content"][0]["input"] == {"arg": "value"}
    assert history[4]["content"][0]["is_error"] is True
    assert history[4]["content"][0]["exit_code"] == 2
    assert path.read_text() == source
