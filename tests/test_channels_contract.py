"""Channel contract layer tests: models, capabilities, results, retry,
null channel, manager dispatch, and the adapter registry.

Migrated from the upstream project's channels test suite; the registry
section is adjusted to register a ``NullChannel`` factory explicitly
instead of relying on concrete webhook channel modules (Feishu/Slack/
Discord adapters are migrated in a later phase, and the default registry
ships without pre-registered types).
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from typing import Any

import pytest

from orchestratord.channels import (
    BaseChannel,
    CapabilityDescriptor,
    CapabilityNotDeclaredError,
    CardUpdateCapability,
    ChannelAdapter,
    ChannelCapability,
    ChannelCapabilitySet,
    ChannelConfig,
    ChannelDisabledError,
    ChannelManager,
    ChannelMessage,
    ChannelNotFoundError,
    ChannelTransport,
    ChannelType,
    InvalidWebhookURLError,
    MessageLevel,
    NullChannel,
    OutboundCapability,
    build_default_registry,
)
from orchestratord.channels.base import default_headers
from orchestratord.channels.null_channel import _NullTransport
from orchestratord.channels.registry import (
    ChannelAdapterRegistry,
    WebhookChannelAdapter,
    _webhook_factory,
)
from orchestratord.channels.results import (
    ChannelHealth,
    ChannelSendResult,
    ErrorCategory,
    SendStatus,
    ValidationResult,
)
from orchestratord.channels.retry import (
    DEFAULT_RETRY_POLICY,
    RetryPolicy,
    classify_exception,
    classify_http_status,
    compute_backoff,
)
from orchestratord.channels.transport import (
    DEFAULT_TIMEOUT_SECONDS,
    TransportError,
    TransportResponse,
    encode_json_body,
)

# A stable, globally-routable public address (Google DNS). Keeps channel
# construction tests deterministic and DNS-independent.
_FAKE_PUBLIC_IP = "8.8.8.8"


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``socket.getaddrinfo`` at a stable public address.

    Channel construction validates webhook URLs, which resolves the
    hostname; tests that assert specific resolution behavior override
    this fixture with their own ``socket.getaddrinfo`` patch.
    """

    def _fake_getaddrinfo(
        host: str,
        port: int | str | None,
        *args: Any,
        **kwargs: Any,
    ) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_FAKE_PUBLIC_IP, port or 443)),
            (socket.AF_INET, socket.SOCK_DGRAM, 17, "", (_FAKE_PUBLIC_IP, port or 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cfg(name: str = "ops", ctype: ChannelType = ChannelType.SLACK) -> ChannelConfig:
    return ChannelConfig(
        type=ctype,
        webhook_url="https://hooks.example.com/services/T0/B0/abcdef0123456789",
        name=name,
    )


class _RecordingTransport(ChannelTransport):
    def __init__(self, status: int = 200, body: bytes = b"") -> None:
        self.status = status
        self.body = body
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    async def post(
        self,
        url: str,
        body: bytes,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> TransportResponse:
        with self._lock:
            self.calls.append(
                {"url": url, "body": body, "headers": dict(headers or {}), "timeout": timeout}
            )
        return TransportResponse(status=self.status, body=self.body, headers={})


class _StubChannel(BaseChannel):
    """A channel that records every send and posts through the transport."""

    def __init__(
        self,
        config: ChannelConfig,
        transport: _RecordingTransport,
        *,
        should_raise: BaseException | None = None,
    ) -> None:
        super().__init__(config, transport=transport)
        self._should_raise = should_raise
        self.received: list[ChannelMessage] = []

    def format_message(self, message: ChannelMessage) -> tuple[bytes, dict[str, str]]:
        return encode_json_body({"text": message.text}), default_headers()

    async def send(self, message: ChannelMessage) -> bool:
        self.received.append(message)
        if self._should_raise is not None:
            raise self._should_raise
        body, headers = self.format_message(message)
        await self._transport.post(
            self._config.webhook_url, body, headers=headers, timeout=DEFAULT_TIMEOUT_SECONDS
        )
        return self._transport.status == 200


class _RaisingChannel(BaseChannel):
    """A channel whose ``send`` always raises the configured exception."""

    def __init__(self, config: ChannelConfig, exc: BaseException) -> None:
        super().__init__(config)
        self._exc = exc

    def format_message(self, message: ChannelMessage) -> tuple[bytes, dict[str, str]]:
        return encode_json_body({"text": message.text}), default_headers()

    async def send(self, message: ChannelMessage) -> bool:
        raise self._exc


class _FailingChannel(BaseChannel):
    """A channel whose ``send`` always returns ``False`` (business rejection)."""

    def format_message(self, message: ChannelMessage) -> tuple[bytes, dict[str, str]]:
        return encode_json_body({"text": message.text}), default_headers()

    async def send(self, message: ChannelMessage) -> bool:
        return False


class _SimpleChannel(BaseChannel):
    """A minimal BaseChannel that exercises URL validation on construction."""

    def format_message(self, message: ChannelMessage) -> tuple[bytes, dict[str, str]]:
        return encode_json_body({"text": message.text}), default_headers()

    async def send(self, message: ChannelMessage) -> bool:
        return True


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


def test_channel_message_defaults() -> None:
    msg = ChannelMessage(text="hello")
    assert msg.text == "hello"
    assert msg.level is MessageLevel.INFO
    assert msg.title is None
    assert msg.markdown is True
    assert msg.attachments is None
    assert msg.metadata is None


def test_channel_message_rejects_empty_text() -> None:
    with pytest.raises(ValueError):
        ChannelMessage(text="")


def test_channel_message_rejects_non_string_text() -> None:
    with pytest.raises(TypeError):
        ChannelMessage(text=123)  # type: ignore[arg-type]


def test_channel_message_rejects_oversized_text() -> None:
    with pytest.raises(ValueError):
        ChannelMessage(text="x" * 30_001)


def test_channel_message_rejects_oversized_title() -> None:
    with pytest.raises(ValueError):
        ChannelMessage(text="ok", title="t" * 201)


def test_channel_message_rejects_non_list_attachments() -> None:
    with pytest.raises(TypeError):
        ChannelMessage(text="ok", attachments="not-a-list")  # type: ignore[arg-type]


def test_channel_message_rejects_non_dict_metadata() -> None:
    with pytest.raises(TypeError):
        ChannelMessage(text="ok", metadata=[1, 2, 3])  # type: ignore[arg-type]


def test_channel_message_round_trip() -> None:
    msg = ChannelMessage(
        text="hello world",
        level=MessageLevel.WARN,
        title="alert",
        markdown=False,
        attachments=[{"color": "red"}],
        metadata={"trace_id": "abc"},
    )
    data = msg.to_dict()
    assert data == {
        "text": "hello world",
        "level": "warn",
        "title": "alert",
        "markdown": False,
        "attachments": [{"color": "red"}],
        "metadata": {"trace_id": "abc"},
    }
    assert ChannelMessage.from_dict(data) == msg


def test_channel_message_from_dict_defaults_level() -> None:
    msg = ChannelMessage.from_dict({"text": "hi"})
    assert msg.level is MessageLevel.INFO
    assert msg.markdown is True


def test_channel_message_from_dict_rejects_non_dict() -> None:
    with pytest.raises(TypeError):
        ChannelMessage.from_dict("not-a-dict")  # type: ignore[arg-type]


def test_channel_message_from_dict_rejects_bad_level() -> None:
    # ``"nope"`` passes the isinstance(str) check and fails in MessageLevel()
    # with the enum's ValueError.
    with pytest.raises(ValueError):
        ChannelMessage.from_dict({"text": "x", "level": "nope"})


def test_channel_config_defaults() -> None:
    cfg = ChannelConfig(
        type=ChannelType.SLACK,
        webhook_url="https://hooks.example.com/services/T0000/B0000/abcdef0123456789",
        name="alerts",
    )
    assert cfg.enabled is True
    assert cfg.extra is None


def test_channel_config_rejects_invalid_name() -> None:
    with pytest.raises(ValueError):
        ChannelConfig(
            type=ChannelType.SLACK,
            webhook_url="https://hooks.example.com/x",
            name="has spaces",
        )
    with pytest.raises(ValueError):
        ChannelConfig(
            type=ChannelType.SLACK,
            webhook_url="https://hooks.example.com/x",
            name="",
        )
    with pytest.raises(ValueError):
        ChannelConfig(
            type=ChannelType.SLACK,
            webhook_url="https://hooks.example.com/x",
            name="a" * 65,
        )


def test_channel_config_rejects_non_string_url() -> None:
    with pytest.raises(ValueError):
        ChannelConfig(
            type=ChannelType.SLACK,
            webhook_url="",  # type: ignore[arg-type]
            name="x",
        )
    with pytest.raises(TypeError):
        ChannelConfig(
            type=ChannelType.SLACK,
            webhook_url=123,  # type: ignore[arg-type]
            name="x",
        )


def test_channel_config_allows_empty_url_for_non_webhook_channel() -> None:
    cfg = ChannelConfig(
        type=ChannelType.FEISHU,
        webhook_url="",
        name="feishu",
        extra={
            "connection_mode": "websocket",
            "app_id": "cli_a",
            "app_secret": "secret",
            "allowed_user_open_id": "ou_user",
        },
    )

    assert cfg.webhook_url == ""
    assert cfg.to_dict()["webhook_url"] == ""


def test_channel_config_rejects_non_dict_extra() -> None:
    with pytest.raises(TypeError):
        ChannelConfig(
            type=ChannelType.SLACK,
            webhook_url="https://hooks.example.com/x",
            name="x",
            extra=[1, 2],  # type: ignore[arg-type]
        )


def test_channel_config_rejects_wrong_type_enum() -> None:
    with pytest.raises(TypeError):
        ChannelConfig(
            type="slack",  # type: ignore[arg-type]
            webhook_url="https://hooks.example.com/x",
            name="x",
        )


def test_channel_config_round_trip() -> None:
    cfg = ChannelConfig(
        type=ChannelType.DISCORD,
        webhook_url="https://discord.com/api/webhooks/1/abcdef0123456789",
        name="bot-1",
        enabled=False,
        extra={"thread_id": "42"},
    )
    data = cfg.to_dict()
    assert data == {
        "type": "discord",
        "webhook_url": "https://discord.com/api/webhooks/1/abcdef0123456789",
        "name": "bot-1",
        "enabled": False,
        "extra": {"thread_id": "42"},
    }
    assert ChannelConfig.from_dict(data) == cfg


def test_channel_config_from_dict_rejects_non_dict() -> None:
    with pytest.raises(TypeError):
        ChannelConfig.from_dict(None)  # type: ignore[arg-type]


def test_channel_type_enum_values() -> None:
    assert ChannelType.FEISHU.value == "feishu"
    assert ChannelType.SLACK.value == "slack"
    assert ChannelType.DISCORD.value == "discord"
    assert ChannelType.WECHAT.value == "wechat"
    assert ChannelType.MCP_PUSH.value == "mcp_push"


def test_message_level_enum_values() -> None:
    assert MessageLevel.INFO.value == "info"
    assert MessageLevel.WARN.value == "warn"
    assert MessageLevel.ERROR.value == "error"
    assert MessageLevel.SUCCESS.value == "success"


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


def test_capability_enum_values() -> None:
    assert ChannelCapability.OUTBOUND_TEXT.value == "outbound_text"
    assert ChannelCapability.INBOUND_POLLING.value == "inbound_polling"
    assert ChannelCapability.INBOUND_WEBHOOK.value == "inbound_webhook"
    assert ChannelCapability.CONTEXT_REPLY.value == "context_reply"
    assert ChannelCapability.LOGIN_MANAGED.value == "login_managed"


def test_capability_set_of_and_has() -> None:
    caps = ChannelCapabilitySet.of(
        ChannelCapability.OUTBOUND_TEXT,
        ChannelCapability.CONTEXT_REPLY,
    )
    assert caps.has(ChannelCapability.OUTBOUND_TEXT)
    assert caps.has(ChannelCapability.CONTEXT_REPLY)
    assert not caps.has(ChannelCapability.INBOUND_POLLING)


def test_capability_set_descriptor_lookup() -> None:
    desc = CapabilityDescriptor(
        ChannelCapability.OUTBOUND_TEXT,
        supports_markdown=False,
        max_text_length=4000,
    )
    caps = ChannelCapabilitySet.of(
        ChannelCapability.OUTBOUND_TEXT,
        descriptors={ChannelCapability.OUTBOUND_TEXT: desc},
    )
    got = caps.descriptor(ChannelCapability.OUTBOUND_TEXT)
    assert got is not None
    assert got.supports_markdown is False
    assert got.max_text_length == 4000
    assert caps.descriptor(ChannelCapability.INBOUND_POLLING) is None


def test_capability_set_rejects_orphan_descriptor() -> None:
    desc = CapabilityDescriptor(ChannelCapability.INBOUND_POLLING)
    with pytest.raises(ValueError):
        ChannelCapabilitySet.of(
            ChannelCapability.OUTBOUND_TEXT,
            descriptors={ChannelCapability.INBOUND_POLLING: desc},
        )


class _FakeAdapter(ChannelAdapter):
    def __init__(self, caps: ChannelCapabilitySet) -> None:
        self._caps = caps

    @property
    def channel_id(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> ChannelCapabilitySet:
        return self._caps

    def validate_config(self) -> ValidationResult:
        return ValidationResult.ok_result()

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(healthy=True, channel_id="fake")


def test_channel_adapter_require_capability_passes_when_declared() -> None:
    adapter = _FakeAdapter(ChannelCapabilitySet.of(ChannelCapability.OUTBOUND_TEXT))
    adapter.require_capability(ChannelCapability.OUTBOUND_TEXT)  # no raise


def test_channel_adapter_require_capability_fail_closed() -> None:
    adapter = _FakeAdapter(ChannelCapabilitySet.of(ChannelCapability.OUTBOUND_TEXT))
    with pytest.raises(CapabilityNotDeclaredError):
        adapter.require_capability(ChannelCapability.MEDIA_IMAGE)


def test_outbound_capability_is_structural_protocol() -> None:
    class _Out:
        channel_id = "x"

        async def send(self, message, *, target=None, context_token=None): ...

    assert isinstance(_Out(), OutboundCapability)


def test_card_update_capability_is_structural_protocol() -> None:
    class _Cards:
        channel_id = "cards"
        capabilities = ChannelCapabilitySet.of(ChannelCapability.CARD_UPDATE)

        def last_inbound_context(self):
            return None

        async def send_placeholder_card(self, chat_id, card):
            return "om_placeholder"

        async def update_progress_card(self, message_id, card):
            return True

    assert isinstance(_Cards(), CardUpdateCapability)


def test_channel_adapter_default_retry_policy() -> None:
    adapter = _FakeAdapter(ChannelCapabilitySet.of(ChannelCapability.OUTBOUND_TEXT))
    assert adapter.retry_policy.max_attempts >= 1


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


def test_send_result_success_factory() -> None:
    r = ChannelSendResult.success("wechat-main", provider_receipt="mid_123")
    assert r.ok is True
    assert r.status is SendStatus.SUCCESS
    assert r.retryable is False
    assert r.provider_receipt == "mid_123"
    assert r.error_category is ErrorCategory.NONE


def test_send_result_retryable_error_requires_retryable_category() -> None:
    r = ChannelSendResult.retryable_error(
        "wechat-main", message="boom", category=ErrorCategory.SERVER_ERROR
    )
    assert r.ok is False
    assert r.retryable is True
    assert r.status is SendStatus.RETRYABLE_ERROR

    with pytest.raises(ValueError):
        ChannelSendResult.retryable_error("wechat-main", message="x", category=ErrorCategory.AUTH)


def test_send_result_nonretryable_error() -> None:
    r = ChannelSendResult.nonretryable_error(
        "wechat-main", message="nope", category=ErrorCategory.AUTH
    )
    assert r.ok is False
    assert r.retryable is False
    assert r.status is SendStatus.NONRETRYABLE_ERROR


def test_send_result_rate_limited_is_visible_but_not_dispatcher_retryable() -> None:
    r = ChannelSendResult.rate_limited(
        "wechat-main", message="rate limited", raw={"retry_after_seconds": 10}
    )
    assert r.ok is False
    assert r.retryable is False
    assert r.status is SendStatus.RATE_LIMITED
    assert r.error_category is ErrorCategory.RATE_LIMIT
    assert r.raw == {"retry_after_seconds": 10}


def test_send_result_unsupported() -> None:
    r = ChannelSendResult.unsupported("wechat-main", message="no media")
    assert r.ok is False
    assert r.status is SendStatus.UNSUPPORTED
    assert r.retryable is False


def test_send_result_retryable_property_matches_category() -> None:
    assert ChannelSendResult(
        ok=False,
        status=SendStatus.RETRYABLE_ERROR,
        channel_id="c",
        error_category=ErrorCategory.RATE_LIMIT,
    ).retryable
    assert not ChannelSendResult(
        ok=False,
        status=SendStatus.NONRETRYABLE_ERROR,
        channel_id="c",
        error_category=ErrorCategory.FORMAT,
    ).retryable
    # ok result is never retryable even if category somehow retryable
    assert not ChannelSendResult(
        ok=True,
        status=SendStatus.SUCCESS,
        channel_id="c",
    ).retryable


def test_send_result_rejects_bad_types() -> None:
    with pytest.raises(TypeError):
        ChannelSendResult(ok=True, status="success", channel_id="c")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ChannelSendResult(ok=True, status=SendStatus.SUCCESS, channel_id="c", attempts=0)


def test_send_result_to_dict_roundtrip() -> None:
    r = ChannelSendResult.success("c", provider_receipt="r", raw={"k": 1})
    d = r.to_dict()
    assert d["ok"] is True
    assert d["status"] == "success"
    assert d["provider_receipt"] == "r"
    assert d["raw"] == {"k": 1}


def test_channel_health_to_dict() -> None:
    h = ChannelHealth(
        healthy=True,
        channel_id="wechat-main",
        circuit_state="closed",
        consecutive_failures=2,
    )
    d = h.to_dict()
    assert d["healthy"] is True
    assert d["circuit_state"] == "closed"
    assert d["consecutive_failures"] == 2
    assert d["extra"] == {}


def test_validation_result_ok_and_fail() -> None:
    assert ValidationResult.ok_result().ok is True
    fail = ValidationResult.fail(["a", "b"])
    assert fail.ok is False
    assert fail.errors == ["a", "b"]
    assert ValidationResult.fail("one").errors == ["one"]


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


def test_classify_http_status() -> None:
    assert classify_http_status(200) is ErrorCategory.NONE
    assert classify_http_status(204) is ErrorCategory.NONE
    assert classify_http_status(429) is ErrorCategory.RATE_LIMIT
    assert classify_http_status(401) is ErrorCategory.AUTH
    assert classify_http_status(403) is ErrorCategory.AUTH
    assert classify_http_status(404) is ErrorCategory.NOT_FOUND
    assert classify_http_status(400) is ErrorCategory.CLIENT_ERROR
    assert classify_http_status(422) is ErrorCategory.CLIENT_ERROR
    assert classify_http_status(500) is ErrorCategory.SERVER_ERROR
    assert classify_http_status(503) is ErrorCategory.SERVER_ERROR
    assert classify_http_status(700) is ErrorCategory.UNKNOWN


def test_classify_exception() -> None:
    assert classify_exception(TimeoutError()) is ErrorCategory.TIMEOUT
    assert classify_exception(TransportError("network down")) is ErrorCategory.NETWORK
    assert classify_exception(TransportError("transport timeout: x")) is ErrorCategory.TIMEOUT
    assert classify_exception(ConnectionError("x")) is ErrorCategory.NETWORK
    assert classify_exception(ValueError("bad")) is ErrorCategory.FORMAT
    assert classify_exception(RuntimeError("??")) is ErrorCategory.UNKNOWN


def test_retry_policy_validation() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_seconds=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_seconds=10, max_seconds=5)
    with pytest.raises(ValueError):
        RetryPolicy(jitter=1.5)


def test_retry_policy_is_retryable() -> None:
    p = DEFAULT_RETRY_POLICY
    assert p.is_retryable(ErrorCategory.SERVER_ERROR)
    assert p.is_retryable(ErrorCategory.RATE_LIMIT)
    assert not p.is_retryable(ErrorCategory.AUTH)
    assert not p.is_retryable(ErrorCategory.FORMAT)


def test_compute_backoff_monotonic_and_capped_without_jitter() -> None:
    policy = RetryPolicy(max_attempts=6, base_seconds=1.0, max_seconds=10.0, jitter=0.0)
    vals = [compute_backoff(a, policy) for a in range(1, 7)]
    # exponential: 1, 2, 4, 8, 10 (capped), 10 (capped)
    assert vals[0] == pytest.approx(1.0)
    assert vals[1] == pytest.approx(2.0)
    assert vals[2] == pytest.approx(4.0)
    assert vals[3] == pytest.approx(8.0)
    assert vals[4] == pytest.approx(10.0)
    assert vals[5] == pytest.approx(10.0)


def test_compute_backoff_jitter_within_bounds() -> None:
    class _Rng:
        def uniform(self, a: float, b: float) -> float:
            return (a + b) / 2  # midpoint

    policy = RetryPolicy(base_seconds=2.0, max_seconds=60.0, jitter=0.25)
    # attempt 1: base=2, delta=0.5, midpoint => 2.0
    assert compute_backoff(1, policy, rng=_Rng()) == pytest.approx(2.0)
    # attempt 3: base=8, delta=2, midpoint => 8.0
    assert compute_backoff(3, policy, rng=_Rng()) == pytest.approx(8.0)


def test_compute_backoff_rejects_bad_attempt() -> None:
    with pytest.raises(ValueError):
        compute_backoff(0, DEFAULT_RETRY_POLICY)


# ---------------------------------------------------------------------------
# null channel
# ---------------------------------------------------------------------------


def _null_config() -> ChannelConfig:
    # Loopback hostname is not validated by NullChannel by design.
    return ChannelConfig(
        type=ChannelType.SLACK,
        webhook_url="https://localhost/hook/abcdef0123456789",
        name="null-1",
    )


def test_null_channel_constructs_with_loopback_url() -> None:
    # If NullChannel ran the URL safety check, this would raise — it must not.
    channel = NullChannel(_null_config())
    assert channel.name == "null-1"
    assert channel.enabled is True


@pytest.mark.asyncio
async def test_null_channel_send_records_payload() -> None:
    channel = NullChannel(_null_config())
    msg = ChannelMessage(text="hi", level=MessageLevel.WARN)
    ok = await channel.send(msg)
    assert ok is True

    log = channel.log
    assert len(log) == 1
    entry = log[0]
    assert entry.message is msg
    # Body is JSON; verify it round-trips and includes the level.
    payload = json.loads(entry.body.decode("utf-8"))
    assert payload["text"] == "hi"
    assert payload["level"] == "warn"
    assert entry.headers.get("Content-Type") == "application/json"


@pytest.mark.asyncio
async def test_null_channel_clear_empties_log() -> None:
    channel = NullChannel(_null_config())
    await channel.send(ChannelMessage(text="x"))
    assert len(channel.log) == 1
    channel.clear()
    assert channel.log == []


@pytest.mark.asyncio
async def test_null_channel_send_is_thread_safe() -> None:
    channel = NullChannel(_null_config())
    n = 50

    async def fire(i: int) -> None:
        await channel.send(ChannelMessage(text=f"m{i}"))

    await asyncio.gather(*(fire(i) for i in range(n)))
    assert len(channel.log) == n


def test_null_channel_does_not_touch_transport_calls() -> None:
    # The internal _NullTransport records network calls; default channel
    # should be using it, not the urllib transport.
    channel = NullChannel(_null_config())
    assert isinstance(channel.transport, _NullTransport)
    assert channel.transport.calls == []


# ---------------------------------------------------------------------------
# ChannelManager
# ---------------------------------------------------------------------------


def _make_config(name: str, url: str = "https://hooks.example.com/x") -> ChannelConfig:
    return ChannelConfig(type=ChannelType.SLACK, webhook_url=url, name=name)


@pytest.mark.asyncio
async def test_manager_register_and_lookup() -> None:
    transport = _RecordingTransport()
    channel = _StubChannel(_make_config("a"), transport)
    manager = ChannelManager()
    manager.register(channel)
    assert manager.names() == ["a"]
    assert manager.get("a") is channel
    assert manager.get("missing") is None


def test_manager_unregister_is_silent_on_missing() -> None:
    manager = ChannelManager()
    manager.unregister("nope")  # must not raise


@pytest.mark.asyncio
async def test_manager_send_to_dispatches_to_named_channel() -> None:
    transport = _RecordingTransport()
    channel = _StubChannel(_make_config("a"), transport)
    manager = ChannelManager()
    manager.register(channel)
    ok = await manager.send_to("a", ChannelMessage(text="hi"))
    assert ok is True
    assert len(transport.calls) == 1
    assert channel.received[0].text == "hi"


@pytest.mark.asyncio
async def test_manager_send_to_raises_when_missing() -> None:
    manager = ChannelManager()
    with pytest.raises(ChannelNotFoundError):
        await manager.send_to("nope", ChannelMessage(text="hi"))


@pytest.mark.asyncio
async def test_manager_send_to_raises_when_disabled() -> None:
    transport = _RecordingTransport()
    cfg = _make_config("a")
    cfg.enabled = False
    channel = _StubChannel(cfg, transport)
    manager = ChannelManager()
    manager.register(channel)
    with pytest.raises(ChannelDisabledError):
        await manager.send_to("a", ChannelMessage(text="hi"))
    assert transport.calls == []


@pytest.mark.asyncio
async def test_broadcast_dispatches_to_all_channels() -> None:
    transport_a = _RecordingTransport()
    transport_b = _RecordingTransport()
    channel_a = _StubChannel(_make_config("a"), transport_a)
    channel_b = _StubChannel(_make_config("b"), transport_b)
    manager = ChannelManager()
    manager.register(channel_a)
    manager.register(channel_b)
    results = await manager.broadcast(ChannelMessage(text="hi"))
    assert results == {"a": True, "b": True}
    assert len(transport_a.calls) == 1
    assert len(transport_b.calls) == 1


@pytest.mark.asyncio
async def test_broadcast_continues_on_per_channel_failure() -> None:
    transport_a = _RecordingTransport()
    transport_b = _RecordingTransport()
    transport_c = _RecordingTransport()
    channel_a = _StubChannel(_make_config("a"), transport_a)
    channel_b = _StubChannel(
        _make_config("b"),
        transport_b,
        should_raise=RuntimeError("boom"),
    )
    channel_c = _StubChannel(_make_config("c"), transport_c)
    manager = ChannelManager()
    manager.register(channel_a)
    manager.register(channel_b)
    manager.register(channel_c)
    results = await manager.broadcast(ChannelMessage(text="hi"))
    # a and c succeed, b is recorded as failure but doesn't crash the call.
    assert results == {"a": True, "b": False, "c": True}
    # The other channels must still receive the call.
    assert len(transport_a.calls) == 1
    assert len(transport_c.calls) == 1


@pytest.mark.asyncio
async def test_broadcast_empty_manager_returns_empty_dict() -> None:
    manager = ChannelManager()
    results = await manager.broadcast(ChannelMessage(text="hi"))
    assert results == {}


@pytest.mark.asyncio
async def test_broadcast_runs_channels_in_parallel() -> None:
    # Three channels that each sleep briefly; broadcast should not be serial.
    delays = [0.1, 0.1, 0.1]

    class _SlowChannel(BaseChannel):
        def __init__(self, name: str, delay: float) -> None:
            super().__init__(_make_config(name, "https://hooks.example.com/x"))
            self.delay = delay
            self.calls = 0

        def format_message(self, message: ChannelMessage) -> tuple[bytes, dict[str, str]]:
            return encode_json_body({"text": message.text}), default_headers()

        async def send(self, message: ChannelMessage) -> bool:
            await asyncio.sleep(self.delay)
            self.calls += 1
            return True

    manager = ChannelManager()
    channels = [_SlowChannel(f"c{i}", d) for i, d in enumerate(delays)]
    for ch in channels:
        manager.register(ch)

    start = time.monotonic()
    results = await manager.broadcast(ChannelMessage(text="hi"))
    elapsed = time.monotonic() - start
    assert results == {f"c{i}": True for i in range(len(delays))}
    # Serial execution would take ~0.3s; parallel should be much closer to
    # 0.1s. Use a generous upper bound to avoid flakiness on slow CI.
    assert elapsed < sum(delays) * 0.6


def test_manager_register_is_thread_safe() -> None:
    manager = ChannelManager()
    transport = _RecordingTransport()
    n = 100

    def register_one(i: int) -> None:
        ch = _StubChannel(_make_config(f"c{i}"), transport)
        manager.register(ch)

    threads = [threading.Thread(target=register_one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # All 100 distinct names must be present after concurrent registration.
    assert set(manager.names()) == {f"c{i}" for i in range(n)}
    assert len(manager.names()) == n


@pytest.mark.asyncio
async def test_manager_send_to_returns_false_on_http_error() -> None:
    transport = _RecordingTransport(status=500)
    channel = _StubChannel(_make_config("a"), transport)
    manager = ChannelManager()
    manager.register(channel)
    ok = await manager.send_to("a", ChannelMessage(text="hi"))
    assert ok is False


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def _null_registry() -> ChannelAdapterRegistry:
    reg = build_default_registry()
    reg.register_type(ChannelType.SLACK, _webhook_factory(NullChannel))
    reg.register_type(ChannelType.DISCORD, _webhook_factory(NullChannel))
    reg.register_type(ChannelType.FEISHU, _webhook_factory(NullChannel))
    return reg


def test_default_registry_registers_webhook_types_in_base_environment(monkeypatch) -> None:
    """Phase 2: feishu (webhook mode) / slack / discord factories are
    pre-registered, and building the registry must not import the
    heavy-adapter modules (feishu_app / wechat_ilink), so a base
    environment without the gateway extras still works."""
    import importlib
    import sys

    import orchestratord  # noqa: F401 — must stay importable in a base env

    # Even with the extras modules unimportable, the default registry must
    # build and list the webhook types.
    monkeypatch.setitem(sys.modules, "orchestratord.channels.feishu_app", None)
    monkeypatch.setitem(sys.modules, "orchestratord.channels.wechat_ilink", None)

    reg = build_default_registry()
    assert set(reg.list_types()) == {"feishu", "slack", "discord"}
    # ... and the heavy modules were never pulled in by registry construction.
    assert "orchestratord.channels.feishu_app" not in sys.modules or (
        sys.modules["orchestratord.channels.feishu_app"] is None
    )
    assert "orchestratord.channels.wechat_ilink" not in sys.modules or (
        sys.modules["orchestratord.channels.wechat_ilink"] is None
    )
    importlib.import_module("orchestratord")


def test_registry_lists_registered_types() -> None:
    reg = _null_registry()
    assert set(reg.list_types()) == {"feishu", "slack", "discord"}


def test_registry_create_and_get_webhook_adapter() -> None:
    reg = _null_registry()
    adapter = reg.create(_cfg("alerts", ChannelType.SLACK))
    assert adapter.channel_id == "alerts"
    assert reg.get("alerts") is adapter
    assert "alerts" in reg.names()
    # webhook channels declare outbound_text only
    assert adapter.capabilities.has(ChannelCapability.OUTBOUND_TEXT)
    assert not adapter.capabilities.has(ChannelCapability.INBOUND_POLLING)


def test_registry_create_unknown_type_raises() -> None:
    reg = build_default_registry()
    with pytest.raises(KeyError):
        reg.create(_cfg("x", ChannelType.WECHAT))


def test_registry_require_capability_fail_closed() -> None:
    reg = _null_registry()
    reg.create(_cfg("alerts", ChannelType.SLACK))
    reg.require_capability("alerts", ChannelCapability.OUTBOUND_TEXT)
    with pytest.raises(CapabilityNotDeclaredError):
        reg.require_capability("alerts", ChannelCapability.MEDIA_IMAGE)
    with pytest.raises(CapabilityNotDeclaredError):
        reg.require_capability("alerts", ChannelCapability.INBOUND_POLLING)


def test_registry_inbound_adapters_empty_for_webhook_only() -> None:
    reg = _null_registry()
    reg.create(_cfg("a", ChannelType.SLACK))
    reg.create(_cfg("b", ChannelType.DISCORD))
    assert reg.inbound_adapters() == []


def test_webhook_adapter_validate_config_ok() -> None:
    reg = _null_registry()
    adapter = reg.create(_cfg("alerts", ChannelType.SLACK))
    result = adapter.validate_config()
    assert result.ok is True


def test_webhook_adapter_validate_config_rejects_bad_url() -> None:
    cfg = ChannelConfig(
        type=ChannelType.SLACK,
        webhook_url="not-a-url",
        name="bad",
    )
    reg = build_default_registry()
    reg.register_type(ChannelType.SLACK, _webhook_factory(_SimpleChannel))
    # BaseChannel construction validates the URL and raises before the adapter
    # is even built; the registry surfaces that as an InvalidWebhookURLError.
    with pytest.raises(InvalidWebhookURLError):
        reg.create(cfg)


@pytest.mark.asyncio
async def test_webhook_adapter_send_success_returns_result() -> None:
    base = NullChannel(_cfg("feishu1", ChannelType.FEISHU))
    adapter = WebhookChannelAdapter(_cfg("feishu1", ChannelType.FEISHU), base)
    result = await adapter.send(ChannelMessage(text="hello"))
    assert result.ok is True
    assert result.status is SendStatus.SUCCESS
    assert result.channel_id == "feishu1"


@pytest.mark.asyncio
async def test_webhook_adapter_send_transport_error_is_retryable() -> None:
    base = _RaisingChannel(_cfg("feishu1", ChannelType.FEISHU), TransportError("network down"))
    adapter = WebhookChannelAdapter(_cfg("feishu1", ChannelType.FEISHU), base)
    result = await adapter.send(ChannelMessage(text="hello"))
    assert result.ok is False
    assert result.retryable is True
    assert result.error_category is ErrorCategory.NETWORK


@pytest.mark.asyncio
async def test_webhook_adapter_send_timeout_is_retryable() -> None:
    base = _RaisingChannel(
        _cfg("feishu1", ChannelType.FEISHU), TransportError("transport timeout: slow")
    )
    adapter = WebhookChannelAdapter(_cfg("feishu1", ChannelType.FEISHU), base)
    result = await adapter.send(ChannelMessage(text="hello"))
    assert result.retryable is True
    assert result.error_category is ErrorCategory.TIMEOUT


@pytest.mark.asyncio
async def test_webhook_adapter_send_false_is_nonretryable() -> None:
    base = _FailingChannel(_cfg("feishu1", ChannelType.FEISHU))
    adapter = WebhookChannelAdapter(_cfg("feishu1", ChannelType.FEISHU), base)
    result = await adapter.send(ChannelMessage(text="hello"))
    assert result.ok is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_webhook_adapter_health_check() -> None:
    base = NullChannel(_cfg("s1", ChannelType.SLACK))
    adapter = WebhookChannelAdapter(_cfg("s1", ChannelType.SLACK), base)
    health = await adapter.health_check()
    assert health.healthy is True
    assert health.channel_id == "s1"
    assert health.circuit_state == "closed"


def test_registry_remove() -> None:
    reg = _null_registry()
    reg.create(_cfg("alerts", ChannelType.SLACK))
    assert reg.remove("alerts") is True
    assert reg.get("alerts") is None
    assert reg.remove("alerts") is False
