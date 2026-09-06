"""Runtime entity + token contract (§6.3).

A Runtime represents a host machine that has run
``orchestratord daemon start --workspace-token ...`` and is connected to
the server over WebSocket. Security invariants:

* Token storage is one-way hashed; plaintext lives only in the
  originating shell environment, never in the DB or logs.
* Tokens are single-use for the issuance round-trip; rotation
  invalidates prior tokens.
* Heartbeats drive ``last_seen_at``; staleness is enforced server-side.
* Revocation flips ``status`` to ``disabled`` and closes the WS.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §6.3, §5.7.4.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4


def _runtime(**overrides):
    from orchestratord.domain.runtime import (  # type: ignore
        Runtime,
        RuntimeStatus,
    )

    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "hostname": "test-host",
        "os": "linux",
        "token_hash": hashlib.sha256(b"raw-token-never-stored").hexdigest(),
        "status": RuntimeStatus.ONLINE,
        "last_seen_at": datetime.now(UTC),
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Runtime(**defaults)


class TestTokenStorage:
    """Plaintext tokens must NEVER appear in the model or be loggable."""

    def test_token_field_is_hash_only(self) -> None:
        rt = _runtime()
        assert not hasattr(rt, "token"), (
            "Runtime exposes a plaintext token attribute — security bug"
        )
        assert len(rt.token_hash) == 64
        int(rt.token_hash, 16)

    def test_token_generation_is_one_way(self) -> None:
        from orchestratord.domain.runtime import (  # type: ignore
            hash_runtime_token,
            issue_runtime_token,
        )

        plaintext, hashed = issue_runtime_token()
        assert plaintext != hashed
        assert hash_runtime_token(plaintext) == hashed


class TestHeartbeatLifecycle:
    """Heartbeat refreshes ``last_seen_at``; staleness marks offline."""

    def test_recent_heartbeat_keeps_online(self) -> None:
        rt = _runtime(last_seen_at=datetime.now(UTC))
        assert rt.is_online(threshold=timedelta(seconds=30)) is True

    def test_stale_heartbeat_marks_offline(self) -> None:
        old = datetime.now(UTC) - timedelta(minutes=5)
        rt = _runtime(last_seen_at=old)
        assert rt.is_online(threshold=timedelta(seconds=30)) is False

    def test_disabled_runtime_is_not_online(self) -> None:
        from orchestratord.domain.runtime import RuntimeStatus  # type: ignore

        rt = _runtime(status=RuntimeStatus.DISABLED)
        assert rt.is_online(threshold=timedelta(seconds=30)) is False


class TestRevocation:
    """Revocation flips status; subsequent token use is rejected."""

    def test_revoke_marks_disabled(self) -> None:
        from orchestratord.domain.runtime import RuntimeStatus  # type: ignore

        rt = _runtime()
        assert rt.status == RuntimeStatus.ONLINE
        rt.revoke()
        assert rt.status == RuntimeStatus.DISABLED

    def test_revoked_token_rejected_by_lookup(self) -> None:
        from orchestratord.domain.runtime import (  # type: ignore
            RuntimeStatus,
            issue_runtime_token,
            verify_runtime_token,
        )

        plaintext, _ = issue_runtime_token()
        rt = _runtime()
        rt.status = RuntimeStatus.DISABLED
        assert verify_runtime_token(rt, plaintext) is False


class TestBackendDiscovery:
    """The runtime reports CLI probes to ``runtime_backends``."""

    def test_runtime_records_probed_cli_list(self) -> None:
        rt = _runtime()
        rt.record_probed_backends([
            {"cli": "claude", "version": "1.0.0"},
            {"cli": "codex", "version": "0.39.0"},
        ])
        assert {b["cli"] for b in rt.probed_backends} == {"claude", "codex"}
