"""hmac_sig sign/verify coverage (DESIGN AC3).

Same input must yield the same signature; any tampering with the MAC
inputs (frame_id, body) or with the signature itself must fail verify;
the ±60s timestamp window is enforced on both sides; and a nonce that
has already been seen is rejected as a replay.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from orchestratord.peer.hmac_sig import (
    TIMESTAMP_WINDOW_SECONDS,
    PeerAuthError,
    sign,
    signature_payload,
    verify,
)
from orchestratord.peer.nonce_store import NonceStore
from orchestratord.peer.protocol import PeerFrame, PeerFrameType

TOKEN = "peer-token-a1b1"


def _invoke_frame(**overrides: object) -> PeerFrame:
    kwargs: dict[str, object] = {
        "type": PeerFrameType.INVOKE,
        "orch_id": "orch-A1",
        "frame_id": "frame-1",
        "request_id": "req-1",
        "method": "POST /api/sessions/x/messages",
        "body": {"content": "hi"},
        "msg_id": "m-1",
        "ordering": "1",
        "nonce": "nonce-1",
    }
    kwargs.update(overrides)
    return PeerFrame(**kwargs)  # type: ignore[arg-type]


def _store(tmp_path) -> NonceStore:
    return NonceStore(tmp_path / "nonces.db")


async def test_sign_then_verify_passes(tmp_path) -> None:
    frame = sign(_invoke_frame(), TOKEN)
    await verify(frame, TOKEN, _store(tmp_path))  # must not raise


def test_same_input_same_signature() -> None:
    fixed_ts = 1_757_328_000.0
    f1 = sign(_invoke_frame(timestamp=fixed_ts), TOKEN)
    f2 = sign(_invoke_frame(timestamp=fixed_ts), TOKEN)
    assert f1.signature == f2.signature
    other = sign(_invoke_frame(timestamp=fixed_ts), "other-token")
    assert other.signature != f1.signature


def test_sign_generates_nonce_when_missing() -> None:
    frame = PeerFrame(type=PeerFrameType.PING, orch_id="orch-A1")
    assert frame.nonce is None
    sign(frame, TOKEN)
    assert frame.nonce and len(frame.nonce) >= 16


def test_sign_refuses_frame_without_orch_id() -> None:
    with pytest.raises(PeerAuthError, match="orch_id"):
        sign(PeerFrame(type=PeerFrameType.PING), TOKEN)


def test_sign_refuses_frame_without_frame_id() -> None:
    frame = PeerFrame(type=PeerFrameType.PING, orch_id="orch-A1", frame_id=None)
    with pytest.raises(PeerAuthError, match="frame_id"):
        sign(frame, TOKEN)


def test_signature_payload_is_key_order_canonical() -> None:
    fixed_ts = 1_757_328_000.0
    a = _invoke_frame(timestamp=fixed_ts, body={"a": 1, "b": 2})
    b = _invoke_frame(timestamp=fixed_ts, body={"b": 2, "a": 1})
    assert signature_payload(a) == signature_payload(b)


async def test_tampered_frame_id_fails(tmp_path) -> None:
    frame = sign(_invoke_frame(), TOKEN)
    store = _store(tmp_path)
    await verify(frame, TOKEN, store)
    frame.frame_id = "frame-tampered"
    with pytest.raises(PeerAuthError, match="signature mismatch"):
        await verify(frame, TOKEN, store)


async def test_tampered_body_fails(tmp_path) -> None:
    frame = sign(_invoke_frame(), TOKEN)
    frame.body = {"content": "evil"}
    with pytest.raises(PeerAuthError, match="signature mismatch"):
        await verify(frame, TOKEN, _store(tmp_path))


async def test_tampered_signature_fails(tmp_path) -> None:
    frame = sign(_invoke_frame(), TOKEN)
    frame.signature = "0" * 64
    with pytest.raises(PeerAuthError, match="signature mismatch"):
        await verify(frame, TOKEN, _store(tmp_path))


async def test_wrong_token_fails(tmp_path) -> None:
    frame = sign(_invoke_frame(), TOKEN)
    with pytest.raises(PeerAuthError, match="signature mismatch"):
        await verify(frame, "wrong-token", _store(tmp_path))


async def test_stale_timestamp_fails(tmp_path) -> None:
    frame = sign(
        _invoke_frame(timestamp=time.time() - TIMESTAMP_WINDOW_SECONDS - 1), TOKEN
    )
    with pytest.raises(PeerAuthError, match="window"):
        await verify(frame, TOKEN, _store(tmp_path))


async def test_future_timestamp_fails(tmp_path) -> None:
    frame = sign(
        _invoke_frame(timestamp=time.time() + TIMESTAMP_WINDOW_SECONDS + 1), TOKEN
    )
    with pytest.raises(PeerAuthError, match="window"):
        await verify(frame, TOKEN, _store(tmp_path))


async def test_edge_of_window_passes(tmp_path) -> None:
    now = time.time()
    frame = sign(_invoke_frame(timestamp=now - TIMESTAMP_WINDOW_SECONDS), TOKEN)
    await verify(frame, TOKEN, _store(tmp_path), now=now)


async def test_nonce_replay_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    frame = sign(_invoke_frame(), TOKEN)
    await verify(frame, TOKEN, store)
    with pytest.raises(PeerAuthError, match="nonce replay"):
        await verify(frame, TOKEN, store)


async def test_same_nonce_on_different_frame_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    f1 = sign(_invoke_frame(nonce="same-nonce"), TOKEN)
    await verify(f1, TOKEN, store)
    f2 = sign(
        _invoke_frame(nonce="same-nonce", frame_id="frame-2", msg_id="m-2"), TOKEN
    )
    with pytest.raises(PeerAuthError, match="nonce replay"):
        await verify(f2, TOKEN, store)


async def test_missing_signature_fails(tmp_path) -> None:
    frame = _invoke_frame()
    with pytest.raises(PeerAuthError, match="missing signature"):
        await verify(frame, TOKEN, _store(tmp_path))


async def test_missing_nonce_fails(tmp_path) -> None:
    frame = _invoke_frame(nonce=None, signature="a" * 64)
    with pytest.raises(PeerAuthError, match="missing nonce"):
        await verify(frame, TOKEN, _store(tmp_path))


async def test_missing_orch_id_fails(tmp_path) -> None:
    frame = PeerFrame(
        type=PeerFrameType.PING, signature="a" * 64, nonce="nonce-1"
    )
    with pytest.raises(PeerAuthError, match="orch_id"):
        await verify(frame, TOKEN, _store(tmp_path))


async def test_verify_rejects_missing_frame_id(tmp_path) -> None:
    # Belt-and-suspenders: a directly-constructed frame with no
    # frame_id must surface as PeerAuthError, not AttributeError.
    frame = PeerFrame(
        type=PeerFrameType.PING, orch_id="orch-A1", frame_id=None,
        signature="a" * 64, nonce="nonce-1",
    )
    with pytest.raises(PeerAuthError, match="frame_id"):
        await verify(frame, TOKEN, _store(tmp_path))


async def test_empty_body_verifies_over_wire(tmp_path) -> None:
    # F1 regression: a signed empty-body frame must verify after a full
    # encode→decode round trip.
    frame = sign(_invoke_frame(body={}), TOKEN)
    wire = PeerFrame.decode(frame.encode())
    await verify(wire, TOKEN, _store(tmp_path))  # must not raise


async def test_null_frame_id_fails_cleanly_over_wire(tmp_path) -> None:
    # F2 regression: "frame_id": null on the wire decodes to a fresh id,
    # so the MAC check fails with a clean auth error — never AttributeError.
    frame = sign(_invoke_frame(), TOKEN)
    data = json.loads(frame.encode())
    data["frame_id"] = None
    wire = PeerFrame.decode(json.dumps(data))
    with pytest.raises(PeerAuthError, match="signature mismatch"):
        await verify(wire, TOKEN, _store(tmp_path))


async def test_parallel_verify_single_winner(tmp_path) -> None:
    # 8 verifications of the same frame race on one nonce: exactly one
    # passes, the rest are replays.
    store = _store(tmp_path)
    frame = sign(_invoke_frame(), TOKEN)
    results = await asyncio.gather(
        *[_verify_quietly(frame, TOKEN, store) for _ in range(8)]
    )
    assert results.count(True) == 1


async def _verify_quietly(frame: PeerFrame, token: str, store: NonceStore) -> bool:
    try:
        await verify(frame, token, store)
    except PeerAuthError:
        return False
    return True
