"""Feishu App channel adapter tests.

Merged from ClawCodex ``tests/services/channels/test_feishu_app_adapter.py``,
``test_feishu_app_card_actions.py``, ``test_feishu_app_events.py``,
``test_feishu_app_settings.py`` and ``test_feishu_app_shutdown.py``.

The adapter is a thin shell over ``lark_oapi.channel.FeishuChannel``; tests
inject a fake channel implementing the small surface the adapter touches
(``connect_until_ready`` / ``disconnect`` / ``send`` / ``update_card`` /
``on`` / ``bot_identity``). SDK-internal wiring (WS loop, dispatcher, dedup
pipeline) is covered by the SDK's own tests and not re-tested here.

The whole module requires the ``gateway-feishu`` extras; it skips when
``lark_oapi`` is not installed.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("lark_oapi", reason="gateway-feishu extras (lark-oapi) not installed")

from lark_oapi.channel.errors import FeishuChannelErrorCode, SendError
from lark_oapi.channel.types import SendResult

from orchestratord.channels.capabilities import ChannelCapability, ProcessingOutcome
from orchestratord.channels.feishu_app import (
    _FEISHU_PROCESSING_REACTION_CACHE_SIZE,
    FeishuAppChannelAdapter,
)
from orchestratord.channels.models import ChannelConfig, ChannelMessage, ChannelType
from orchestratord.channels.results import ErrorCategory, SendStatus

# ---------------------------------------------------------------------------
# adapter tests (from test_feishu_app_adapter.py)
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(
        self,
        *,
        send_results: list | None = None,
        connect_exc: BaseException | None = None,
        bot_identity: Any | None = None,
    ) -> None:
        self.sent: list[dict] = []
        self.send_options: list[dict | None] = []
        self.updated_cards: list[dict] = []
        self.added_reactions: list[tuple[str, str]] = []
        self.removed_reactions: list[tuple[str, str]] = []
        self.add_reaction_success = True
        self.reaction_id_present = True
        self.delete_reaction_success = True
        self.connect_exc = connect_exc
        self.connected = False
        self.disconnected = False
        self._handlers: dict[str, Any] = {}
        self._bot_identity = bot_identity
        self._send_results = list(send_results or [])

    def on(self, name, handler=None) -> None:
        if isinstance(name, dict):
            self._handlers.update({k: v for k, v in name.items() if v is not None})
            return
        if handler is not None:
            self._handlers[name] = handler

    async def connect_until_ready(self, *, timeout: float | None = None) -> None:
        if self.connect_exc is not None:
            raise self.connect_exc
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send(self, to, message, opts=None) -> SendResult:
        self.sent.append({"to": to, "message": message})
        self.send_options.append(opts)
        if self._send_results:
            result = self._send_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return SendResult.ok(message_id="om_sent")

    async def update_card(self, message_id: str, card: dict) -> SendResult:
        self.updated_cards.append({"message_id": message_id, "card": card})
        return SendResult.ok(message_id=message_id)

    async def add_reaction(self, message_id: str, emoji_type: str) -> SendResult:
        self.added_reactions.append((message_id, emoji_type))
        if not self.add_reaction_success:
            return SendResult.fail(
                _send_error(
                    FeishuChannelErrorCode.UNKNOWN,
                    retryable=False,
                    hint="add rejected",
                )
            )
        return SendResult.ok(
            message_id=message_id,
            raw={
                "code": 0,
                "data": (
                    {"reaction_id": f"r_{len(self.added_reactions)}"}
                    if self.reaction_id_present
                    else {}
                ),
            },
        )

    async def remove_reaction(self, message_id: str, reaction_id: str) -> SendResult:
        self.removed_reactions.append((message_id, reaction_id))
        if self.delete_reaction_success:
            return SendResult.ok(message_id=message_id)
        return SendResult.fail(
            _send_error(
                FeishuChannelErrorCode.UNKNOWN,
                retryable=False,
                hint="delete rejected",
            )
        )

    @property
    def bot_identity(self) -> Any:
        return self._bot_identity

    async def fire_message(self, inbound: Any) -> None:
        await self._handlers["message"](inbound)

    async def fire_card_action(self, payload: Any) -> None:
        await self._handlers["cardAction"](payload)

    async def fire_reconnecting(self) -> None:
        cb = self._handlers.get("reconnecting")
        if cb:
            res = cb()
            if inspect.isawaitable(res):
                await res

    async def fire_reconnected(self) -> None:
        cb = self._handlers.get("reconnected")
        if cb:
            res = cb()
            if inspect.isawaitable(res):
                await res


class _BlockingChannel(_FakeChannel):
    def __init__(self) -> None:
        super().__init__()
        self.release_connect = asyncio.Event()
        self.connect_entered = asyncio.Event()

    async def connect_until_ready(self, *, timeout: float | None = None) -> None:
        self.connect_entered.set()
        await self.release_connect.wait()
        self.connected = True


async def _sleep_forever() -> None:
    await asyncio.sleep(3600)


def _drain_test_loop(loop: asyncio.AbstractEventLoop, tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))


class _SdkLikeWsClient:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._auto_reconnect = True
        self._cache = SimpleNamespace(_cron=loop.create_task(_sleep_forever()))
        self.ping_task = loop.create_task(_sleep_forever())
        self.receive_task = loop.create_task(_sleep_forever())

    @property
    def tasks(self) -> list[asyncio.Task]:
        return [self._cache._cron, self.ping_task, self.receive_task]


class _SdkLikeChannel(_FakeChannel):
    def __init__(self, ws_client: _SdkLikeWsClient) -> None:
        super().__init__()
        self._ws_client = ws_client

    async def disconnect(self) -> None:
        await super().disconnect()
        self._ws_client = None


class _FakeFeishuSenderStore:
    def __init__(self) -> None:
        self.last_senders: dict[str, str] = {}

    def set_feishu_last_sender(self, channel_id: str, sender: str | None) -> None:
        if sender:
            self.last_senders[channel_id] = sender
        else:
            self.last_senders.pop(channel_id, None)

    def get_feishu_last_sender(self, channel_id: str) -> str | None:
        return self.last_senders.get(channel_id)


def _config(extra: dict | None = None) -> ChannelConfig:
    payload = {
        "connection_mode": "websocket",
        "app_id": "cli_app",
        "app_secret": "secret",
        "allowed_user_open_id": "ou_allowed",
        "bot_open_id": "ou_bot",
        "batching": {"text_batch_delay_seconds": 0.01},
        "send": {"sdk_send_attempts": 1, "sdk_send_timeout_seconds": 1.0},
    }
    if extra:
        payload.update(extra)
    return ChannelConfig(
        type=ChannelType.FEISHU,
        webhook_url="",
        name="feishu",
        extra=payload,
    )


def _sdk_inbound(
    *,
    message_id: str = "om_msg_1",
    chat_id: str = "oc_chat",
    chat_type: str = "p2p",
    open_id: str = "ou_allowed",
    text: str = "hello",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        content_text=text,
        create_time=123,
        raw_content_type="text",
        conversation=SimpleNamespace(chat_id=chat_id, chat_type=chat_type),
        sender=SimpleNamespace(open_id=open_id),
    )


def _card_action_event(
    *,
    approval_id: str,
    nonce: str,
    choice: str = "y",
    operator_open_id: str = "ou_allowed",
    chat_id: str = "oc_chat",
    message_id: str = "om_card",
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        chat_id=chat_id,
        operator=SimpleNamespace(open_id=operator_open_id),
        action=SimpleNamespace(
            tag="button",
            value={
                "orchestratord_action": "permission_approval",
                "approval_id": approval_id,
                "nonce": nonce,
                "choice": choice,
            },
        ),
    )


def _send_error(code: FeishuChannelErrorCode, *, retryable: bool, hint: str = "") -> SendError:
    return SendError(code=code, retryable=retryable, hint=hint)


def test_feishu_app_adapter_declares_capabilities() -> None:
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: _FakeChannel())

    assert adapter.capabilities.has(ChannelCapability.OUTBOUND_TEXT)
    assert adapter.capabilities.has(ChannelCapability.INBOUND_POLLING)
    assert adapter.capabilities.has(ChannelCapability.CONTEXT_REPLY)
    assert adapter.capabilities.has(ChannelCapability.LOGIN_MANAGED)
    assert adapter.capabilities.has(ChannelCapability.REACTION)
    assert adapter.capabilities.has(ChannelCapability.PROCESSING_STATUS)


@pytest.mark.asyncio
async def test_feishu_processing_reaction_success_lifecycle() -> None:
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()

    assert await adapter.on_processing_start("om_1") is True
    assert await adapter.on_processing_start("om_1") is True
    assert channel.added_reactions == [("om_1", "Typing")]

    assert await adapter.on_processing_complete("om_1", ProcessingOutcome.SUCCESS) is True
    assert channel.removed_reactions == [("om_1", "r_1")]
    assert channel.added_reactions == [("om_1", "Typing")]


@pytest.mark.asyncio
async def test_feishu_processing_reaction_failure_and_cancelled() -> None:
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()

    await adapter.on_processing_start("om_failed")
    assert await adapter.on_processing_complete("om_failed", ProcessingOutcome.FAILURE) is True
    assert channel.added_reactions[-1] == ("om_failed", "CrossMark")

    await adapter.on_processing_start("om_cancelled")
    assert await adapter.on_processing_complete("om_cancelled", ProcessingOutcome.CANCELLED) is True
    assert ("om_cancelled", "CrossMark") not in channel.added_reactions


@pytest.mark.asyncio
async def test_feishu_processing_delete_failure_does_not_stack_cross_mark() -> None:
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()
    await adapter.on_processing_start("om_1")
    channel.delete_reaction_success = False

    assert await adapter.on_processing_complete("om_1", ProcessingOutcome.FAILURE) is False
    assert channel.added_reactions == [("om_1", "Typing")]
    assert "om_1" in adapter._pending_processing_reactions


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_reaction_id", [False, True])
async def test_feishu_processing_add_failure_does_not_cache_handle(
    missing_reaction_id: bool,
) -> None:
    channel = _FakeChannel()
    channel.add_reaction_success = missing_reaction_id
    channel.reaction_id_present = not missing_reaction_id
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()

    assert await adapter.on_processing_start("") is False
    assert await adapter.on_processing_start("om_1") is False
    assert "om_1" not in adapter._pending_processing_reactions


@pytest.mark.asyncio
async def test_feishu_processing_reactions_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_REACTIONS", "false")
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()

    assert await adapter.on_processing_start("om_1") is True
    assert await adapter.on_processing_complete("om_1", ProcessingOutcome.FAILURE) is True
    assert channel.added_reactions == []


def test_feishu_processing_reaction_cache_is_bounded() -> None:
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: _FakeChannel())
    for index in range(_FEISHU_PROCESSING_REACTION_CACHE_SIZE + 1):
        adapter._remember_reaction(f"om_{index}", f"r_{index}")

    assert len(adapter._pending_processing_reactions) == _FEISHU_PROCESSING_REACTION_CACHE_SIZE
    assert "om_0" not in adapter._pending_processing_reactions


@pytest.mark.asyncio
async def test_terminal_cross_mark_does_not_evict_live_typing_handle() -> None:
    channel = _FakeChannel()
    channel.reaction_id_present = False
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()
    for index in range(_FEISHU_PROCESSING_REACTION_CACHE_SIZE):
        adapter._remember_reaction(f"om_{index}", f"r_{index}")

    assert (
        await adapter.on_processing_complete(
            "om_without_typing",
            ProcessingOutcome.FAILURE,
        )
        is True
    )
    assert len(adapter._pending_processing_reactions) == _FEISHU_PROCESSING_REACTION_CACHE_SIZE
    assert adapter._pending_processing_reactions["om_0"] == "r_0"
    assert "om_without_typing" not in adapter._pending_processing_reactions


@pytest.mark.asyncio
async def test_feishu_app_adapter_start_and_health_with_fake_channel() -> None:
    channel = _FakeChannel(bot_identity=SimpleNamespace(open_id="ou_bot", name="orchestratord"))
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)

    await adapter.start()
    health = await adapter.health_check()

    assert channel.connected is True
    assert channel._handlers["message"] == adapter._on_message
    assert channel._handlers["cardAction"] == adapter._on_card_action
    assert health.healthy is True
    assert health.account_status == "websocket:connected"
    assert health.extra["bot_open_id"] == "ou_bot"


@pytest.mark.asyncio
async def test_feishu_app_adapter_start_blocks_until_channel_ready() -> None:
    channel = _BlockingChannel()
    adapter = FeishuAppChannelAdapter(
        _config({"websocket": {"startup_connect_timeout_seconds": 5}}),
        channel_factory=lambda s: channel,
    )

    start_task = asyncio.create_task(adapter.start())
    await asyncio.wait_for(channel.connect_entered.wait(), timeout=1.0)

    assert channel.connected is False
    assert start_task.done() is False

    channel.release_connect.set()
    await asyncio.wait_for(start_task, timeout=1.0)
    health = await adapter.health_check()

    assert health.account_status == "websocket:connected"
    assert adapter._connect_task is None


@pytest.mark.asyncio
async def test_feishu_app_adapter_initial_retryable_failure_enters_background_retry() -> None:
    failing = _FakeChannel(connect_exc=RuntimeError("network down"))
    channels = [failing, _FakeChannel()]

    def factory(settings):
        return channels.pop(0)

    adapter = FeishuAppChannelAdapter(
        _config({"websocket": {"startup_connect_timeout_seconds": 0.1}}),
        channel_factory=factory,
    )

    await adapter.start()
    health = await adapter.health_check()

    assert health.healthy is False
    assert health.account_status == "websocket:retrying"
    assert "network down" in (health.last_error or "")
    assert adapter._connect_task is not None


@pytest.mark.asyncio
async def test_feishu_app_adapter_background_retry_recovers_to_connected() -> None:
    channels = [_FakeChannel(connect_exc=RuntimeError("network down")), _FakeChannel()]

    def factory(settings):
        return channels.pop(0)

    adapter = FeishuAppChannelAdapter(
        _config({"websocket": {"startup_connect_timeout_seconds": 0.1}}),
        channel_factory=factory,
        retry_sleep=lambda _seconds: asyncio.sleep(0),
    )

    await adapter.start()
    assert adapter._connect_task is not None

    await asyncio.wait_for(adapter._connect_task, timeout=1.0)
    health = await adapter.health_check()

    assert health.healthy is True
    assert health.account_status == "websocket:connected"


@pytest.mark.asyncio
async def test_feishu_app_adapter_missing_credentials_do_not_retry() -> None:
    adapter = FeishuAppChannelAdapter(
        _config({"app_id": "", "app_secret": ""}),
        channel_factory=lambda s: _FakeChannel(),
    )

    await adapter.start()
    health = await adapter.health_check()

    assert health.account_status == "credentials_missing"
    assert adapter._connect_task is None


@pytest.mark.asyncio
async def test_feishu_app_adapter_stop_disconnects_channel() -> None:
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()

    await adapter.stop()

    assert channel.disconnected is True
    health = await adapter.health_check()
    assert health.account_status == "websocket:disconnected"


@pytest.mark.asyncio
async def test_feishu_app_adapter_stop_drains_sdk_ws_loop_tasks() -> None:
    sdk_loop = asyncio.new_event_loop()
    ws_client = _SdkLikeWsClient(sdk_loop)
    channel = _SdkLikeChannel(ws_client)
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()

    try:
        await adapter.stop()

        assert channel.disconnected is True
        assert ws_client._auto_reconnect is False
        assert all(task.done() for task in ws_client.tasks)
    finally:
        await asyncio.to_thread(_drain_test_loop, sdk_loop, ws_client.tasks)
        sdk_loop.close()


@pytest.mark.asyncio
async def test_feishu_adapter_inbound_translates_and_delivers() -> None:
    delivered = []
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    adapter.set_inbound_handler(delivered.append)
    await adapter.start()

    await channel.fire_message(_sdk_inbound())

    assert len(delivered) == 1
    assert delivered[0].text == "hello"
    assert delivered[0].context_token == "oc_chat"
    assert delivered[0].from_user_id == "ou_allowed"


@pytest.mark.asyncio
async def test_feishu_adapter_inbound_drops_non_p2p() -> None:
    delivered = []
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    adapter.set_inbound_handler(delivered.append)
    await adapter.start()

    await channel.fire_message(_sdk_inbound(chat_type="group"))

    assert delivered == []


@pytest.mark.asyncio
async def test_feishu_adapter_inbound_drops_disallowed_sender_without_side_effects() -> None:
    """Sender auth group 2 (adapter level): an unauthorized sender is dropped
    before any last_known_sender / sender-store side effect."""
    store = _FakeFeishuSenderStore()
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(
        _config(), channel_factory=lambda s: channel, sender_store=store
    )
    delivered = []
    adapter.set_inbound_handler(delivered.append)
    await adapter.start()

    await channel.fire_message(_sdk_inbound(open_id="ou_other"))

    assert delivered == []
    assert adapter._last_sender is None
    assert store.last_senders == {}


@pytest.mark.asyncio
async def test_feishu_adapter_inbound_empty_allowlist_drops_all() -> None:
    """Sender auth group 3 (adapter level): an empty allowlist rejects all."""
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(
        _config({"allowed_user_open_id": ""}), channel_factory=lambda s: channel
    )
    delivered = []
    adapter.set_inbound_handler(delivered.append)
    await adapter.start()

    await channel.fire_message(_sdk_inbound(open_id="ou_allowed"))
    await channel.fire_message(_sdk_inbound(open_id="ou_anyone"))

    assert delivered == []
    assert adapter._last_sender is None


def test_feishu_adapter_authorized_recipients() -> None:
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: _FakeChannel())
    assert adapter.authorized_recipients() == ["ou_allowed"]

    empty = FeishuAppChannelAdapter(
        _config({"allowed_user_open_id": ""}), channel_factory=lambda s: _FakeChannel()
    )
    assert empty.authorized_recipients() == []


@pytest.mark.asyncio
async def test_feishu_adapter_tracks_last_known_sender_from_inbound_chat() -> None:
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()

    await channel.fire_message(_sdk_inbound())

    assert adapter.last_known_sender() == "oc_chat"


def test_feishu_adapter_last_sender_falls_back_to_configured_user() -> None:
    adapter = FeishuAppChannelAdapter(
        _config({"allowed_user_open_id": "ou_scanner"}),
        channel_factory=lambda s: _FakeChannel(),
    )

    assert adapter.last_known_sender() == "ou_scanner"


@pytest.mark.asyncio
async def test_feishu_adapter_sends_to_scanner_before_first_inbound() -> None:
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(
        _config({"allowed_user_open_id": "ou_scanner"}),
        channel_factory=lambda s: channel,
    )
    await adapter.start()

    result = await adapter.send(
        ChannelMessage(text="orchestrator connected"),
        target=adapter.last_known_sender(),
    )

    assert result.ok is True
    assert channel.sent == [{"to": "ou_scanner", "message": {"text": "orchestrator connected"}}]
    assert channel.send_options == [{"receive_id_type": "open_id"}]


def test_pinned_sdk_infers_scanner_id_as_open_id() -> None:
    # Contract check against the locked lark-oapi 1.7.0 implementation:
    # even if the explicit adapter option is removed accidentally, the SDK
    # still recognizes the scanner identifier as an open_id.
    from lark_oapi.channel.outbound.routing import infer_receive_id_type

    assert infer_receive_id_type("ou_scanner") == "open_id"


@pytest.mark.asyncio
async def test_feishu_adapter_last_sender_survives_restart_via_store() -> None:
    store = _FakeFeishuSenderStore()
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(
        _config(), channel_factory=lambda s: channel, sender_store=store
    )
    await adapter.start()

    await channel.fire_message(_sdk_inbound(message_id="om_persisted"))

    restarted = FeishuAppChannelAdapter(
        _config(), channel_factory=lambda s: _FakeChannel(), sender_store=store
    )

    assert adapter.last_known_sender() == "oc_chat"
    assert restarted.last_known_sender() == "oc_chat"


@pytest.mark.asyncio
async def test_feishu_send_uses_context_token_chat_id() -> None:
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()

    result = await adapter.send(ChannelMessage(text="hello"), context_token="oc_chat")

    assert result.ok is True
    assert result.provider_receipt == "om_sent"
    assert channel.sent == [{"to": "oc_chat", "message": {"text": "hello"}}]


@pytest.mark.asyncio
async def test_feishu_send_prefers_context_chat_id_when_target_is_origin_open_id() -> None:
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()

    result = await adapter.send(
        ChannelMessage(text="hello", metadata={"origin": "feishu:dm:cli_app:ou_allowed"}),
        target="ou_allowed",
        context_token="oc_chat",
    )

    assert result.ok is True
    assert channel.sent[0]["to"] == "oc_chat"


@pytest.mark.asyncio
async def test_feishu_send_returns_retryable_on_rate_limit() -> None:
    channel = _FakeChannel(
        send_results=[
            SendResult.fail(
                _send_error(
                    FeishuChannelErrorCode.RATE_LIMITED, retryable=True, hint="rate limited"
                )
            )
        ]
    )
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()

    result = await adapter.send(ChannelMessage(text="hello"), target="oc_chat")

    assert result.ok is False
    assert result.status is SendStatus.RETRYABLE_ERROR
    assert result.error_category is ErrorCategory.RATE_LIMIT
    assert result.retryable is True


@pytest.mark.asyncio
async def test_feishu_send_returns_nonretryable_on_bad_request() -> None:
    channel = _FakeChannel(
        send_results=[
            SendResult.fail(
                _send_error(
                    FeishuChannelErrorCode.FORMAT_ERROR, retryable=False, hint="bad payload"
                )
            )
        ]
    )
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()

    result = await adapter.send(ChannelMessage(text="hello"), target="oc_chat")

    assert result.ok is False
    assert result.status is SendStatus.NONRETRYABLE_ERROR
    assert result.error_category is ErrorCategory.CLIENT_ERROR


@pytest.mark.asyncio
async def test_feishu_send_falls_back_to_text_on_format_error() -> None:
    channel = _FakeChannel(
        send_results=[
            SendResult.fail(
                _send_error(
                    FeishuChannelErrorCode.FORMAT_ERROR, retryable=False, hint="post rejected"
                )
            ),
            SendResult.ok(message_id="om_sent"),
        ]
    )
    cfg = _config({"send": {"sdk_send_attempts": 1, "sdk_send_timeout_seconds": 1.0}})
    adapter = FeishuAppChannelAdapter(cfg, channel_factory=lambda s: channel)
    await adapter.start()

    result = await adapter.send(
        ChannelMessage(text="```code\nx\n```", markdown=True), target="oc_chat"
    )

    assert result.ok is True
    assert channel.sent[0]["message"] == {"markdown": "```code\nx\n```"}
    assert channel.sent[1]["message"] == {"text": "```code\nx\n```"}


@pytest.mark.asyncio
async def test_feishu_permission_metadata_sends_interactive_card() -> None:
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()
    message = ChannelMessage(
        text="fallback",
        metadata={
            "intent": "permission_approval",
            "permission": {
                "message": "orchestratord wants to use Bash.",
                "suggestion": "Review command",
                "options": [
                    {"value": "y", "label": "允许"},
                    {"value": "n", "label": "拒绝"},
                ],
                "expires_in_seconds": 600,
            },
        },
    )

    result = await adapter.send(message, context_token="oc_chat")

    assert result.ok is True
    card = channel.sent[0]["message"]["card"]
    assert card["header"]["title"]["content"] == "权限审批"


@pytest.mark.asyncio
async def test_feishu_card_click_emits_approval_inbound_and_updates_card() -> None:
    channel = _FakeChannel()
    delivered = []
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    adapter.set_inbound_handler(delivered.append)
    await adapter.start()
    message = ChannelMessage(
        text="fallback",
        metadata={
            "intent": "permission_approval",
            "permission": {
                "message": "orchestratord wants to use Bash.",
                "options": [{"value": "y", "label": "允许"}],
            },
        },
    )
    await adapter.send(message, context_token="oc_chat")
    pending = next(iter(adapter.approval_manager.pending.values()))

    await channel.fire_card_action(
        _card_action_event(approval_id=pending.approval_id, nonce=pending.nonce, choice="y")
    )

    assert len(delivered) == 1
    assert delivered[0].text == "y"
    assert delivered[0].semantic_tags == ["approval"]
    assert len(channel.updated_cards) == 1
    resolved = channel.updated_cards[0]["card"]
    assert resolved["header"]["template"] == "green"
    assert all(element.get("tag") != "action" for element in resolved["elements"])


@pytest.mark.asyncio
async def test_feishu_card_click_invalid_payload_does_nothing() -> None:
    channel = _FakeChannel()
    delivered = []
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    adapter.set_inbound_handler(delivered.append)
    await adapter.start()

    await channel.fire_card_action(_card_action_event(approval_id="unknown", nonce="x"))

    assert delivered == []
    assert channel.updated_cards == []


@pytest.mark.asyncio
async def test_feishu_send_does_not_hang_forever_when_sdk_hangs() -> None:
    class _HangingChannel(_FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.send_started = asyncio.Event()

        async def send(self, to, message, opts=None) -> SendResult:
            self.send_started.set()
            await asyncio.Event().wait()  # never returns
            return SendResult.ok()  # pragma: no cover

    channel = _HangingChannel()
    cfg = _config({"send": {"sdk_send_attempts": 1, "sdk_send_timeout_seconds": 0.5}})
    adapter = FeishuAppChannelAdapter(cfg, channel_factory=lambda s: channel)
    await adapter.start()

    task = asyncio.create_task(adapter.send(ChannelMessage(text="first"), target="oc_chat"))
    await asyncio.wait_for(channel.send_started.wait(), timeout=1.0)

    try:
        result = await asyncio.wait_for(task, timeout=3.0)
    except TimeoutError:
        pytest.fail("adapter.send hung forever when channel.send never returned")

    assert result.ok is False
    assert result.error_category is ErrorCategory.TIMEOUT


@pytest.mark.asyncio
async def test_feishu_health_reflects_reconnecting_reconnected_hooks() -> None:
    channel = _FakeChannel()
    adapter = FeishuAppChannelAdapter(_config(), channel_factory=lambda s: channel)
    await adapter.start()

    await channel.fire_reconnecting()
    assert (await adapter.health_check()).account_status == "websocket:reconnecting"

    await channel.fire_reconnected()
    health = await adapter.health_check()
    assert health.account_status == "websocket:connected"
    assert health.healthy is True


# ---------------------------------------------------------------------------
# card-action timing tests (from test_feishu_app_card_actions.py)
# ---------------------------------------------------------------------------


class _CardFakeChannel:
    def __init__(self) -> None:
        self.updated_cards: list[dict] = []

    async def update_card(self, message_id: str, card: dict) -> None:
        self.updated_cards.append({"message_id": message_id, "card": card})


def _card_config() -> ChannelConfig:
    return ChannelConfig(
        type=ChannelType.FEISHU,
        webhook_url="",
        name="feishu",
        extra={
            "connection_mode": "websocket",
            "app_id": "cli_app",
            "app_secret": "secret",
            "allowed_user_open_id": "ou_allowed",
        },
    )


def _simple_card_action_event(*, approval_id: str, nonce: str, choice: str = "y") -> SimpleNamespace:
    return SimpleNamespace(
        message_id="om_card",
        chat_id="oc_chat",
        operator=SimpleNamespace(open_id="ou_allowed"),
        action=SimpleNamespace(
            tag="button",
            value={
                "orchestratord_action": "permission_approval",
                "approval_id": approval_id,
                "nonce": nonce,
                "choice": choice,
            },
        ),
    )


def test_feishu_adapter_exposes_public_inbound_activity_context() -> None:
    adapter = FeishuAppChannelAdapter(
        _card_config(), channel_factory=lambda _settings: _CardFakeChannel()
    )

    assert adapter.last_inbound_context() is None
    adapter._remember_inbound(SimpleNamespace(message_id="om_activity", chat_id="oc_chat"))

    context = adapter.last_inbound_context()
    assert context is not None
    assert context.message_id == "om_activity"
    assert context.chat_id == "oc_chat"


@pytest.mark.asyncio
async def test_feishu_card_click_updates_card_before_slow_gateway_handler() -> None:
    channel = _CardFakeChannel()
    started = asyncio.Event()
    release = asyncio.Event()
    delivered = []
    adapter = FeishuAppChannelAdapter(
        _card_config(), channel_factory=lambda _settings: channel
    )
    adapter._channel = channel
    adapter._main_loop = asyncio.get_running_loop()

    async def _slow_handler(message) -> None:
        delivered.append(message)
        started.set()
        await release.wait()

    adapter.set_inbound_handler(_slow_handler)
    pending = adapter.approval_manager.create_pending(
        origin="feishu:dm:cli_app:ou_allowed",
        chat_id="oc_chat",
        allowed_user_open_id="ou_allowed",
        choices={"y"},
        ttl_seconds=60,
    )

    task = asyncio.create_task(
        adapter._on_card_action(
            _simple_card_action_event(approval_id=pending.approval_id, nonce=pending.nonce)
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    try:
        assert channel.updated_cards, "card should be resolved before gateway delivery finishes"
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_feishu_session_approval_click_updates_card_as_allowed() -> None:
    channel = _CardFakeChannel()
    delivered = []
    adapter = FeishuAppChannelAdapter(
        _card_config(), channel_factory=lambda _settings: channel
    )
    adapter._channel = channel
    adapter._main_loop = asyncio.get_running_loop()
    adapter.set_inbound_handler(lambda message: delivered.append(message))
    pending = adapter.approval_manager.create_pending(
        origin="feishu:dm:cli_app:ou_allowed",
        chat_id="oc_chat",
        allowed_user_open_id="ou_allowed",
        choices={"y", "s", "n"},
        allow_choices={"y", "s"},
        ttl_seconds=60,
    )

    await adapter._on_card_action(
        _simple_card_action_event(
            approval_id=pending.approval_id,
            nonce=pending.nonce,
            choice="s",
        )
    )

    assert len(delivered) == 1
    assert delivered[0].text == "s"
    assert channel.updated_cards[0]["card"]["header"]["template"] == "green"
    assert "已允许" in channel.updated_cards[0]["card"]["header"]["title"]["content"]


# ---------------------------------------------------------------------------
# SDK→gateway inbound translation (from test_feishu_app_events.py)
# ---------------------------------------------------------------------------

from orchestratord.channels.feishu_events import translate_inbound
from orchestratord.channels.feishu_settings import FeishuAppSettings


def _events_settings() -> FeishuAppSettings:
    return FeishuAppSettings(
        channel_id="feishu",
        connection_mode="websocket",
        app_id="cli_app",
        app_secret="secret",
        allowed_user_open_id="ou_allowed",
        bot_open_id="ou_bot",
    )


def test_feishu_translate_inbound_maps_sdk_inbound_to_gateway_inbound() -> None:
    inbound = translate_inbound(_sdk_inbound(), _events_settings())

    assert inbound is not None
    assert inbound.origin == "feishu:dm:cli_app:ou_allowed"
    assert inbound.channel == "feishu"
    assert inbound.message_id == "om_msg_1"
    assert inbound.text == "hello"
    assert inbound.context_token == "oc_chat"
    assert inbound.from_user_id == "ou_allowed"
    assert inbound.raw["chat_id"] == "oc_chat"
    assert inbound.raw["create_time"] == 123


def test_feishu_translate_inbound_drops_non_p2p() -> None:
    assert translate_inbound(_sdk_inbound(chat_type="group"), _events_settings()) is None


def test_feishu_translate_inbound_drops_disallowed_sender() -> None:
    """Sender auth group 2: a sender outside the allowlist is dropped."""
    assert translate_inbound(_sdk_inbound(open_id="ou_other"), _events_settings()) is None


def test_feishu_translate_inbound_drops_self_echo() -> None:
    assert translate_inbound(_sdk_inbound(open_id="ou_bot"), _events_settings()) is None


def test_feishu_translate_inbound_drops_empty_text() -> None:
    assert translate_inbound(_sdk_inbound(text="   "), _events_settings()) is None


def test_feishu_translate_inbound_empty_allowlist_fails_closed() -> None:
    """Sender auth group 3: an empty allowlist rejects every p2p sender."""
    settings = FeishuAppSettings(
        channel_id="feishu",
        connection_mode="websocket",
        app_id="cli_app",
        app_secret="secret",
        allowed_user_open_id="",
        bot_open_id="ou_bot",
    )
    assert translate_inbound(_sdk_inbound(open_id="ou_anyone"), settings) is None
    assert translate_inbound(_sdk_inbound(open_id="ou_allowed"), settings) is None


def test_feishu_translate_inbound_empty_allowlist_warns_once(caplog) -> None:
    import logging

    from orchestratord.channels import feishu_events

    settings = FeishuAppSettings(
        channel_id="feishu",
        connection_mode="websocket",
        app_id="cli_app",
        app_secret="secret",
        allowed_user_open_id="",
        bot_open_id="ou_bot",
    )
    feishu_events._empty_allowlist_warned = False
    caplog.set_level(logging.WARNING, logger="orchestratord.channels.feishu_events")
    try:
        translate_inbound(_sdk_inbound(open_id="ou_anyone"), settings)
        translate_inbound(_sdk_inbound(open_id="ou_other"), settings)
        warnings = [
            record for record in caplog.records if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "allowed_user_open_id" in warnings[0].getMessage()
    finally:
        feishu_events._empty_allowlist_warned = False


# ---------------------------------------------------------------------------
# settings parsing (from test_feishu_app_settings.py)
# ---------------------------------------------------------------------------


def _websocket_config(extra: dict | None = None) -> ChannelConfig:
    payload = {
        "connection_mode": "websocket",
        "app_id": "cli_app",
        "app_secret": "cli_secret",
        "domain": "feishu",
        "allowed_user_open_id": "ou_allowed",
        "bot_open_id": "ou_bot",
    }
    if extra:
        payload.update(extra)
    return ChannelConfig(
        type=ChannelType.FEISHU,
        webhook_url="",
        name="feishu",
        extra=payload,
    )


def test_feishu_settings_reads_extra_and_env() -> None:
    cfg = _websocket_config(
        {
            "app_id": "${FEISHU_APP_ID}",
            "app_secret": "${FEISHU_APP_SECRET}",
            "encrypt_key": "${FEISHU_ENCRYPT_KEY}",
            "verification_token": "${FEISHU_VERIFICATION_TOKEN}",
            "allowed_user_open_id": "${FEISHU_ALLOWED_USER_OPEN_ID}",
        }
    )

    settings = FeishuAppSettings.from_config(
        cfg,
        environ={
            "FEISHU_APP_ID": "cli_env_app",
            "FEISHU_APP_SECRET": "env_secret",
            "FEISHU_ENCRYPT_KEY": "env_encrypt_key",
            "FEISHU_VERIFICATION_TOKEN": "env_verification_token",
            "FEISHU_ALLOWED_USER_OPEN_ID": "ou_env_user",
        },
    )

    assert settings.connection_mode == "websocket"
    assert settings.app_id == "cli_env_app"
    assert settings.app_secret == "env_secret"
    assert settings.encrypt_key == "env_encrypt_key"
    assert settings.verification_token == "env_verification_token"
    assert settings.allowed_user_open_id == "ou_env_user"
    assert settings.domain == "feishu"


def test_feishu_settings_ignores_sdk_owned_ws_tuning_fields() -> None:
    cfg = _websocket_config(
        {
            "websocket": {
                "ws_reconnect_interval": "180",
                "ws_ping_interval": "",
                "ws_ping_timeout": "12.5",
            },
            "batching": {
                "text_batch_delay_seconds": "0.2",
                "text_batch_max_messages": "3",
                "text_batch_max_chars": "1200",
            },
            "send": {
                "sdk_send_attempts": "2",
                "sdk_send_backoff_base_seconds": "0.1",
                "per_origin_serial": False,
            },
        }
    )

    settings = FeishuAppSettings.from_config(cfg)

    # WS tuning is server-authoritative in the SDK; none of it is surfaced.
    assert not hasattr(settings, "connect_attempts")
    assert not hasattr(settings, "ws_reconnect_interval")
    assert not hasattr(settings, "ws_ping_interval")
    assert not hasattr(settings, "ws_ping_timeout")
    assert not hasattr(settings, "per_origin_serial")
    assert settings.text_batch_delay_seconds == 0.2
    assert settings.text_batch_max_messages == 3
    assert settings.text_batch_max_chars == 1200
    assert settings.sdk_send_attempts == 2
    assert settings.sdk_send_backoff_base_seconds == 0.1


def test_feishu_settings_reads_startup_connect_timeout() -> None:
    cfg = _websocket_config(
        {
            "websocket": {
                "startup_connect_timeout_seconds": "5.5",
            },
        }
    )

    settings = FeishuAppSettings.from_config(cfg)

    assert settings.startup_connect_timeout_seconds == 5.5


def test_feishu_reactions_env_overrides_channel_config() -> None:
    cfg = _websocket_config({"reactions": False})

    assert FeishuAppSettings.from_config(cfg, environ={}).reactions_enabled is False
    assert (
        FeishuAppSettings.from_config(
            cfg,
            environ={"FEISHU_REACTIONS": "true"},
        ).reactions_enabled
        is True
    )


def test_feishu_settings_rejects_missing_credentials_in_websocket_mode() -> None:
    cfg = _websocket_config({"app_id": "", "app_secret": ""})

    settings = FeishuAppSettings.from_config(cfg)
    errors = settings.validation_errors()

    assert "app_id is required for feishu websocket mode" in errors
    assert "app_secret is required for feishu websocket mode" in errors


# ---------------------------------------------------------------------------
# WS-loop shutdown (from test_feishu_app_shutdown.py)
# ---------------------------------------------------------------------------

from orchestratord.channels.feishu_app import _cancel_feishu_sdk_ws_tasks


class _SdkLikeExpiringCache:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._cron = loop.create_task(self._start_clear_cron())

    async def _start_clear_cron(self) -> None:
        await asyncio.sleep(3600)


class _ShutdownSdkLikeWsClient:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._close_seen = loop.create_future()
        self._cache = _SdkLikeExpiringCache(loop)
        self.ping_task = loop.create_task(self._ping_loop())
        self.receive_task = loop.create_task(self._receive_message_loop())

    @property
    def tasks(self) -> list[asyncio.Task]:
        return [self._cache._cron, self.ping_task, self.receive_task]

    async def _ping_loop(self) -> None:
        await asyncio.sleep(3600)

    async def _receive_message_loop(self) -> None:
        await self._close_seen
        raise RuntimeError("sent 1000 (OK); no close frame received")

    def close_from_disconnect(self) -> None:
        if not self._close_seen.done():
            self._close_seen.set_result(None)
        if not self.receive_task.done():
            self._close_seen.get_loop().run_until_complete(asyncio.sleep(0))


@pytest.mark.asyncio
async def test_cancel_feishu_sdk_ws_tasks_prevents_receive_close_exception() -> None:
    sdk_loop = asyncio.new_event_loop()
    ws_client = _ShutdownSdkLikeWsClient(sdk_loop)

    try:
        await _cancel_feishu_sdk_ws_tasks(sdk_loop)
        ws_client.close_from_disconnect()

        assert ws_client.receive_task.cancelled() is True
        assert all(task.done() for task in ws_client.tasks)
    finally:
        await asyncio.to_thread(_drain_and_close_loop, sdk_loop, ws_client.tasks)


def _drain_and_close_loop(loop: asyncio.AbstractEventLoop, tasks: list[asyncio.Task]) -> None:
    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    loop.close()
