"""HMAC-SHA256 frame signing for ``peer/1`` (ADR-001 D3).

Every signed frame carries an HMAC over the canonical byte string
``orch_id ∥ frame_id ∥ canonical_body ∥ timestamp``, keyed by the
per-peer bearer token, plus a one-time nonce. The verifier enforces a
±60s timestamp window (DESIGN_PEER_FEDERATION.md §4.2/§4.3) and
records nonces in a :class:`~orchestratord.peer.nonce_store.NonceStore`
so a replayed frame is rejected.

Note: per D3 the signature covers (orch_id, frame_id, body, ts) only —
topic/payload ride TLS-protected but outside the MAC. The signature
exists as replay defense, not as full-frame integrity.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import TYPE_CHECKING, Any

from orchestratord.peer.nonce_store import DEFAULT_NONCE_TTL_SECONDS

if TYPE_CHECKING:
    from orchestratord.peer.nonce_store import NonceStore
    from orchestratord.peer.protocol import PeerFrame

TIMESTAMP_WINDOW_SECONDS = 60.0

_SEP = b"\x1f"


class PeerAuthError(Exception):
    """Raised when a peer frame fails signature, timestamp or nonce checks."""


def _canonical_bytes(body: dict[str, Any] | None) -> bytes:
    if body is None:
        return b""
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def signature_payload(frame: PeerFrame) -> bytes:
    """Canonical MAC input: orch_id, frame_id, body, timestamp."""
    orch_id = (frame.orch_id or "").encode("utf-8")
    ts = f"{frame.timestamp:.6f}".encode("ascii")
    return _SEP.join(
        (orch_id, frame.frame_id.encode("utf-8"), _canonical_bytes(frame.body), ts)
    )


def sign(frame: PeerFrame, token: str) -> PeerFrame:
    """Sign *frame* in place and return it.

    Generates a one-time nonce when the frame does not carry one, so
    every signed frame is replay-guarded by construction.
    """
    if not frame.orch_id:
        raise PeerAuthError("cannot sign frame without orch_id")
    if not frame.frame_id:
        raise PeerAuthError("cannot sign frame without frame_id")
    if frame.nonce is None:
        frame.nonce = secrets.token_hex(16)
    frame.signature = hmac.new(
        token.encode("utf-8"), signature_payload(frame), hashlib.sha256
    ).hexdigest()
    return frame


async def verify(
    frame: PeerFrame,
    token: str,
    nonce_store: NonceStore,
    *,
    now: float | None = None,
    window_seconds: float = TIMESTAMP_WINDOW_SECONDS,
) -> None:
    """Verify *frame* or raise :class:`PeerAuthError`.

    Checks, in order: orch_id/signature/nonce presence, timestamp
    freshness (±``window_seconds``), HMAC equality (constant-time),
    nonce first-sight. A nonce already seen in *nonce_store* means the
    frame is a replay.
    """
    now = time.time() if now is None else now
    if not frame.orch_id:
        raise PeerAuthError("frame missing orch_id")
    if not frame.frame_id:
        # A wire frame_id of null would otherwise crash the MAC
        # computation with AttributeError instead of surfacing as an
        # auth failure.
        raise PeerAuthError("frame missing frame_id")
    if frame.signature is None:
        raise PeerAuthError("frame missing signature")
    if frame.nonce is None:
        # Our sign() always sets a nonce; accepting nonce-less frames
        # would silently downgrade the replay guard, so refuse them.
        raise PeerAuthError("frame missing nonce")
    if abs(now - frame.timestamp) > window_seconds:
        raise PeerAuthError(
            f"timestamp outside ±{window_seconds:g}s window "
            f"(frame={frame.timestamp:.6f}, now={now:.6f})"
        )
    expected = hmac.new(
        token.encode("utf-8"), signature_payload(frame), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, frame.signature):
        raise PeerAuthError("signature mismatch")
    # Keep the nonce recorded at least as long as the frame could pass
    # the window check — a caller enlarging window_seconds must not
    # silently reopen the replay hole the default 2×60s TTL closes.
    ttl = max(DEFAULT_NONCE_TTL_SECONDS, 2 * window_seconds)
    if not await nonce_store.check_and_store(
        frame.orch_id, frame.nonce, ttl_seconds=ttl
    ):
        raise PeerAuthError("nonce replay detected")
