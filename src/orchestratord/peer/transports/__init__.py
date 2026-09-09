"""Frame-transport factory selector (DESIGN §6.1, PR-B2).

The :class:`~orchestratord.peer.handshake.FrameTransport` Protocol is
wire-agnostic; the actual binding is selected from the remote Agent
Card's ``transports[]`` list. This module owns the rule:

* a remote advertising ``{"protocol": "frame", ...}`` switches the
  client to :class:`HttpsFrameTransport` (this PR's actual binding);
* otherwise the injected fallback (the Phase-1 in-memory or SSE path)
  is used unchanged.

The selector returns a factory, not a transport, because each handshake
attempt gets a fresh transport instance — see D23 / no-half-state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from orchestratord.peer.handshake import FrameTransport
from orchestratord.peer.transports.https_frame import HttpsFrameTransport

TransportFactory = Callable[[], Awaitable[FrameTransport]]


def select_transport_factory(
    card: dict | None,
    *,
    frame_url: str | None,
    orch_id: str,
    token: str,
    fallback_factory: TransportFactory,
) -> TransportFactory:
    """Choose the wire binding based on the remote Agent Card.

    Decision rule (PR-B2 §F):

    * remote advertises ``transports[].protocol == "frame"`` AND a
      ``frame_url`` is known → return a factory that opens
      :class:`HttpsFrameTransport` against that URL.
    * otherwise → return *fallback_factory* unchanged so Phase-1 paths
      keep working without configuration.

    Both choices preserve PR-B1's invariants: a v1 client (no
    ``transports[]``) falls through to ``fallback_factory`` and the
    ``v1_sunset`` classification; a v2 client (transports[] present)
    uses frame when advertised.
    """
    if card and isinstance(card, dict):
        for entry in card.get("transports") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("protocol") == "frame" and frame_url:
                async def factory(
                    _url: str = frame_url,
                    _orch: str = orch_id,
                    _tok: str = token,
                ) -> FrameTransport:
                    return await HttpsFrameTransport.connect(
                        url=_url, orch_id=_orch, token=_tok
                    )
                return factory
    return fallback_factory


__all__ = [
    "HttpsFrameTransport",
    "TransportFactory",
    "select_transport_factory",
]