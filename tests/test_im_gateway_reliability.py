"""P4 reliability tests: audit redaction, target_offline, retention sweep.

Migrated from ClawCodex ``test_reliability_p4.py`` + ``test_retention.py``.
The daemon-loop tests (``_retention_loop``) live in the Phase 3 daemon
server module (``orchestratord.im_gateway.server``).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from orchestratord.im_gateway.audit import hash_user, redact
from orchestratord.im_gateway.binding import BindingPolicy
from orchestratord.im_gateway.config import ReliabilityConfig
from orchestratord.im_gateway.dispatcher import InboundDispatcher
from orchestratord.im_gateway.retention import run_retention_sweep
from orchestratord.im_gateway.router import SessionRouter
from orchestratord.im_gateway.store import ReliabilityStore
from orchestratord.ipc.models import (
    AckLayer,
    InboundMessage,
    OriginKey,
    SessionTarget,
)

# -- audit redaction ---------------------------------------------------


def test_redact_masks_tokens_and_hashes_users() -> None:
    out = redact(
        {
            "bot_token": "secret_tok",
            "context_token": "ctx_abc",
            "from_user_id": "user_gz",
            "webhook_url": "https://hooks.example.com/services/T/B/abcdef0123456789",
            "event_type": "send",
            "nested": {"token": "x", "ok": "keep"},
        }
    )
    assert out["bot_token"] == "***"
    assert out["context_token"] == "***"
    assert out["from_user_id"] == hash_user("user_gz")
    assert out["from_user_id"] != "user_gz"
    assert "***" in out["webhook_url"]
    assert "abcdef0123456789" not in out["webhook_url"]
    assert out["event_type"] == "send"
    assert out["nested"]["token"] == "***"
    assert out["nested"]["ok"] == "keep"


def test_store_audit_redacts_sensitive_fields(tmp_path) -> None:
    s = ReliabilityStore(tmp_path)
    s.audit(
        "wechat_send",
        channel="wechat-main",
        bot_token="supersecret",
        from_user_id="user_gz",
        context_token="ctx_xyz",
    )
    raw = (tmp_path / "audit.ndjson").read_text(encoding="utf-8")
    assert "supersecret" not in raw
    assert "ctx_xyz" not in raw
    assert "user_gz" not in raw
    assert "***" in raw


# -- target_offline ----------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_rejects_when_target_offline(tmp_path) -> None:
    store = ReliabilityStore(tmp_path)
    bp = BindingPolicy()
    o = OriginKey.wechat("default", "user_gz")
    bp.bind(o, SessionTarget("repl_main", "repl"))
    bp.mark_offline(o)
    router = SessionRouter(bp, store)
    disp = InboundDispatcher(store, router)
    msg = InboundMessage(origin=str(o), text="hi", message_id="m1", channel="wechat-main")
    ack = await disp.process(msg)
    assert ack.layer is AckLayer.ACCEPTED
    assert "target_offline" in ack.message
    # audit recorded (redacted origin is the raw origin string here)
    assert any(e["event_type"] == "target_offline" for e in store.audit_entries())


def test_router_is_offline_flag(tmp_path) -> None:
    store = ReliabilityStore(tmp_path)
    bp = BindingPolicy()
    o = OriginKey.wechat("default", "u1")
    bp.bind(o, SessionTarget("repl_main", "repl"))
    router = SessionRouter(bp, store)
    assert not router.is_offline(o)
    bp.mark_offline(o)
    assert router.is_offline(o)
    bp.terminate(o)
    assert not router.is_offline(o)  # terminated → no binding → not offline (default route)


# -- restart reload hook ----------------------------------------------
# NOTE: the ClawCodex ``test_message_gateway_reload_channel_rebuilds`` variant
# exercised the real WeChat iLink adapter; the equivalent fake-factory path is
# covered in ``tests/test_im_gateway_core.py::test_gateway_reload_channel_rebuilds``
# (the real WeChat adapter lands in Phase 2).


# -- retention sweep -----------------------------------------------------


def _write_ndjson(path, entries) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_ndjson(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read_json(path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def test_dead_letter_rotates_on_max_bytes(tmp_path) -> None:
    cfg = ReliabilityConfig(dead_letter_max_bytes=100, dead_letter_backup_count=3)
    store = ReliabilityStore(tmp_path, reliability=cfg)
    path = tmp_path / "dead_letter.ndjson"
    _write_ndjson(path, [{"k": "x" * 200}])

    store.append_dead_letter({"k": "new"})

    assert path.exists()
    assert (tmp_path / "dead_letter.ndjson.1").exists()
    assert _read_ndjson(path) == [{"k": "new"}]


def test_audit_rotates_on_max_bytes(tmp_path) -> None:
    cfg = ReliabilityConfig(audit_max_bytes=100, audit_backup_count=3)
    store = ReliabilityStore(tmp_path, reliability=cfg)
    path = tmp_path / "audit.ndjson"
    _write_ndjson(path, [{"event_type": "old", "payload": "x" * 200}])

    store.audit("test_event", payload="new")

    assert path.exists()
    assert (tmp_path / "audit.ndjson.1").exists()


def test_purge_processed_inbound_uses_seen_at(tmp_path) -> None:
    store = ReliabilityStore(tmp_path)
    now = time.time()
    _write_ndjson(
        tmp_path / "processed_inbound.ndjson",
        [
            {"key": "old", "seen_at": now - 8 * 86400},
            {"key": "new", "seen_at": now - 100},
        ],
    )

    removed = store.purge_processed_inbound(ttl_seconds=7 * 86400, max_entries=10000)

    assert removed == 1
    assert _read_ndjson(tmp_path / "processed_inbound.ndjson") == [
        {"key": "new", "seen_at": pytest.approx(now - 100)}
    ]


def test_purge_outbox_uses_at_with_timestamp_fallback(tmp_path) -> None:
    store = ReliabilityStore(tmp_path)
    now = time.time()
    _write_ndjson(
        tmp_path / "outbox.ndjson",
        [
            {"id": "old-at", "at": now - 31 * 86400},
            {"id": "new-at", "at": now - 100},
            {"id": "old-timestamp", "timestamp": now - 31 * 86400},
            {"id": "new-timestamp", "timestamp": now - 100},
            {"id": "legacy-no-time"},
        ],
    )

    removed = store.purge_outbox(ttl_seconds=30 * 86400, max_entries=50000)

    assert removed == 2
    assert {entry["id"] for entry in _read_ndjson(tmp_path / "outbox.ndjson")} == {
        "new-at",
        "new-timestamp",
        "legacy-no-time",
    }


def test_purge_unsupported_inbound_uses_received_at(tmp_path) -> None:
    store = ReliabilityStore(tmp_path)
    now = time.time()
    _write_ndjson(
        tmp_path / "unsupported_inbound.ndjson",
        [
            {"id": "old", "received_at": now - 8 * 86400},
            {"id": "new", "received_at": now - 100},
            {"id": "legacy-no-time"},
        ],
    )

    removed = store.purge_unsupported_inbound(ttl_seconds=7 * 86400, max_entries=10000)

    assert removed == 1
    assert {entry["id"] for entry in _read_ndjson(tmp_path / "unsupported_inbound.ndjson")} == {
        "new",
        "legacy-no-time",
    }


def test_purge_unsupported_inbound_caps_legacy_without_timestamps(tmp_path) -> None:
    store = ReliabilityStore(tmp_path)
    _write_ndjson(
        tmp_path / "unsupported_inbound.ndjson",
        [{"id": f"legacy-{idx}"} for idx in range(5)],
    )

    removed = store.purge_unsupported_inbound(ttl_seconds=7 * 86400, max_entries=2)

    assert removed == 3
    assert [entry["id"] for entry in _read_ndjson(tmp_path / "unsupported_inbound.ndjson")] == [
        "legacy-3",
        "legacy-4",
    ]


def test_purge_all_only_manages_bounded_append_files(tmp_path) -> None:
    cfg = ReliabilityConfig()
    store = ReliabilityStore(tmp_path, reliability=cfg)
    now = time.time()
    _write_ndjson(
        tmp_path / "processed_inbound.ndjson",
        [{"key": "old", "seen_at": now - 8 * 86400}],
    )
    _write_ndjson(tmp_path / "outbox.ndjson", [{"id": "old", "at": now - 31 * 86400}])
    _write_ndjson(
        tmp_path / "unsupported_inbound.ndjson",
        [{"id": "old", "received_at": now - 8 * 86400}],
    )
    _write_json(tmp_path / "im_session_map.json", {"legacy": {"session_id": "old"}})
    _write_json(tmp_path / "wechat_context_tokens.json", {"acct:user": "ctx"})
    _write_json(tmp_path / "feishu_last_senders.json", {"feishu": "ou_user"})
    _write_json(tmp_path / "wechat_accounts.json", {"default": {"get_updates_buf": "cursor"}})
    _write_json(tmp_path / "wechat" / "wechat_pairing.json", {"codes": [{"code": "abc"}]})

    result = store.purge_all(cfg)

    assert result == {
        "processed_inbound.ndjson": 1,
        "outbox.ndjson": 1,
        "unsupported_inbound.ndjson": 1,
    }
    assert _read_json(tmp_path / "im_session_map.json") == {"legacy": {"session_id": "old"}}
    assert _read_json(tmp_path / "wechat_context_tokens.json") == {"acct:user": "ctx"}
    assert _read_json(tmp_path / "feishu_last_senders.json") == {"feishu": "ou_user"}
    assert _read_json(tmp_path / "wechat_accounts.json") == {
        "default": {"get_updates_buf": "cursor"}
    }
    assert _read_json(tmp_path / "wechat" / "wechat_pairing.json") == {"codes": [{"code": "abc"}]}


def test_disabled_returns_empty_and_does_not_modify(tmp_path) -> None:
    cfg = ReliabilityConfig(retention_enabled=False)
    _write_ndjson(tmp_path / "processed_inbound.ndjson", [{"key": "k", "seen_at": 0}])

    result = run_retention_sweep(str(tmp_path), cfg)

    assert result == {}
    assert _read_ndjson(tmp_path / "processed_inbound.ndjson") == [{"key": "k", "seen_at": 0}]


def test_run_retention_sweep_enabled(tmp_path) -> None:
    cfg = ReliabilityConfig()
    now = time.time()
    _write_ndjson(
        tmp_path / "processed_inbound.ndjson",
        [{"key": "old", "seen_at": now - 8 * 86400}],
    )

    result = run_retention_sweep(str(tmp_path), cfg)

    assert result.get("processed_inbound.ndjson") == 1


def test_run_retention_sweep_exception_returns_empty(tmp_path, monkeypatch) -> None:
    import orchestratord.im_gateway.retention as retention_mod

    def boom(self, *args, **kwargs) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(retention_mod.ReliabilityStore, "__init__", boom)

    assert run_retention_sweep(str(tmp_path), ReliabilityConfig()) == {}


def test_corrupted_ndjson_file_does_not_crash_purge(tmp_path) -> None:
    store = ReliabilityStore(tmp_path)
    path = tmp_path / "processed_inbound.ndjson"
    path.write_text(
        '{"key": "ok", "seen_at": ' + str(time.time()) + "}\n"
        "not json\n"
        '{"key": "ok2", "seen_at": ' + str(time.time()) + "}\n",
        encoding="utf-8",
    )

    assert store.purge_processed_inbound(ttl_seconds=7 * 86400, max_entries=100) == 0


def test_retention_loop_executes_and_cancels(tmp_path, monkeypatch) -> None:
    import orchestratord.im_gateway.server as server_mod

    call_count = 0

    def fake_sweep(state_dir, reliability) -> None:
        nonlocal call_count
        call_count += 1

    monkeypatch.setattr(server_mod, "run_retention_sweep", fake_sweep)

    async def run() -> None:
        task = asyncio.create_task(
            server_mod._retention_loop(str(tmp_path), 0.01, ReliabilityConfig())
        )
        await asyncio.sleep(0.05)
        assert call_count >= 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_retention_loop_swallows_exception(tmp_path, monkeypatch) -> None:
    import orchestratord.im_gateway.server as server_mod

    call_count = 0

    def flaky_sweep(state_dir, reliability) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("flaky")

    monkeypatch.setattr(server_mod, "run_retention_sweep", flaky_sweep)

    async def run() -> None:
        task = asyncio.create_task(
            server_mod._retention_loop(str(tmp_path), 0.01, ReliabilityConfig())
        )
        await asyncio.sleep(0.05)
        assert call_count >= 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
