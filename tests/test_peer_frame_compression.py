"""PR-B8 frame compression tests.

The wire contract: ``PeerFrame.encode(min_bytes=N)`` ships a
``body``/``payload`` dict that serializes to >= N bytes as a
base64(gzip(JSON)) string and stamps ``comp: "gzip+base64"``;
:func:`PeerFrame.decode` transparently restores the plain dict, so the
HMAC input (the *plain* body, :mod:`orchestratord.peer.hmac_sig`) is
compression-invisible and old/new peers interoperate frame-by-frame.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json

import pytest

from orchestratord.peer.hmac_sig import sign, verify
from orchestratord.peer.nonce_store import NonceStore
from orchestratord.peer.protocol import (
    COMPRESS_GZIP_BASE64,
    DEFAULT_COMPRESS_MIN_BYTES,
    PeerFrame,
    frame_compress_min_bytes,
)

_BIG = {"blob": "x" * 8000}
_SMALL = {"a": 1}


def test_encode_below_threshold_leaves_wire_untouched() -> None:
    frame = PeerFrame.invoke(orch_id="a", request_id="r", method="m", body=_SMALL)
    wire = json.loads(frame.encode(min_bytes=DEFAULT_COMPRESS_MIN_BYTES))
    assert "comp" not in wire
    assert wire["body"] == _SMALL
    # Decoding an uncompressed frame keeps working (Phase 1 parity).
    assert PeerFrame.decode(frame.encode()).body == _SMALL


def test_encode_compresses_large_body_and_roundtrips() -> None:
    frame = PeerFrame.invoke(orch_id="a", request_id="r", method="m", body=_BIG)
    raw = frame.encode(min_bytes=1024)
    wire = json.loads(raw)
    assert wire["comp"] == COMPRESS_GZIP_BASE64
    assert isinstance(wire["body"], str)
    # The wire value really is gzip+base64 of the JSON body.
    restored = json.loads(gzip.decompress(base64.b64decode(wire["body"])))
    assert restored == _BIG
    # ...and decode() restores the plain dict transparently.
    assert PeerFrame.decode(raw).body == _BIG


def test_encode_compresses_event_payload_too() -> None:
    frame = PeerFrame.event(orch_id="a", topic="t", payload=_BIG)
    wire = json.loads(frame.encode(min_bytes=1024))
    assert wire["comp"] == COMPRESS_GZIP_BASE64
    assert PeerFrame.decode(frame.encode(min_bytes=1024)).payload == _BIG


def test_explicit_zero_disables_compression() -> None:
    frame = PeerFrame.invoke(orch_id="a", request_id="r", method="m", body=_BIG)
    wire = json.loads(frame.encode(min_bytes=0))
    assert "comp" not in wire
    assert wire["body"] == _BIG


def test_env_threshold_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATORD_PEER_FRAME_COMPRESS_BYTES", "0")
    assert frame_compress_min_bytes() == 0
    monkeypatch.setenv("ORCHESTRATORD_PEER_FRAME_COMPRESS_BYTES", "bogus")
    assert frame_compress_min_bytes() == DEFAULT_COMPRESS_MIN_BYTES
    monkeypatch.delenv("ORCHESTRATORD_PEER_FRAME_COMPRESS_BYTES")
    assert frame_compress_min_bytes() == DEFAULT_COMPRESS_MIN_BYTES


def test_unknown_comp_marker_is_a_protocol_error() -> None:
    raw = json.dumps(
        {"type": "invoke", "body": _SMALL, "comp": "zstd", "frame_id": "f"}
    )
    with pytest.raises(ValueError, match="unsupported frame comp"):
        PeerFrame.decode(raw)


def test_corrupt_compressed_body_raises_valueerror() -> None:
    raw = json.dumps(
        {"type": "invoke", "body": "not-base64-gzip!!", "comp": "gzip+base64"}
    )
    with pytest.raises(ValueError, match="failed decompression"):
        PeerFrame.decode(raw)


def test_compression_is_signature_invisible() -> None:
    """sign() the plain body → compress on the wire → verify passes.

    This is the invariant that lets compressed and uncompressed peers
    interoperate without a MAC change (D3 covers the plain body).
    """
    frame = PeerFrame.invoke(orch_id="orch-A", request_id="r", method="m", body=_BIG)
    sign(frame, "tok")
    assert frame.signature is not None
    decoded = PeerFrame.decode(frame.encode(min_bytes=1024))
    asyncio.run(
        verify(decoded, "tok", NonceStore("/tmp/prb8-mac-nonces.db"))
    )
