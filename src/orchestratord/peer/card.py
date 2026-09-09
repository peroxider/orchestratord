"""Agent Card construction + persistent orch_id (DESIGN §4.3/§14.2, AC4).

Card field names borrow the A2A spec subset (D20) but
``protocol_version`` is ``peer/1`` — A2A compatibility is never
advertised (ADR-001 D1). The orch_id is generated once and persisted at
``~/.orchestratord/data/orch_id`` so it survives daemon restarts (AC4).
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestratord._version import __version__
from orchestratord.workspace_locator import ORCHESTRATORD_BASE

ORCH_ID_PATH = ORCHESTRATORD_BASE / "data" / "orch_id"

DEFAULT_CAPABILITIES = [
    "peer.invoke",
    "peer.subscribe",
    "sessions.read",
    "sessions.message.post",
    "sessions.approve",
    "agents.list",
    "agents.message",
    "inbox.read",
    "realtime.subscribe",
]

DEFAULT_SKILLS = [
    {
        "id": "sessions.message.post",
        "description": "Append a message to a session timeline (DB-backed)",
        "input_schema": {
            "session_id": "uuid",
            "role": "user|assistant",
            "content": "string",
        },
    },
    {
        "id": "realtime.subscribe",
        "description": "SSE stream of broker topic frames",
        "input_schema": {"topics": ["string"]},
    },
]


def load_orch_id(path: Path | None = None) -> str | None:
    p = path or ORCH_ID_PATH
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def ensure_orch_id(instance_name: str | None = None, path: Path | None = None) -> str:
    """Return the persistent orchestrator ID, creating it on first use.

    Format: ``orch-{instance-slug}-{YYYY-MM-DD}-{6 hex}`` (§4.3 example:
    ``orch-A1-2026-09-08-9f3c1a``). An existing ID is returned unchanged;
    only a missing/empty file triggers a write.
    """
    p = path or ORCH_ID_PATH
    existing = load_orch_id(p)
    if existing:
        return existing
    name = _slug(
        instance_name
        if instance_name is not None
        else os.environ.get("ORCHESTRATORD_INSTANCE_NAME", "orchestratord")
    )
    orch_id = (
        f"orch-{name}-{datetime.now(tz=UTC).date().isoformat()}"
        f"-{uuid.uuid4().hex[:6]}"
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(orch_id + "\n", encoding="utf-8")
    return orch_id


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return slug or "orchestratord"


# PR-B1: a sentinel fallback used when neither ``ORCHESTRATORD_PEER_PUBLIC_URL``
# nor ``ORCHESTRATORD_PEER_LISTEN`` is set. The router (``routers/peer.py``)
# also defaults to this port so the card URL never disagrees with the listener.
_DEFAULT_PEER_PORT = "9001"


def _resolve_card_url() -> str:
    """Return the publicly-reachable peer base URL for the Agent Card.

    ``ORCHESTRATORD_PEER_PUBLIC_URL`` wins (operator-set override for
    reverse-proxy deployments). Otherwise fall back to ``http://`` +
    ``ORCHESTRATORD_PEER_LISTEN`` (``HOST:PORT`` form), or the local
    sentinel default. Keeps the ``url`` field and ``transports[rest].url``
    field on a single source of truth so they cannot diverge.
    """
    public = os.environ.get("ORCHESTRATORD_PEER_PUBLIC_URL", "").strip()
    if public:
        return public.rstrip("/")
    listen = os.environ.get("ORCHESTRATORD_PEER_LISTEN", "").strip()
    if listen:
        # ORCHESTRATORD_PEER_LISTEN is HOST:PORT; assume plain HTTP for the
        # loopback default — operators fronting with TLS set PUBLIC_URL.
        host, _, port = listen.partition(":")
        if host and port:
            return f"http://{host}:{port}"
    return f"http://127.0.0.1:{_DEFAULT_PEER_PORT}"


# PR-B2: the frame transport URL. The default single-port design
# (plan §A) appends this path to the REST base URL; PR-B2.1 splits
# the frame listener onto its own socket when the operator sets
# ``ORCHESTRATORD_PEER_FRAME_LISTEN`` — the helper shape stays
# stable across both deployment modes.
_FRAME_STREAM_PATH = "/peer/v1/stream"


def _resolve_frame_url() -> str:
    """Return the publicly-reachable peer/1 frame transport URL.

    Resolution order:

    1. ``ORCHESTRATORD_PEER_FRAME_LISTEN`` (HOST:PORT) — operator
       override for the PR-B2.1 split-port deployment where the frame
       listener runs on its own socket.
    2. ``ORCHESTRATORD_PEER_PUBLIC_URL`` (and the rest of
       :func:`_resolve_card_url`'s chain) + the
       :data:`_FRAME_STREAM_PATH` suffix — single-port design where
       the frame endpoint shares the REST listener.

    Symmetric with :func:`_resolve_card_url` so an operator that flips
    ``ORCHESTRATORD_PEER_PUBLIC_URL` between deployments gets matching
    URLs in both ``transports[rest]`` and ``transports[frame]`` without
    double-bookkeeping.
    """
    frame_listen = os.environ.get("ORCHESTRATORD_PEER_FRAME_LISTEN", "").strip()
    if frame_listen:
        host, _, port = frame_listen.partition(":")
        if host and port:
            # Loopback default assumes plain HTTP; TLS-fronted operators
            # keep using ORCHESTRATORD_PEER_PUBLIC_URL instead.
            return f"http://{host}:{port}{_FRAME_STREAM_PATH}"
    return f"{_resolve_card_url()}{_FRAME_STREAM_PATH}"


def build_agent_card(
    *,
    orch_id: str,
    url: str,
    name: str | None = None,
    description: str | None = None,
    capabilities: list[str] | None = None,
    skills: list[dict[str, Any]] | None = None,
    provider: dict[str, str] | None = None,
    transports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the ``/.well-known/agent.json`` payload (§4.3, PR-B1/PR-B2).

    Pure function — discovery contains no workspace data, so it is safe
    to serve unauthenticated.

    PR-B1: ``transports`` is a list of
    ``{"protocol": "rest"|"frame", "url": "...", "version": "1"}``
    entries advertising the inbound surfaces this daemon accepts.
    PR-B2: the default advertises **both** rest and frame (frame
    preferred) — a v2 client reading ``transports[]`` will pick frame
    automatically, while a v1 client reading ``preferred_transport``
    still gets a single hint it can act on.
    """
    instance = (
        name
        if name is not None
        else os.environ.get("ORCHESTRATORD_INSTANCE_NAME", "orchestratord")
    )
    if transports is None:
        # PR-B2 default: advertise frame first (preferred), then rest as
        # fallback. The frame URL is the rest URL + the
        # ``/peer/v1/stream`` path (single-port design, plan §A) — the
        # caller passes a fully-qualified ``url`` so the two entries
        # cannot diverge even when env vars are unset (test paths).
        frame_url = f"{url.rstrip('/')}{_FRAME_STREAM_PATH}"
        actual_transports = [
            {"protocol": "frame", "url": frame_url, "version": "1"},
            {"protocol": "rest", "url": url, "version": "1"},
        ]
    else:
        actual_transports = list(transports)
    preferred = actual_transports[0]["protocol"] if actual_transports else "rest"
    return {
        "name": instance,
        "description": description
        or f"orchestratord daemon instance ({instance})",
        "version": __version__,
        "url": url,
        "orch_id": orch_id,
        "protocol_version": "peer/1",
        # §14.2: borrowed field naming is disclosed without claiming
        # A2A compatibility (D1/D20).
        "agent_card_version": "draft-2026-04-a2a-style",
        "inspired_by": ["a2a-protocol-v1.0", "mcp-2026-07"],
        "preferred_transport": preferred,
        # PR-B1: ordered list (frame first when PR-B2 lands, rest as
        # fallback). v1 clients reading only preferred_transport still
        # see a single hint; v2 clients iterate this list to negotiate.
        "transports": actual_transports,
        "capabilities": list(
            capabilities if capabilities is not None else DEFAULT_CAPABILITIES
        ),
        "defaultInputModes": ["text/plain", "application/json"],
        "security_schemes": {
            "bearer": {
                "type": "http",
                "scheme": "bearer",
                "description": (
                    "Per-peer token; SHA-256 hash stored in "
                    "auth_tokens.scopes=['peer.*']"
                ),
            },
            "hmac": {
                # The design doc's sample carried type "mutual-tls" next
                # to an HMAC description; mTLS is a rejected alternative
                # (ADR-001 D3), so the type names the actual scheme.
                "type": "hmac-sha256",
                "description": (
                    "HMAC-SHA256 over (orch_id, frame_id, body, ts); "
                    "±60s window; nonce replay guard"
                ),
            },
        },
        "skills": list(skills if skills is not None else DEFAULT_SKILLS),
        "provider": provider
        or {"organization": "self-hosted", "contact": "operator@example.com"},
    }
