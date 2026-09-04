"""Tests for gateway core components: store, binding, router, gate, MessageGateway.

Migrated from ClawCodex ``test_core.py`` + ``test_gateway.py`` +
``test_connection_notify.py`` + ``test_stub_agent.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from orchestratord.channels.capabilities import (
    CapabilityDescriptor,
    CapabilityNotDeclaredError,
    ChannelAdapter,
    ChannelCapability,
    ChannelCapabilitySet,
)
from orchestratord.channels.models import ChannelConfig, ChannelMessage, ChannelType
from orchestratord.channels.registry import ChannelAdapterRegistry
from orchestratord.channels.results import (
    ChannelHealth,
    ChannelSendResult,
    ErrorCategory,
    ValidationResult,
)
from orchestratord.im_gateway.binding import BindingPolicy
from orchestratord.im_gateway.capability_gate import CapabilityGate
from orchestratord.im_gateway.config import (
    CommandAllowlistConfig,
    GatewayConfig,
    ReliabilityConfig,
    save_config,
)
from orchestratord.im_gateway.gateway import MessageGateway
from orchestratord.im_gateway.outbound import OutboundDispatcher
from orchestratord.im_gateway.router import SessionRouter
from orchestratord.im_gateway.store import ReliabilityStore
from orchestratord.im_gateway.stub_agent import make_stub_handler
from orchestratord.ipc.models import (
    IM_DIRECT_ALL_ORIGIN,
    AckLayer,
    AckReceipt,
    InboundMessage,
    MessageSemantics,
    OriginKey,
    OutboundMessage,
    SessionTarget,
)

# -- fake adapter --------------------------------------------------------


class _FakeAdapter(ChannelAdapter):
    def __init__(
        self,
        name: str = "fake",
        caps: ChannelCapabilitySet | None = None,
        *,
        send_result: ChannelSendResult | None = None,
        supports_markdown: bool = False,
    ) -> None:
        self._name = name
        self._caps = caps or ChannelCapabilitySet.of(
            ChannelCapability.OUTBOUND_TEXT,
            descriptors={
                ChannelCapability.OUTBOUND_TEXT: CapabilityDescriptor(
                    ChannelCapability.OUTBOUND_TEXT,
                    supports_markdown=supports_markdown,
                )
            },
        )
        self._send_result = send_result
        self.sends: list[ChannelMessage] = []
        self.send_calls: list[dict] = []

    @property
    def channel_id(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ChannelCapabilitySet:
        return self._caps

    def validate_config(self) -> ValidationResult:
        return ValidationResult.ok_result()

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(healthy=True, channel_id=self._name)

    async def send(self, message, *, target=None, context_token=None) -> ChannelSendResult:
        self.sends.append(message)
        self.send_calls.append(
            {"message": message, "target": target, "context_token": context_token}
        )
        if self._send_result is not None:
            return self._send_result
        return ChannelSendResult.success(self._name, provider_receipt=f"mid_{len(self.sends)}")


def _registry_with(*adapters: _FakeAdapter) -> ChannelAdapterRegistry:
    reg = ChannelAdapterRegistry()
    for a in adapters:
        reg.register(a)
    return reg


# -- store ---------------------------------------------------------------


def test_store_dedupe(tmp_path) -> None:
    s = ReliabilityStore(tmp_path, ReliabilityConfig(inbound_dedupe_ttl_seconds=600))
    assert s.check_and_record("k1", message_id="m1") is True
    assert s.check_and_record("k1", message_id="m1") is False
    assert s.is_duplicate("k1") is True


def test_store_check_and_record_honors_dedupe_ttl(tmp_path, monkeypatch) -> None:
    from orchestratord.im_gateway import store as store_mod

    now = [1000.0]
    monkeypatch.setattr(store_mod.time, "time", lambda: now[0])
    s = ReliabilityStore(tmp_path, ReliabilityConfig(inbound_dedupe_ttl_seconds=1))
    assert s.check_and_record("k1", message_id="m1") is True
    assert s.check_and_record("k1", message_id="m1") is False
    now[0] += 2.0
    assert s.check_and_record("k1", message_id="m1") is True


def test_store_outbox_and_dead_letter(tmp_path) -> None:
    s = ReliabilityStore(tmp_path)
    s.append_outbox({"idempotency_key": "o1", "channel": "c", "status": "pending"})
    s.append_outbox({"idempotency_key": "o1", "channel": "c", "status": "delivered"})
    s.append_outbox({"idempotency_key": "o2", "channel": "c", "status": "pending"})
    pending = s.outbox_pending()
    assert {e["idempotency_key"] for e in pending} == {"o2"}
    s.append_dead_letter({"idempotency_key": "o2", "reason": "boom"})
    assert len(s.dead_letter_entries()) == 1
    s.append_outbox({"idempotency_key": "o2", "channel": "c", "status": "failed"})
    assert s.outbox_pending() == []


def test_store_context_tokens(tmp_path) -> None:
    s = ReliabilityStore(tmp_path)
    s.set_context_token("acct", "user1", "tok_abc")
    assert s.get_context_token("acct", "user1") == "tok_abc"
    s.set_context_token("acct", "user1", None)
    assert s.get_context_token("acct", "user1") is None


def test_store_audit(tmp_path) -> None:
    s = ReliabilityStore(tmp_path)
    s.audit("binding_override", origin="o1", session_id="s1")
    entries = s.audit_entries()
    assert len(entries) == 1
    assert entries[0]["event_type"] == "binding_override"


# -- binding + router ----------------------------------------------------


def test_binding_unique_target_and_override_audit() -> None:
    audits: list[tuple] = []
    bp = BindingPolicy(auditor=lambda action, entry, prev: audits.append((action, entry.origin)))
    o = OriginKey.wechat("default", "user_gz")
    bp.bind(o, SessionTarget("repl_main", "repl"))
    assert bp.is_opt_in(o)
    # override
    bp.bind(o, SessionTarget("run_xyz", "orchestrator"))
    assert bp.get(o).target.session_id == "run_xyz"
    actions = [a for a, _ in audits]
    assert "binding_created" in actions
    assert "binding_override" in actions


def test_binding_terminate_restores_default_route() -> None:
    bp = BindingPolicy()
    o = OriginKey.wechat("default", "user_gz")
    bp.bind(o, SessionTarget("repl_main", "repl"))
    assert bp.is_opt_in(o)
    bp.terminate(o)
    assert not bp.is_opt_in(o)


def test_router_default_when_no_binding(tmp_path) -> None:
    store = ReliabilityStore(tmp_path)
    bp = BindingPolicy()
    router = SessionRouter(bp, store)
    o = OriginKey.wechat("default", "user_gz")
    target = router.route(o)
    assert target.host_type == "default"
    assert "im:default:" in target.session_id


def test_router_ignores_legacy_session_map(tmp_path) -> None:
    (tmp_path / "im_session_map.json").write_text(
        '{"wechat:direct:default:user_gz": {"session_id": "stale", "host_type": "repl"}}',
        encoding="utf-8",
    )
    store = ReliabilityStore(tmp_path)
    router = SessionRouter(BindingPolicy(), store)

    target = router.route(OriginKey.wechat("default", "user_gz"))

    assert target.host_type == "default"
    assert target.session_id == "im:default:wechat:direct:default:user_gz"


def test_router_opt_in_overrides_default(tmp_path) -> None:
    store = ReliabilityStore(tmp_path)
    bp = BindingPolicy()
    o = OriginKey.wechat("default", "user_gz")
    bp.bind(o, SessionTarget("repl_main", "repl"))
    router = SessionRouter(bp, store)
    assert router.route(o).session_id == "repl_main"
    assert router.is_opt_in(o)


def test_router_wechat_direct_wildcard_binding_matches_any_private_sender(tmp_path) -> None:
    store = ReliabilityStore(tmp_path)
    bp = BindingPolicy()
    bp.bind("wechat:direct:*:*", SessionTarget("repl_all_private", "repl"))
    router = SessionRouter(bp, store)

    assert router.route("wechat:direct:acct_a:user_1").session_id == "repl_all_private"
    assert router.route("wechat:direct:acct_b:user_2").session_id == "repl_all_private"
    assert router.is_opt_in("wechat:direct:acct_a:user_1")


def test_router_generic_direct_wildcard_binding_matches_wechat_and_feishu(tmp_path) -> None:
    store = ReliabilityStore(tmp_path)
    bp = BindingPolicy()
    bp.bind(IM_DIRECT_ALL_ORIGIN, SessionTarget("repl_all_im", "repl"))
    router = SessionRouter(bp, store)

    assert router.route("wechat:direct:acct_a:user_1").session_id == "repl_all_im"
    assert router.route("feishu:dm:cli_app:ou_user").session_id == "repl_all_im"
    assert router.is_opt_in("feishu:dm:cli_app:ou_user")


def test_router_wechat_direct_wildcard_offline_applies_to_matching_private_sender(
    tmp_path,
) -> None:
    store = ReliabilityStore(tmp_path)
    bp = BindingPolicy()
    bp.bind("wechat:direct:*:*", SessionTarget("repl_all_private", "repl"))
    bp.mark_offline("wechat:direct:*:*")
    router = SessionRouter(bp, store)

    assert router.is_offline("wechat:direct:acct_a:user_1")


# -- capability gate -----------------------------------------------------


def test_capability_gate_fail_closed_for_undeclared() -> None:
    adapter = _FakeAdapter(caps=ChannelCapabilitySet.of(ChannelCapability.OUTBOUND_TEXT))
    reg = _registry_with(adapter)
    gate = CapabilityGate(reg)
    gate.require_outbound("fake")  # ok
    with pytest.raises(CapabilityNotDeclaredError):
        gate.require_media("fake", ChannelCapability.MEDIA_IMAGE)
    with pytest.raises(CapabilityNotDeclaredError):
        gate.require_context_reply("fake")


def test_capability_gate_rejects_non_media_capability() -> None:
    reg = _registry_with(_FakeAdapter())
    gate = CapabilityGate(reg)
    with pytest.raises(ValueError):
        gate.require_media("fake", ChannelCapability.OUTBOUND_TEXT)


# -- MessageGateway facade ------------------------------------------------


def _gateway(tmp_path, *, adapter: _FakeAdapter | None = None) -> MessageGateway:
    reg = ChannelAdapterRegistry()
    if adapter is not None:
        reg.register(adapter)
    cfg = GatewayConfig(state_dir=str(tmp_path))
    return MessageGateway(cfg, registry=reg)


def test_gateway_applies_configured_command_allowlists(tmp_path) -> None:
    cfg = GatewayConfig(
        state_dir=str(tmp_path),
        command_allowlists=CommandAllowlistConfig(
            repl=("/model",),
            orchestrator=("/issue takeover",),
        ),
    )

    gateway = MessageGateway(cfg, registry=ChannelAdapterRegistry())

    assert gateway.inbound._repl_allowed_commands == {"/model"}
    assert gateway.inbound._orchestrator_allowed_commands == {"/issue takeover"}


class _FakeInboundAdapter(_FakeAdapter):
    """Inbound adapter whose account_status flips to connected after N polls."""

    def __init__(self, name: str = "fake-in", *, connect_after: int = 0) -> None:
        super().__init__(name)
        self._caps = ChannelCapabilitySet.of(
            ChannelCapability.OUTBOUND_TEXT,
            ChannelCapability.INBOUND_POLLING,
        )
        self._polls = 0
        self._connect_after = connect_after

    def set_inbound_handler(self, handler) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def health_check(self) -> ChannelHealth:
        self._polls += 1
        status = (
            "websocket:connected" if self._polls > self._connect_after else "websocket:reconnecting"
        )
        return ChannelHealth(
            healthy=self._polls > self._connect_after,
            channel_id=self._name,
            account_status=status,
        )


@pytest.mark.asyncio
async def test_gateway_wait_channels_ready_returns_when_connected(tmp_path) -> None:
    adapter = _FakeInboundAdapter("feishu", connect_after=2)
    gw = _gateway(tmp_path, adapter=adapter)
    # Simulate gateway having attached it as inbound.
    gw._inbound_adapters.append(adapter)

    result = await gw.wait_channels_ready(timeout=5.0)

    assert result == {"feishu": "websocket:connected"}


@pytest.mark.asyncio
async def test_gateway_wait_channels_ready_times_out_degraded(tmp_path) -> None:
    adapter = _FakeInboundAdapter("feishu", connect_after=1000)  # never connects
    gw = _gateway(tmp_path, adapter=adapter)
    gw._inbound_adapters.append(adapter)

    result = await gw.wait_channels_ready(timeout=1.5)

    assert result["feishu"] == "websocket:reconnecting"


@pytest.mark.asyncio
async def test_gateway_send_uses_outbound_dispatcher(tmp_path) -> None:
    adapter = _FakeAdapter("wechat-main")
    gw = _gateway(tmp_path, adapter=adapter)
    result = await gw.send(OutboundMessage(text="hello", channel="wechat-main"))
    assert result.ok is True
    assert len(adapter.sends) == 1


@pytest.mark.asyncio
async def test_gateway_broadcast(tmp_path) -> None:
    a1 = _FakeAdapter("a")
    a2 = _FakeAdapter("b")
    reg = ChannelAdapterRegistry()
    reg.register(a1)
    reg.register(a2)
    gw = MessageGateway(GatewayConfig(state_dir=str(tmp_path)), registry=reg)
    results = await gw.broadcast(OutboundMessage(text="hi", channel="a"))
    assert results["a"].ok and results["b"].ok


@pytest.mark.asyncio
async def test_gateway_inbound_dedupe_and_classify(tmp_path) -> None:
    gw = _gateway(tmp_path)
    msg = InboundMessage(
        origin="wechat:direct:default:u", text="hello", message_id="m1", channel="c"
    )
    r1 = await gw.receive(msg)
    assert r1.message != "duplicate; skipped"
    r2 = await gw.receive(msg)
    assert r2.message == "duplicate; skipped"
    assert msg.semantic is MessageSemantics.NEW_PROMPT


@pytest.mark.asyncio
async def test_gateway_inbound_pushes_to_opt_in_bound_origin(tmp_path) -> None:
    """When an origin is bound to an opt-in peer, dispatch pushes via IPC,
    NOT the default stub handler."""
    gw = _gateway(tmp_path)
    pushed: list[InboundMessage] = []

    async def _push(msg):
        pushed.append(msg)
        return True

    gw.set_push_handler(_push)
    # bind the origin to a REPL opt-in target
    gw.binding.bind(
        "wechat:direct:default:u",
        SessionTarget(session_id="repl_main", host_type="repl"),
    )
    handler_calls: list[InboundMessage] = []

    async def _ack():
        return AckReceipt("d", AckLayer.PROCESSED, "stub")

    gw.set_handler(lambda m: handler_calls.append(m) or _ack())

    msg = InboundMessage(
        origin="wechat:direct:default:u", text="/clear", message_id="m1", channel="wechat-main"
    )
    await gw.receive(msg)
    assert len(pushed) == 1  # pushed to the opt-in peer
    assert pushed[0].text == "/clear"
    assert handler_calls == []  # default handler NOT called (opt-in overrides)


@pytest.mark.asyncio
async def test_gateway_inbound_pushes_feishu_to_generic_opt_in_binding(tmp_path) -> None:
    """A channel-neutral opt-in binding must catch Feishu DM origins."""
    gw = _gateway(tmp_path)
    pushed: list[InboundMessage] = []

    async def _push(msg):
        pushed.append(msg)
        return True

    gw.set_push_handler(_push)
    gw.binding.bind(
        IM_DIRECT_ALL_ORIGIN,
        SessionTarget(session_id="repl_main", host_type="repl"),
    )

    msg = InboundMessage(
        origin="feishu:dm:cli_app:ou_user",
        text="/clear",
        message_id="m-feishu",
        channel="feishu",
        context_token="oc_chat",
    )
    ack = await gw.receive(msg)

    assert ack.message == "pushed to opt-in peer"
    assert len(pushed) == 1
    assert pushed[0].origin == "feishu:dm:cli_app:ou_user"
    assert pushed[0].context_token == "oc_chat"


@pytest.mark.asyncio
async def test_gateway_notifies_feishu_sender_when_repl_command_is_blocked(tmp_path) -> None:
    adapter = _FakeAdapter("feishu")
    gw = _gateway(tmp_path, adapter=adapter)
    pushed: list[InboundMessage] = []

    async def _push(msg):
        pushed.append(msg)
        return True

    gw.set_push_handler(_push)
    gw.binding.bind(
        IM_DIRECT_ALL_ORIGIN,
        SessionTarget(session_id="repl_main", host_type="repl"),
    )
    msg = InboundMessage(
        origin="feishu:dm:cli_app:ou_user",
        text="/exit",
        message_id="m-feishu-blocked",
        channel="feishu",
        context_token="oc_chat",
        from_user_id="ou_user",
    )

    ack = await gw._on_inbound(msg)

    assert pushed == []
    assert getattr(ack, "notify_user", False) is True
    assert len(adapter.send_calls) == 1
    call = adapter.send_calls[0]
    assert call["target"] == "ou_user"
    assert call["context_token"] == "oc_chat"
    assert "/exit" in call["message"].text


@pytest.mark.asyncio
async def test_gateway_notifies_feishu_sender_when_orchestrator_command_is_blocked(
    tmp_path,
) -> None:
    adapter = _FakeAdapter("feishu")
    gw = _gateway(tmp_path, adapter=adapter)
    pushed: list[InboundMessage] = []

    async def _push(msg):
        pushed.append(msg)
        return True

    gw.set_push_handler(_push)
    gw.binding.bind(
        IM_DIRECT_ALL_ORIGIN,
        SessionTarget(session_id="orch_main", host_type="orchestrator"),
    )
    msg = InboundMessage(
        origin="feishu:dm:cli_app:ou_user",
        text="/server stop",
        message_id="m-feishu-orch-blocked",
        channel="feishu",
        context_token="oc_chat",
        from_user_id="ou_user",
    )

    ack = await gw._on_inbound(msg)

    assert pushed == []
    assert getattr(ack, "notify_user", False) is True
    assert ack.message == "不支持 /server stop 执行"
    assert len(adapter.send_calls) == 1
    call = adapter.send_calls[0]
    assert call["target"] == "ou_user"
    assert call["context_token"] == "oc_chat"
    assert call["message"].text == "不支持 /server stop 执行"


@pytest.mark.asyncio
async def test_gateway_inbound_default_origin_still_uses_handler(tmp_path) -> None:
    """Unbound (default) origins still go to the stub/handler, not push."""
    gw = _gateway(tmp_path)
    pushed: list[InboundMessage] = []

    async def _push(msg):
        pushed.append(msg)
        return True

    gw.set_push_handler(_push)
    handler_calls: list[InboundMessage] = []

    async def _ack():
        return AckReceipt("d", AckLayer.PROCESSED, "stub")

    gw.set_handler(lambda m: handler_calls.append(m) or _ack())

    msg = InboundMessage(
        origin="wechat:direct:default:u", text="/help", message_id="m1", channel="wechat-main"
    )
    await gw.receive(msg)
    assert pushed == []  # no opt-in binding → no push
    assert len(handler_calls) == 1  # default handler called


@pytest.mark.asyncio
async def test_gateway_inbound_classifies_slash_as_command(tmp_path) -> None:
    gw = _gateway(tmp_path)
    msg = InboundMessage(origin="o", text="/agent retry AGENTSDK-15", message_id="m1", channel="c")
    await gw.receive(msg)
    assert msg.semantic is MessageSemantics.COMMAND


@pytest.mark.asyncio
async def test_gateway_reload_channel_rebuilds(tmp_path) -> None:
    # registry with a fake factory for "slack" type
    reg = ChannelAdapterRegistry()

    def _factory(cfg: ChannelConfig) -> _FakeAdapter:
        return _FakeAdapter(cfg.name)

    reg.register_type(ChannelType.SLACK, _factory)
    cfg = GatewayConfig(state_dir=str(tmp_path))
    cfg.channels.append(
        ChannelConfig(
            type=ChannelType.SLACK,
            webhook_url="https://hooks.example.com/x",
            name="slack-ops",
        )
    )
    config_path = save_config(cfg, tmp_path / "channels.yaml")
    gw = MessageGateway(cfg, registry=reg, config_path=config_path)
    assert await gw.reload_channel("slack-ops") is True
    assert gw.registry.get("slack-ops") is not None
    assert await gw.reload_channel("nope") is False


@pytest.mark.asyncio
async def test_gateway_reload_channel_is_transactional_on_bad_disk_config(tmp_path) -> None:
    """P1-4: an invalid on-disk config fails the reload and keeps the old
    adapter serving; the in-memory config is untouched."""
    reg = ChannelAdapterRegistry()
    built: list[str] = []

    def _factory(cfg: ChannelConfig) -> _FakeAdapter:
        built.append(cfg.name)
        return _FakeAdapter(cfg.name)

    reg.register_type(ChannelType.SLACK, _factory)
    cfg = GatewayConfig(state_dir=str(tmp_path))
    cfg.channels.append(
        ChannelConfig(
            type=ChannelType.SLACK,
            webhook_url="https://hooks.example.com/x",
            name="slack-ops",
        )
    )
    config_path = tmp_path / "channels.yaml"
    save_config(cfg, config_path)
    gw = MessageGateway(cfg, registry=reg, config_path=config_path)
    old_adapter = gw.registry.get("slack-ops")
    assert old_adapter is not None
    initial_builds = len(built)  # the constructor loaded the channel once

    # Corrupt the on-disk config: reload must fail and keep the old adapter.
    config_path.write_text("channels: [ {not valid yaml", encoding="utf-8")
    assert await gw.reload_channel("slack-ops") is False
    assert gw.registry.get("slack-ops") is old_adapter
    assert [c.name for c in gw.config.channels] == ["slack-ops"]
    assert len(built) == initial_builds  # no new adapter was constructed

    # A valid config without the channel also fails, old adapter intact.
    other = GatewayConfig(state_dir=str(tmp_path))
    other.channels = []
    save_config(other, config_path)
    assert await gw.reload_channel("slack-ops") is False
    assert gw.registry.get("slack-ops") is old_adapter
    assert len(built) == initial_builds


@pytest.mark.asyncio
async def test_gateway_reload_channel_picks_up_disk_changes(tmp_path) -> None:
    """A successful reload adopts the on-disk config (e.g. a new webhook)."""
    reg = ChannelAdapterRegistry()
    built: list[ChannelConfig] = []

    def _factory(cfg: ChannelConfig) -> _FakeAdapter:
        built.append(cfg)
        return _FakeAdapter(cfg.name)

    reg.register_type(ChannelType.SLACK, _factory)
    cfg = GatewayConfig(state_dir=str(tmp_path))
    cfg.channels.append(
        ChannelConfig(
            type=ChannelType.SLACK,
            webhook_url="https://hooks.example.com/old",
            name="slack-ops",
        )
    )
    config_path = save_config(cfg, tmp_path / "channels.yaml")
    gw = MessageGateway(cfg, registry=reg, config_path=config_path)

    disk_cfg = GatewayConfig(state_dir=str(tmp_path))
    disk_cfg.channels.append(
        ChannelConfig(
            type=ChannelType.SLACK,
            webhook_url="https://hooks.example.com/new",
            name="slack-ops",
        )
    )
    save_config(disk_cfg, config_path)

    assert await gw.reload_channel("slack-ops") is True
    # One build at construction + one for the reload itself.
    assert len(built) == 2
    assert built[-1].webhook_url == "https://hooks.example.com/new"
    assert [c.webhook_url for c in gw.config.channels] == ["https://hooks.example.com/new"]


@pytest.mark.asyncio
async def test_gateway_start_replays_pending_outbox(tmp_path) -> None:
    """P4 outbox recovery: records a crashed process left pending are re-sent
    at startup under the original idempotency key; legacy records without a
    payload are marked dead instead of blocking the backlog."""
    import time as _time

    adapter = _FakeAdapter("slack-ops")
    reg = _registry_with(adapter)
    cfg = GatewayConfig(state_dir=str(tmp_path))
    # Simulate a predecessor that wrote pending records and died: a
    # payload-bearing pending record and a legacy pending record.
    with (tmp_path / "outbox.ndjson").open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "idempotency_key": "out-1",
                    "channel": "slack-ops",
                    "target": "C123",
                    "text": "issue AGENTSDK-1 finished",
                    "markdown": True,
                    "status": "pending",
                    "at": _time.time(),
                }
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "idempotency_key": "out-legacy",
                    "channel": "slack-ops",
                    "payload_size": 42,
                    "status": "pending",
                    "at": _time.time(),
                }
            )
            + "\n"
        )

    gw = MessageGateway(cfg, registry=reg)
    await gw.start()
    try:
        # The recoverable record was re-sent exactly once with its payload.
        assert len(adapter.send_calls) == 1
        call = adapter.send_calls[0]
        assert call["target"] == "C123"
        assert call["message"].text == "issue AGENTSDK-1 finished"
        # The replay outcome is recorded: delivered for the replayed send,
        # dead for the legacy no-payload record.
        entries = gw.store.outbox_entries()
        statuses = {
            (e["idempotency_key"], e["status"]) for e in entries if e.get("status")
        }
        assert ("out-1", "delivered") in statuses
        assert ("out-legacy", "dead") in statuses
        assert gw.store.outbox_pending() == []
    finally:
        await gw.stop()


def test_gateway_normalizes_duplicate_channel_types_before_runtime_load(tmp_path) -> None:
    reg = ChannelAdapterRegistry()

    def _factory(cfg: ChannelConfig) -> _FakeAdapter:
        return _FakeAdapter(cfg.name)

    reg.register_type(ChannelType.SLACK, _factory)
    cfg = GatewayConfig(state_dir=str(tmp_path))
    cfg.channels = [
        ChannelConfig(
            type=ChannelType.SLACK,
            webhook_url="https://hooks.example.com/old",
            name="slack-old",
        ),
        ChannelConfig(
            type=ChannelType.SLACK,
            webhook_url="https://hooks.example.com/new",
            name="slack-new",
        ),
    ]

    gw = MessageGateway(cfg, registry=reg)

    assert gw.registry.names() == ["slack-new"]
    assert [c.name for c in gw.config.channels] == ["slack-new"]


@pytest.mark.asyncio
async def test_gateway_health(tmp_path) -> None:
    gw = _gateway(tmp_path, adapter=_FakeAdapter("wechat-main"))
    health = await gw.health()
    assert health["running"] is False
    assert "wechat-main" in health["channels"]
    assert health["outbox_pending"] == 0


@pytest.mark.asyncio
async def test_gateway_stop_logs_stopped_once_when_called_concurrently(tmp_path, caplog) -> None:
    class _SlowStopAdapter(_FakeInboundAdapter):
        async def stop(self) -> None:
            await asyncio.sleep(0.05)

    adapter = _SlowStopAdapter("feishu")
    gw = _gateway(tmp_path, adapter=adapter)
    gw._inbound_adapters.append(adapter)
    await gw.start()

    caplog.set_level("INFO", logger="orchestratord.im_gateway.gateway")
    await asyncio.gather(gw.stop(), gw.stop())

    stopped = [
        record
        for record in caplog.records
        if record.name == "orchestratord.im_gateway.gateway"
        and record.getMessage() == "gateway stopped"
    ]
    assert len(stopped) == 1


def test_gateway_loads_wechat_channel_from_config(tmp_path) -> None:
    cfg = GatewayConfig(state_dir=str(tmp_path))
    cfg.channels.append(
        ChannelConfig(
            type=ChannelType.WECHAT,
            webhook_url="https://ilinkai.weixin.qq.com/dummy",
            name="wechat",
            enabled=True,
            extra={
                "base_url": "https://ilinkai.weixin.qq.com",
                "account_id": "default",
                "allowed_users": [],
            },
        )
    )
    gw = MessageGateway(cfg)
    adapter = gw.registry.get("wechat")
    assert adapter is not None
    assert adapter.capabilities.has(ChannelCapability.INBOUND_POLLING)
    assert any(a.channel_id == "wechat" for a in gw._inbound_adapters)
    assert adapter._account_status == "unconfigured"


def test_gateway_loads_wechat_and_feishu_inbound_channels_together(tmp_path) -> None:
    cfg = GatewayConfig(state_dir=str(tmp_path))
    cfg.channels.extend(
        [
            ChannelConfig(
                type=ChannelType.WECHAT,
                webhook_url="https://ilinkai.weixin.qq.com/dummy",
                name="wechat",
                enabled=True,
                extra={"account_id": "default"},
            ),
            ChannelConfig(
                type=ChannelType.FEISHU,
                webhook_url="",
                name="feishu",
                enabled=True,
                extra={
                    "connection_mode": "websocket",
                    "app_id": "cli_test",
                    "app_secret": "test-secret",
                },
            ),
        ]
    )

    gateway = MessageGateway(cfg)

    assert set(gateway.registry.names()) == {"wechat", "feishu"}
    assert {adapter.channel_id for adapter in gateway._inbound_adapters} == {
        "wechat",
        "feishu",
    }


def test_gateway_normalizes_legacy_wechat_name_and_reuses_legacy_auth(tmp_path) -> None:
    from orchestratord.channels.wechat_ilink import (
        WeChatAuthRecord,
        WeChatIlinkAuthStore,
    )

    wechat_dir = tmp_path / "wechat"
    old_auth = wechat_dir / "wechat-main_auth.json"
    WeChatIlinkAuthStore(old_auth).save(
        WeChatAuthRecord(
            bot_token="bot_tok_123",
            account_id="acct",
            base_url="https://ilinkai.weixin.qq.com",
            user_id="bot_user",
        )
    )
    cfg = GatewayConfig(state_dir=str(tmp_path))
    cfg.channels.append(
        ChannelConfig(
            type=ChannelType.WECHAT,
            webhook_url="https://ilinkai.weixin.qq.com/dummy",
            name="wechat-main",
            enabled=True,
            extra={"base_url": "https://ilinkai.weixin.qq.com", "account_id": "default"},
        )
    )

    gw = MessageGateway(cfg)

    assert gw.registry.get("wechat-main") is None
    adapter = gw.registry.get("wechat")
    assert adapter is not None
    assert adapter._account_status == "logged_in"
    assert adapter._account_id == "acct"


def test_gateway_skips_disabled_channels(tmp_path) -> None:
    cfg = GatewayConfig(state_dir=str(tmp_path))
    cfg.channels.append(
        ChannelConfig(
            type=ChannelType.WECHAT,
            webhook_url="https://ilinkai.weixin.qq.com/dummy",
            name="wechat-off",
            enabled=False,
        )
    )
    gw = MessageGateway(cfg)
    assert gw.registry.get("wechat-off") is None


@pytest.mark.asyncio
async def test_gateway_wechat_adapter_build_fails_soft_without_extras(
    tmp_path, monkeypatch
) -> None:
    """A wechat channel without the gateway-wechat extras must not crash the
    gateway constructor — the adapter build degrades to a warning and the
    gateway still serves configured registry-based channels."""
    import sys

    from orchestratord.channels.models import ChannelType

    # Simulate a base environment where the extras (and therefore the
    # wechat_ilink module) are absent: a ``None`` entry in ``sys.modules``
    # makes ``from orchestratord.channels.wechat_ilink import ...`` raise
    # ImportError inside the gateway's lazy-import branch.
    monkeypatch.setitem(sys.modules, "orchestratord.channels.wechat_ilink", None)

    reg = ChannelAdapterRegistry()

    def _factory(cfg: ChannelConfig) -> _FakeAdapter:
        return _FakeAdapter(cfg.name)

    reg.register_type(ChannelType.SLACK, _factory)
    cfg = GatewayConfig(state_dir=str(tmp_path))
    cfg.channels.append(
        ChannelConfig(
            type=ChannelType.WECHAT,
            webhook_url="https://ilinkai.weixin.qq.com/dummy",
            name="wechat",
            enabled=True,
        )
    )
    cfg.channels.append(
        ChannelConfig(
            type=ChannelType.SLACK,
            webhook_url="https://hooks.example.com/x",
            name="slack-ops",
        )
    )
    gw = MessageGateway(cfg, registry=reg)

    assert gw.registry.get("slack-ops") is not None
    assert gw.registry.get("wechat") is None


# -- connection notification ----------------------------------------------


class _FakeWeChatAdapter(ChannelAdapter):
    """Minimal WeChat adapter for notification tests."""

    def __init__(self, name: str = "wechat", account_id: str = "default") -> None:
        self._name = name
        self._account_id = account_id
        self._config = ChannelConfig(
            type=ChannelType.WECHAT,
            webhook_url="https://ilinkai.weixin.qq.com/dummy",
            name=name,
            enabled=True,
        )
        self._caps = ChannelCapabilitySet.of(
            ChannelCapability.OUTBOUND_TEXT,
            ChannelCapability.INBOUND_POLLING,
            ChannelCapability.CONTEXT_REPLY,
            descriptors={
                ChannelCapability.OUTBOUND_TEXT: CapabilityDescriptor(
                    ChannelCapability.OUTBOUND_TEXT, supports_markdown=False
                )
            },
        )
        self.sends: list[tuple[ChannelMessage, str | None]] = []
        self._last_sender: str | None = None

    @property
    def channel_id(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ChannelCapabilitySet:
        return self._caps

    @property
    def config(self) -> ChannelConfig:
        return self._config

    def validate_config(self) -> ValidationResult:
        return ValidationResult.ok_result()

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(healthy=True, channel_id=self._name)

    async def send(self, message, *, target=None, context_token=None) -> ChannelSendResult:
        self.sends.append((message, target))
        return ChannelSendResult.success(self._name, provider_receipt="r")

    def last_known_sender(self) -> str | None:
        return self._last_sender

    def set_inbound_handler(self, handler) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _gateway_with_wechat(
    tmp_path, *, sender: str | None = "operator@im.wechat"
) -> tuple[MessageGateway, _FakeWeChatAdapter]:
    """Build a real gateway with a fake WeChat adapter that has a known sender."""
    adapter = _FakeWeChatAdapter("wechat")
    adapter._last_sender = sender
    reg = ChannelAdapterRegistry()
    reg.register(adapter)
    cfg = GatewayConfig(state_dir=str(tmp_path))
    gw = MessageGateway(cfg, registry=reg)
    return gw, adapter


@pytest.mark.asyncio
async def test_notify_on_connect_repl(tmp_path) -> None:
    """binding_created → sends 'orchestratord-REPL已连接' to the WeChat user."""
    gw, adapter = _gateway_with_wechat(tmp_path)

    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="repl-1", host_type="repl"),
    )
    await asyncio.sleep(0.05)  # let create_task notification run

    texts = [msg.text for msg, _ in adapter.sends]
    assert "orchestratord-REPL已连接" in texts


@pytest.mark.asyncio
async def test_notify_on_connect_orchestrator(tmp_path) -> None:
    """binding_created → sends 'orchestratord-orchestrator已连接，当前Orchestrator仅支持命令交互'."""
    gw, adapter = _gateway_with_wechat(tmp_path)

    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="orchestrator-1", host_type="orchestrator"),
    )
    await asyncio.sleep(0.05)

    texts = [msg.text for msg, _ in adapter.sends]
    assert "orchestratord-orchestrator已连接，当前Orchestrator仅支持命令交互" in texts


@pytest.mark.asyncio
async def test_notify_on_disconnect_offline(tmp_path) -> None:
    """binding_offline → sends 'orchestratord-REPL已断开'."""
    gw, adapter = _gateway_with_wechat(tmp_path)

    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="repl-1", host_type="repl"),
    )
    await asyncio.sleep(0.05)
    adapter.sends.clear()

    gw.binding.mark_offline("wechat:direct:*:*", session_id="repl-1")
    await asyncio.sleep(0.05)

    texts = [msg.text for msg, _ in adapter.sends]
    assert "orchestratord-REPL已断开" in texts


@pytest.mark.asyncio
async def test_repeated_offline_transition_does_not_duplicate_disconnect(tmp_path) -> None:
    """A stale socket cleanup must not announce the same disconnect twice."""
    gw, adapter = _gateway_with_wechat(tmp_path)

    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="orchestrator-1", host_type="orchestrator"),
    )
    await asyncio.sleep(0.05)
    adapter.sends.clear()

    gw.binding.mark_offline("wechat:direct:*:*", session_id="orchestrator-1")
    gw.binding.mark_offline("wechat:direct:*:*", session_id="orchestrator-1")
    await asyncio.sleep(0.05)

    texts = [msg.text for msg, _ in adapter.sends]
    assert texts.count("orchestratord-orchestrator已断开") == 1


@pytest.mark.asyncio
async def test_notify_on_disconnect_terminated(tmp_path) -> None:
    """binding_terminated → sends 'orchestratord-orchestrator已断开'."""
    gw, adapter = _gateway_with_wechat(tmp_path)

    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="orch-1", host_type="orchestrator"),
    )
    await asyncio.sleep(0.05)
    adapter.sends.clear()

    gw.binding.terminate("wechat:direct:*:*", session_id="orch-1")
    await asyncio.sleep(0.05)

    texts = [msg.text for msg, _ in adapter.sends]
    assert "orchestratord-orchestrator已断开" in texts


@pytest.mark.asyncio
async def test_notify_on_override_active_previous(tmp_path) -> None:
    """binding_override with active previous → disconnect + connect notices."""
    gw, adapter = _gateway_with_wechat(tmp_path)

    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="orch-1", host_type="orchestrator"),
    )
    await asyncio.sleep(0.05)
    adapter.sends.clear()

    # REPL replaces orchestrator (both in wechat:direct exclusive group)
    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="repl-1", host_type="repl"),
    )
    await asyncio.sleep(0.05)

    texts = [msg.text for msg, _ in adapter.sends]
    assert "orchestratord-orchestrator已断开" in texts
    assert "orchestratord-REPL已连接" in texts
    # Disconnect notification should come before connect notification
    assert texts.index("orchestratord-orchestrator已断开") < texts.index("orchestratord-REPL已连接")


@pytest.mark.asyncio
async def test_notify_on_override_offline_previous(tmp_path) -> None:
    """binding_override with offline previous → only connect notice (no duplicate)."""
    gw, adapter = _gateway_with_wechat(tmp_path)

    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="orch-1", host_type="orchestrator"),
    )
    await asyncio.sleep(0.05)
    # Mark offline first (e.g. socket closed before REPL registers)
    gw.binding.mark_offline("wechat:direct:*:*", session_id="orch-1")
    await asyncio.sleep(0.05)
    adapter.sends.clear()

    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="repl-1", host_type="repl"),
    )
    await asyncio.sleep(0.05)

    texts = [msg.text for msg, _ in adapter.sends]
    assert "orchestratord-REPL已连接" in texts
    assert "orchestratord-orchestrator已断开" not in texts  # already offline, no duplicate


@pytest.mark.asyncio
async def test_notify_skipped_when_origin_unresolvable(tmp_path) -> None:
    """If origin can't be resolved (no known sender), no notification is sent."""
    gw, adapter = _gateway_with_wechat(tmp_path, sender=None)

    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="repl-1", host_type="repl"),
    )
    await asyncio.sleep(0.05)

    assert adapter.sends == []  # no notification — can't address the user


@pytest.mark.asyncio
async def test_notify_best_effort_does_not_raise(tmp_path) -> None:
    """If outbound.send raises, the notification is swallowed (best-effort)."""
    gw, adapter = _gateway_with_wechat(tmp_path)

    # Make send raise
    original_send = adapter.send

    async def _raising_send(message, *, target=None, context_token=None):
        raise RuntimeError("simulated send failure")

    adapter.send = _raising_send

    # Should not raise
    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="repl-1", host_type="repl"),
    )
    await asyncio.sleep(0.05)

    # Restore and verify gateway is still functional
    adapter.send = original_send


@pytest.mark.asyncio
async def test_notify_failed_send_result_is_not_logged_as_sent(tmp_path, caplog) -> None:
    """A non-ok send result is a failed notification, not a delivered one."""
    gw, adapter = _gateway_with_wechat(tmp_path)

    async def _failed_send(message, *, target=None, context_token=None):
        adapter.sends.append((message, target))
        return ChannelSendResult.nonretryable_error(
            "wechat",
            message="session expired",
            category=ErrorCategory.AUTH,
        )

    adapter.send = _failed_send
    caplog.set_level(logging.INFO, logger="orchestratord.im_gateway.gateway")

    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="orch-1", host_type="orchestrator"),
    )
    await asyncio.sleep(0.05)

    assert "connection notify: send failed" in caplog.text
    assert "connection notify: sent 'orchestratord-orchestrator已连接'" not in caplog.text


@pytest.mark.asyncio
async def test_notify_enqueued_send_result_is_not_logged_as_sent(tmp_path, caplog) -> None:
    """A deferred notification is accepted for later delivery, not delivered."""
    gw, adapter = _gateway_with_wechat(tmp_path)

    async def _enqueued_send(message, *, target=None, context_token=None):
        adapter.sends.append((message, target))
        return ChannelSendResult.enqueued(
            "wechat",
            message="deferred due to rate limit",
            raw={"retry_after_seconds": 10},
        )

    adapter.send = _enqueued_send
    caplog.set_level(logging.INFO, logger="orchestratord.im_gateway.gateway")

    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="orch-1", host_type="orchestrator"),
    )
    await asyncio.sleep(0.05)

    assert "connection notify: enqueued" in caplog.text


@pytest.mark.asyncio
async def test_notify_concrete_origin(tmp_path) -> None:
    """Concrete origin wechat:direct:{account}:{user} resolves and sends."""
    gw, adapter = _gateway_with_wechat(tmp_path)

    gw.binding.bind(
        "wechat:direct:default:operator@im.wechat",
        SessionTarget(session_id="repl-1", host_type="repl"),
    )
    await asyncio.sleep(0.05)

    texts = [msg.text for msg, _ in adapter.sends]
    assert "orchestratord-REPL已连接" in texts
    # Verify it was sent to the correct target
    targets = [t for _, t in adapter.sends if t is not None]
    assert "operator@im.wechat" in targets


@pytest.mark.asyncio
async def test_notify_terminate_matching_sends_for_each(tmp_path) -> None:
    """terminate_matching sends '已断开' for each removed binding."""
    gw, adapter = _gateway_with_wechat(tmp_path)

    gw.binding.bind(
        "wechat:direct:default:user_a",
        SessionTarget(session_id="repl-1", host_type="repl"),
    )
    await asyncio.sleep(0.05)
    adapter.sends.clear()

    # terminate_matching removes all bindings in the wechat:direct group
    removed = gw.binding.terminate_matching("wechat:direct:*:*")
    assert len(removed) >= 1
    await asyncio.sleep(0.05)

    texts = [msg.text for msg, _ in adapter.sends]
    assert "orchestratord-REPL已断开" in texts


class _FakeFeishuAdapter(ChannelAdapter):
    """Minimal Feishu adapter for notification broadcast tests."""

    def __init__(self, name: str = "feishu", last_sender: str | None = "ou_feishu_user") -> None:
        self._name = name
        self._config = ChannelConfig(
            type=ChannelType.FEISHU,
            webhook_url="",
            name=name,
            enabled=True,
            extra={"connection_mode": "websocket"},
        )
        self._caps = ChannelCapabilitySet.of(
            ChannelCapability.OUTBOUND_TEXT,
            ChannelCapability.INBOUND_POLLING,
            ChannelCapability.CONTEXT_REPLY,
            descriptors={
                ChannelCapability.OUTBOUND_TEXT: CapabilityDescriptor(
                    ChannelCapability.OUTBOUND_TEXT, supports_markdown=True
                )
            },
        )
        self.sends: list[tuple[ChannelMessage, str | None]] = []
        self._last_sender: str | None = last_sender

    @property
    def channel_id(self) -> str:
        return self._name

    @property
    def config(self) -> ChannelConfig:
        return self._config

    @property
    def capabilities(self) -> ChannelCapabilitySet:
        return self._caps

    def validate_config(self) -> ValidationResult:
        return ValidationResult.ok_result()

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(healthy=True, channel_id=self._name)

    async def send(self, message, *, target=None, context_token=None) -> ChannelSendResult:
        self.sends.append((message, target))
        return ChannelSendResult.success(self._name, provider_receipt="r")

    def last_known_sender(self) -> str | None:
        return self._last_sender

    def set_inbound_handler(self, handler) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _gateway_with_feishu(
    tmp_path, *, sender: str | None = "ou_feishu_user"
) -> tuple[MessageGateway, _FakeFeishuAdapter]:
    """Build a real gateway with only a fake Feishu adapter (no WeChat)."""
    adapter = _FakeFeishuAdapter("feishu")
    adapter._last_sender = sender
    reg = ChannelAdapterRegistry()
    reg.register(adapter)
    cfg = GatewayConfig(state_dir=str(tmp_path))
    gw = MessageGateway(cfg, registry=reg)
    return gw, adapter


@pytest.mark.asyncio
async def test_notify_broadcasts_to_feishu_when_no_wechat(tmp_path) -> None:
    """REPL connect notification must reach Feishu when no WeChat adapter.

    The wildcard origin ``wechat:direct:*:*`` must not hard-code WeChat-only
    resolution: the notification should broadcast to all connected outbound
    channels, not just WeChat.
    """
    gw, adapter = _gateway_with_feishu(tmp_path)

    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="repl-1", host_type="repl"),
    )
    await asyncio.sleep(0.05)

    texts = [msg.text for msg, _ in adapter.sends]
    assert "orchestratord-REPL已连接" in texts


def test_collect_broadcast_targets_uses_persisted_feishu_sender_after_restart(tmp_path) -> None:
    from orchestratord.im_gateway.gateway import _collect_broadcast_targets

    store = ReliabilityStore(tmp_path)
    store.set_feishu_last_sender("feishu", "oc_chat")
    cfg = GatewayConfig(state_dir=str(tmp_path))
    cfg.replace_channel(
        ChannelConfig(
            type=ChannelType.FEISHU,
            webhook_url="",
            name="feishu",
            enabled=True,
            extra={
                "connection_mode": "websocket",
                "app_id": "cli_app",
                "app_secret": "secret",
            },
        )
    )

    gw = MessageGateway(cfg, store=store)

    assert _collect_broadcast_targets(gw.registry) == [("feishu", "oc_chat")]


def test_collect_broadcast_targets_uses_scanner_before_first_inbound(tmp_path) -> None:
    from orchestratord.im_gateway.gateway import _collect_broadcast_targets

    cfg = GatewayConfig(state_dir=str(tmp_path))
    cfg.replace_channel(
        ChannelConfig(
            type=ChannelType.FEISHU,
            webhook_url="",
            name="feishu",
            enabled=True,
            extra={
                "connection_mode": "websocket",
                "app_id": "cli_app",
                "app_secret": "secret",
                "allowed_user_open_id": "ou_scanner",
            },
        )
    )

    gw = MessageGateway(cfg)

    assert _collect_broadcast_targets(gw.registry) == [("feishu", "ou_scanner")]


@pytest.mark.asyncio
async def test_notify_broadcasts_to_all_connected_channels(tmp_path) -> None:
    """REPL connect notification broadcasts to WeChat AND Feishu."""
    wechat = _FakeWeChatAdapter("wechat")
    wechat._last_sender = "user_wx"
    feishu = _FakeFeishuAdapter("feishu")
    feishu._last_sender = "ou_feishu_user"
    reg = ChannelAdapterRegistry()
    reg.register(wechat)
    reg.register(feishu)
    cfg = GatewayConfig(state_dir=str(tmp_path))
    gw = MessageGateway(cfg, registry=reg)

    gw.binding.bind(
        "wechat:direct:*:*",
        SessionTarget(session_id="repl-1", host_type="repl"),
    )
    await asyncio.sleep(0.05)

    wechat_texts = [msg.text for msg, _ in wechat.sends]
    feishu_texts = [msg.text for msg, _ in feishu.sends]
    assert "orchestratord-REPL已连接" in wechat_texts
    assert "orchestratord-REPL已连接" in feishu_texts


# -- stub agent -----------------------------------------------------------


class _FakeOutAdapter(ChannelAdapter):
    def __init__(self, name: str = "wechat") -> None:
        self._name = name
        self._caps = ChannelCapabilitySet.of(
            ChannelCapability.OUTBOUND_TEXT,
            descriptors={
                ChannelCapability.OUTBOUND_TEXT: CapabilityDescriptor(
                    ChannelCapability.OUTBOUND_TEXT, supports_markdown=False
                )
            },
        )
        self.sends: list[tuple] = []  # (message, target)

    @property
    def channel_id(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ChannelCapabilitySet:
        return self._caps

    def validate_config(self) -> ValidationResult:
        return ValidationResult.ok_result()

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(healthy=True, channel_id=self._name)

    async def send(self, message, *, target=None, context_token=None) -> ChannelSendResult:
        self.sends.append((message, target))
        return ChannelSendResult.success(self._name, provider_receipt="mid_1")


async def _noop_sleep(_delay: float) -> None:
    return None


def _make_outbound(tmp_path, adapter):
    reg = ChannelAdapterRegistry()
    reg.register(adapter)
    store = ReliabilityStore(tmp_path, ReliabilityConfig())
    config = GatewayConfig(state_dir=str(tmp_path), reliability=store._reliability)
    gate = CapabilityGate(reg)
    return OutboundDispatcher(reg, gate, store, config, sleep=_noop_sleep), adapter


@pytest.mark.asyncio
async def test_stub_handler_guides_user_to_bind_repl_or_orchestrator(tmp_path) -> None:
    outbound, adapter = _make_outbound(tmp_path, _FakeOutAdapter("wechat"))
    handler = make_stub_handler(outbound)

    msg = InboundMessage(
        origin="wechat:direct:acct:user_zhao",
        text="你好",
        message_id="m1",
        channel="wechat",
        from_user_id="user_zhao",
    )
    receipt = await handler(msg)

    assert receipt.layer.value == "processed"
    assert len(adapter.sends) == 1
    sent_msg, target = adapter.sends[0]
    assert target == "user_zhao"
    assert "orchestratord server connect-gateway" in sent_msg.text
    assert "你好" not in sent_msg.text


@pytest.mark.asyncio
async def test_stub_handler_skips_reply_when_target_missing(tmp_path) -> None:
    outbound, adapter = _make_outbound(tmp_path, _FakeOutAdapter("wechat"))
    handler = make_stub_handler(outbound)

    msg = InboundMessage(
        origin="wechat:direct:acct:",
        text="hi",
        message_id="m2",
        channel="wechat",
        from_user_id=None,
    )
    receipt = await handler(msg)
    assert receipt.layer.value == "accepted"
    assert adapter.sends == []  # no reply attempted
