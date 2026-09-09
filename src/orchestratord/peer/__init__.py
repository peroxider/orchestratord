"""Peer federation layer (``peer/1``) — cross-daemon interconnect.

Phase 1 lands incrementally per DESIGN_PEER_FEDERATION.md §10
(PR1 protocol/hmac → PR2 card/registry → PR3 realtime relay →
PR4 client/handshake/group → PR5 router/dispatcher/CLI → PR6 e2e).
"""

from orchestratord.peer.hmac_sig import (
    TIMESTAMP_WINDOW_SECONDS,
    PeerAuthError,
    sign,
    verify,
)
from orchestratord.peer.nonce_store import NonceStore
from orchestratord.peer.protocol import (
    PROTOCOL_VERSION,
    PeerFrame,
    PeerFrameType,
)

__all__ = [
    "PROTOCOL_VERSION",
    "TIMESTAMP_WINDOW_SECONDS",
    "NonceStore",
    "PeerAuthError",
    "PeerFrame",
    "PeerFrameType",
    "sign",
    "verify",
]
