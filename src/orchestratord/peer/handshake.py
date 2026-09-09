"""HELLO/WELCOME/SUBSCRIBE handshake state machine (DESIGN §5, D23).

The handshake is synchronous but trust is asynchronous: A1 sends a
signed HELLO, B1's operator (or the ``ORCHESTRATORD_PEER_TRUST``
whitelist) accepts, and only then does the WELCOME come back. Any
timeout aborts the attempt and closes the transport — "超时即拒绝，
不留 half-state" (D23): connect 10s + HELLO response 30s, with up to
3 retries under exponential backoff driven by
:class:`~orchestratord.peer.client.PeerClient`.

Frames travel over an injected :class:`FrameTransport` so the state
machine is testable without a socket; the HTTPS binding arrives with
the PR5 server endpoints (DESIGN §10).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from orchestratord.peer.hmac_sig import PeerAuthError, sign, verify
from orchestratord.peer.nonce_store import NonceStore
from orchestratord.peer.protocol import PeerFrame, PeerFrameType

CONNECT_TIMEOUT_SECONDS = 10.0
HELLO_TIMEOUT_SECONDS = 30.0
MAX_HANDSHAKE_RETRIES = 3


class HandshakeError(Exception):
    """HELLO/WELCOME exchange failed (reject, protocol error, timeout)."""


class FrameTransport(Protocol):
    """Minimal async frame pipe the handshake and client drive."""

    async def send_frame(self, frame: PeerFrame) -> None: ...

    async def receive_frame(self) -> PeerFrame: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class HandshakeResult:
    """Outcome of a successful HELLO→WELCOME exchange."""

    welcome: PeerFrame
    remote_orch_id: str | None
    remote_capabilities: list[str]


async def perform_handshake(
    transport: FrameTransport,
    *,
    orch_id: str,
    token: str,
    nonce_store: NonceStore,
    capabilities: list[str] | None = None,
    hello_timeout: float = HELLO_TIMEOUT_SECONDS,
) -> HandshakeResult:
    """Drive one HELLO→WELCOME exchange on an already-open transport.

    Raises :class:`HandshakeError` on non-WELCOME replies, failed
    signature verification, or the D23 HELLO timeout; every failure
    path closes the transport before returning control. The per-peer
    token is symmetric (D15), so the remote's WELCOME is verified with
    the same token and our nonce store guards the reply.
    """
    hello = PeerFrame.hello(orch_id=orch_id, capabilities=capabilities)
    sign(hello, token)
    try:
        await transport.send_frame(hello)
        welcome = await asyncio.wait_for(
            transport.receive_frame(), timeout=hello_timeout
        )
    except TimeoutError as exc:
        # D23: a timeout rejects the whole attempt — no half-state.
        await transport.close()
        raise HandshakeError(
            f"HELLO response timed out after {hello_timeout:g}s"
        ) from exc
    except Exception as exc:
        # A dead socket mid-HELLO (not a timeout) must also close the
        # transport — every failure path leaves no half-state (D23).
        await transport.close()
        raise HandshakeError(f"HELLO exchange failed: {exc}") from exc
    if welcome.type is not PeerFrameType.WELCOME:
        await transport.close()
        raise HandshakeError(f"expected WELCOME, got {welcome.type.value!r}")
    try:
        await verify(welcome, token, nonce_store)
    except PeerAuthError as exc:
        await transport.close()
        raise HandshakeError(f"WELCOME failed verification: {exc}") from exc
    return HandshakeResult(
        welcome=welcome,
        remote_orch_id=welcome.orch_id,
        remote_capabilities=list(welcome.capabilities),
    )


async def send_subscriptions(
    transport: FrameTransport,
    topics: list[str] | set[str],
) -> None:
    """Emit one SUBSCRIBE frame per topic (§5 subscribe step)."""
    for topic in sorted(topics):
        frame = PeerFrame(type=PeerFrameType.SUBSCRIBE, topic=topic)
        await transport.send_frame(frame)
