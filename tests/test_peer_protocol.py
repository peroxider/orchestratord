"""PeerFrame encode/decode coverage for ``peer/1`` (DESIGN AC2).

All 11 frame types must round-trip through encode→decode losslessly,
and corrupted wire input must fail loudly: missing type, unknown type,
non-object JSON, truncated JSON, mismatched protocol_version, and a
non-numeric timestamp.
"""

from __future__ import annotations

import json

import pytest

from orchestratord.peer.protocol import (
    PROTOCOL_VERSION,
    PeerFrame,
    PeerFrameType,
)


def _sample(ftype: PeerFrameType) -> PeerFrame:
    if ftype is PeerFrameType.HELLO:
        return PeerFrame.hello(
            orch_id="orch-A1", capabilities=["peer.invoke", "sessions.read"]
        )
    if ftype is PeerFrameType.WELCOME:
        return PeerFrame.welcome(orch_id="orch-A1", capabilities=["peer.subscribe"])
    if ftype is PeerFrameType.INVOKE:
        return PeerFrame.invoke(
            orch_id="orch-A1",
            request_id="req-1",
            method="POST /api/sessions/{id}/messages",
            body={"content": "hello from A1"},
            headers={"X-Custom": "v"},
            msg_id="m-1",
            ordering="7",
        )
    if ftype is PeerFrameType.RESULT:
        return PeerFrame.result(
            orch_id="orch-A1", request_id="req-1", status=200,
            body={"ok": True}, msg_id="m-1",
        )
    if ftype is PeerFrameType.EVENT:
        return PeerFrame.event(
            orch_id="orch-A1", topic="peer.agent.a1.events", payload={"seq": 1}
        )
    if ftype in (PeerFrameType.SUBSCRIBE, PeerFrameType.UNSUBSCRIBE):
        return PeerFrame(type=ftype, orch_id="orch-A1", topic="session.*")
    if ftype is PeerFrameType.PING:
        return PeerFrame.ping(orch_id="orch-A1")
    if ftype is PeerFrameType.PONG:
        return PeerFrame.pong(orch_id="orch-A1")
    if ftype is PeerFrameType.REVOKE:
        return PeerFrame(type=ftype, orch_id="orch-A1")
    return PeerFrame.goodbye(orch_id="orch-A1", in_flight=2)


@pytest.mark.parametrize("ftype", list(PeerFrameType))
def test_roundtrip_all_frame_types(ftype: PeerFrameType) -> None:
    frame = _sample(ftype)
    decoded = PeerFrame.decode(frame.encode())
    assert decoded == frame
    assert decoded.type is ftype


def test_protocol_version_is_peer_1() -> None:
    frame = PeerFrame.ping(orch_id="orch-A1")
    assert frame.protocol_version == PROTOCOL_VERSION == "peer/1"
    assert PeerFrame.decode(frame.encode()).protocol_version == "peer/1"


# -- D18: INVOKE/RESULT carry msg_id --

def test_invoke_fills_msg_id_when_missing() -> None:
    f1 = PeerFrame.invoke(orch_id="o", request_id="r", method="GET /x")
    f2 = PeerFrame.invoke(orch_id="o", request_id="r", method="GET /x")
    assert f1.msg_id and f2.msg_id
    assert f1.msg_id != f2.msg_id


def test_result_echoes_invoke_msg_id() -> None:
    invoke = PeerFrame.invoke(
        orch_id="o", request_id="r", method="GET /x", msg_id="m-fixed"
    )
    echo = PeerFrame.result(
        orch_id="o", request_id="r", status=200, msg_id=invoke.msg_id
    )
    assert echo.msg_id == invoke.msg_id


# -- D19: ordering round-trips as an opaque string --

def test_ordering_roundtrip() -> None:
    frame = PeerFrame.invoke(
        orch_id="o", request_id="r", method="POST /api/s/x/messages", ordering="42"
    )
    data = json.loads(frame.encode())
    assert data["ordering"] == "42"
    assert PeerFrame.decode(frame.encode()).ordering == "42"


# -- D24: GOODBYE carries in_flight --

def test_goodbye_carries_in_flight() -> None:
    frame = PeerFrame.goodbye(orch_id="orch-A1", in_flight=3)
    data = json.loads(frame.encode())
    assert data["in_flight"] == 3
    assert PeerFrame.decode(frame.encode()).in_flight == 3


def test_encode_is_single_line_jsonl() -> None:
    encoded = PeerFrame.ping(orch_id="o").encode()
    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert json.loads(encoded)["type"] == "ping"


def test_frame_ids_and_timestamps_auto_generated() -> None:
    a, b = PeerFrame.ping(orch_id="o"), PeerFrame.ping(orch_id="o")
    assert a.frame_id != b.frame_id
    assert a.timestamp > 0


# -- corrupted wire input --

def test_decode_rejects_non_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        PeerFrame.decode("[1, 2, 3]")


def test_decode_rejects_missing_type() -> None:
    with pytest.raises(ValueError, match="missing 'type'"):
        PeerFrame.decode('{"frame_id": "f-1"}')


def test_decode_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown frame type"):
        PeerFrame.decode('{"type": "teleport"}')


def test_decode_rejects_truncated_json() -> None:
    with pytest.raises(ValueError):
        PeerFrame.decode(b'{"type": "ping"')


def test_decode_rejects_wrong_protocol_version() -> None:
    with pytest.raises(ValueError, match="protocol_version"):
        PeerFrame.decode('{"type": "ping", "protocol_version": "gateway/1"}')


def test_decode_rejects_non_numeric_timestamp() -> None:
    with pytest.raises(ValueError, match="timestamp"):
        PeerFrame.decode('{"type": "ping", "timestamp": "soon"}')


def test_decode_rejects_invalid_utf8() -> None:
    # UnicodeDecodeError subclasses ValueError.
    with pytest.raises(ValueError):
        PeerFrame.decode(b'{"type": "ping\xff"}')


# -- verifier findings: empty body / null frame_id on the wire --

def test_empty_body_round_trips() -> None:
    # body={} is a legal §4.2 value and part of the MAC input (D3);
    # to_dict must not drop it (F1 regression).
    frame = PeerFrame.invoke(orch_id="o", request_id="r", method="GET /x", body={})
    data = json.loads(frame.encode())
    assert data["body"] == {}
    assert PeerFrame.decode(frame.encode()).body == {}


def test_body_none_omitted_from_wire() -> None:
    frame = PeerFrame.ping(orch_id="o")
    assert "body" not in json.loads(frame.encode())


def test_decode_tolerates_null_frame_id_with_fresh_id() -> None:
    # A null frame_id on the wire must not crash sign/verify later (F2);
    # decode fills a fresh id, so a signed frame then fails the MAC
    # cleanly instead of raising AttributeError.
    frame = PeerFrame.decode('{"type": "ping", "frame_id": null}')
    assert frame.frame_id


def test_decode_rejects_non_string_frame_id() -> None:
    with pytest.raises(ValueError, match="frame_id"):
        PeerFrame.decode('{"type": "ping", "frame_id": 7}')


def test_decode_generates_frame_id_when_missing() -> None:
    assert PeerFrame.decode('{"type": "ping"}').frame_id
