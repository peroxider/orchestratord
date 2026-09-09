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


def build_agent_card(
    *,
    orch_id: str,
    url: str,
    name: str | None = None,
    description: str | None = None,
    capabilities: list[str] | None = None,
    skills: list[dict[str, Any]] | None = None,
    provider: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the ``/.well-known/agent.json`` payload (§4.3).

    Pure function — discovery contains no workspace data, so it is safe
    to serve unauthenticated.
    """
    instance = (
        name
        if name is not None
        else os.environ.get("ORCHESTRATORD_INSTANCE_NAME", "orchestratord")
    )
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
        "preferred_transport": "https+sse",
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
