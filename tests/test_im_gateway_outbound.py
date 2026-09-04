"""Tests for the outbound dispatcher (fail-closed, markdown fallback, outbox, dead-letter).

Also covers the outbound text helpers (strip/split/truncate).
Migrated from ClawCodex ``test_outbound.py`` + ``test_text.py``.
"""

from __future__ import annotations

import pytest

from orchestratord.channels.capabilities import (
    CapabilityDescriptor,
    ChannelAdapter,
    ChannelCapability,
    ChannelCapabilitySet,
)
from orchestratord.channels.models import ChannelMessage
from orchestratord.channels.registry import ChannelAdapterRegistry
from orchestratord.channels.results import (
    ChannelHealth,
    ChannelSendResult,
    ErrorCategory,
    SendStatus,
    ValidationResult,
)
from orchestratord.im_gateway.capability_gate import CapabilityGate
from orchestratord.im_gateway.config import GatewayConfig, ReliabilityConfig
from orchestratord.im_gateway.outbound import OutboundDispatcher
from orchestratord.im_gateway.store import ReliabilityStore
from orchestratord.im_gateway.text import (
    DEFAULT_MAX_CHUNKS,
    maybe_truncate_with_liveview,
    split_text,
    strip_markdown,
)
from orchestratord.ipc.models import OutboundMessage


class _FakeOutAdapter(ChannelAdapter):
    def __init__(
        self,
        name: str = "fake",
        *,
        supports_markdown: bool = False,
        send_result: ChannelSendResult | None = None,
        results_seq: list[ChannelSendResult] | None = None,
        caps: ChannelCapabilitySet | None = None,
    ) -> None:
        self._name = name
        if caps is not None:
            self._caps = caps
        else:
            self._caps = ChannelCapabilitySet.of(
                ChannelCapability.OUTBOUND_TEXT,
                descriptors={
                    ChannelCapability.OUTBOUND_TEXT: CapabilityDescriptor(
                        ChannelCapability.OUTBOUND_TEXT,
                        supports_markdown=supports_markdown,
                    )
                },
            )
        self._send_result = send_result
        self._results_seq = list(results_seq) if results_seq else None
        self.sends: list[ChannelMessage] = []

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
        if self._results_seq is not None:
            idx = len(self.sends) - 1
            if idx < len(self._results_seq):
                return self._results_seq[idx]
            return self._results_seq[-1]
        if self._send_result is not None:
            return self._send_result
        return ChannelSendResult.success(self._name, provider_receipt=f"mid_{len(self.sends)}")


async def _noop_sleep(_delay: float) -> None:
    return None


def _make_dispatcher(
    tmp_path, adapter: _FakeOutAdapter
) -> tuple[OutboundDispatcher, ReliabilityStore, ChannelAdapterRegistry]:
    reg = ChannelAdapterRegistry()
    reg.register(adapter)
    store = ReliabilityStore(
        tmp_path, ReliabilityConfig(markdown_fallback=True, long_message_threshold_chunks=4)
    )
    config = GatewayConfig(state_dir=str(tmp_path), reliability=store._reliability)
    gate = CapabilityGate(reg)
    return OutboundDispatcher(reg, gate, store, config, sleep=_noop_sleep), store, reg


@pytest.mark.asyncio
async def test_send_success_records_outbox_and_receipt(tmp_path) -> None:
    adapter = _FakeOutAdapter("wechat-main", supports_markdown=False)
    disp, store, _ = _make_dispatcher(tmp_path, adapter)
    result = await disp.send(OutboundMessage(text="hello", channel="wechat-main"))
    assert result.ok is True
    assert result.provider_receipt is not None
    entries = store.outbox_entries()
    statuses = [e["status"] for e in entries]
    assert "pending" in statuses and "delivered" in statuses
    delivered = [e for e in entries if e["status"] == "delivered"]
    assert delivered[-1]["provider_receipt"] == result.provider_receipt


@pytest.mark.asyncio
async def test_send_fail_closed_when_no_outbound_capability(tmp_path) -> None:
    caps = ChannelCapabilitySet.of(ChannelCapability.MEDIA_IMAGE)
    adapter = _FakeOutAdapter("media-only", caps=caps)
    disp, store, _ = _make_dispatcher(tmp_path, adapter)
    result = await disp.send(OutboundMessage(text="hi", channel="media-only"))
    assert result.ok is False
    assert result.status is SendStatus.UNSUPPORTED
    assert any(e["status"] == "dead" for e in store.outbox_entries())


@pytest.mark.asyncio
async def test_send_strips_markdown_for_non_markdown_channel(tmp_path) -> None:
    adapter = _FakeOutAdapter("wechat-main", supports_markdown=False)
    disp, _, _ = _make_dispatcher(tmp_path, adapter)
    await disp.send(
        OutboundMessage(text="**bold** and `code`", channel="wechat-main", markdown=True)
    )
    assert len(adapter.sends) == 1
    sent = adapter.sends[0]
    assert "**" not in sent.text
    assert "`" not in sent.text
    assert sent.markdown is False  # stripped -> markdown False


@pytest.mark.asyncio
async def test_send_keeps_markdown_for_markdown_channel(tmp_path) -> None:
    adapter = _FakeOutAdapter("slack-ops", supports_markdown=True)
    disp, _, _ = _make_dispatcher(tmp_path, adapter)
    await disp.send(OutboundMessage(text="**bold**", channel="slack-ops", markdown=True))
    assert adapter.sends[0].text == "**bold**"
    assert adapter.sends[0].markdown is True


@pytest.mark.asyncio
async def test_send_nonretryable_failure_goes_to_dead_letter(tmp_path) -> None:
    adapter = _FakeOutAdapter(
        "wechat-main",
        supports_markdown=False,
        send_result=ChannelSendResult.nonretryable_error(
            "wechat-main", message="bad", category=ErrorCategory.AUTH
        ),
    )
    disp, store, _ = _make_dispatcher(tmp_path, adapter)
    result = await disp.send(OutboundMessage(text="hi", channel="wechat-main"))
    assert result.ok is False
    assert len(store.dead_letter_entries()) == 1
    assert store.dead_letter_entries()[0]["error_category"] == "auth"


@pytest.mark.asyncio
async def test_send_retryable_exhausts_then_dead_letters(tmp_path) -> None:
    adapter = _FakeOutAdapter(
        "wechat-main",
        send_result=ChannelSendResult.retryable_error(
            "wechat-main", message="5xx", category=ErrorCategory.SERVER_ERROR
        ),
    )
    disp, store, _ = _make_dispatcher(tmp_path, adapter)
    result = await disp.send(OutboundMessage(text="hi", channel="wechat-main"))
    assert result.ok is False
    # default policy max_attempts=5 → 5 send calls then dead-letter
    assert len(adapter.sends) == 5
    assert len(store.dead_letter_entries()) == 1
    # retry_pending outbox entries recorded across attempts
    statuses = [e["status"] for e in store.outbox_entries()]
    assert "retry_pending" in statuses
    assert store.outbox_pending() == []


@pytest.mark.asyncio
async def test_wechat_rate_limit_is_reported_without_hidden_deferred_success(tmp_path) -> None:
    adapter = _FakeOutAdapter(
        "wechat",
        send_result=ChannelSendResult.rate_limited(
            "wechat",
            message="rate limited",
            raw={"retry_after_seconds": 10},
        ),
    )
    disp, store, _ = _make_dispatcher(tmp_path, adapter)

    result = await disp.send(OutboundMessage(text="hi", channel="wechat", target="u1"))

    assert result.ok is False
    assert result.error_category is ErrorCategory.RATE_LIMIT
    assert result.status is SendStatus.RATE_LIMITED
    assert len(adapter.sends) == 1
    assert store.dead_letter_entries() == []
    assert disp.deferred_outbound_count() == 0
    assert not any(e["status"] == "deferred" for e in store.outbox_entries())
    assert any(e["status"] == "failed" for e in store.outbox_entries())


@pytest.mark.asyncio
async def test_rate_limited_status_is_reported_without_channel_specific_retry(tmp_path) -> None:
    adapter = _FakeOutAdapter(
        "line-direct",
        send_result=ChannelSendResult(
            ok=False,
            status=SendStatus.RATE_LIMITED,
            channel_id="line-direct",
            error_category=ErrorCategory.RATE_LIMIT,
            message="platform rate limited",
            raw={"retry_after_seconds": 10},
        ),
    )
    disp, store, _ = _make_dispatcher(tmp_path, adapter)

    result = await disp.send(OutboundMessage(text="hi", channel="line-direct", target="u1"))

    assert result.ok is False
    assert result.status is SendStatus.RATE_LIMITED
    assert result.error_category is ErrorCategory.RATE_LIMIT
    assert len(adapter.sends) == 1
    assert store.dead_letter_entries() == []
    assert disp.deferred_outbound_count() == 0
    assert not any(e["status"] == "deferred" for e in store.outbox_entries())
    assert any(e["status"] == "failed" for e in store.outbox_entries())


@pytest.mark.asyncio
async def test_wechat_rate_limit_without_retry_after_still_does_not_retry(tmp_path) -> None:
    adapter = _FakeOutAdapter(
        "wechat",
        send_result=ChannelSendResult.rate_limited(
            "wechat",
            message="rate limited",
        ),
    )
    disp, _, _ = _make_dispatcher(tmp_path, adapter)

    result = await disp.send(OutboundMessage(text="hi", channel="wechat", target="u1"))

    assert result.ok is False
    assert result.status is SendStatus.RATE_LIMITED
    assert len(adapter.sends) == 1
    assert disp.deferred_outbound_count() == 0


@pytest.mark.asyncio
async def test_send_retryable_then_success_recovers(tmp_path) -> None:
    adapter = _FakeOutAdapter(
        "wechat-main",
        results_seq=[
            ChannelSendResult.retryable_error(
                "wechat-main", message="5xx", category=ErrorCategory.SERVER_ERROR
            ),
            ChannelSendResult.retryable_error(
                "wechat-main", message="5xx", category=ErrorCategory.SERVER_ERROR
            ),
            ChannelSendResult.success("wechat-main", provider_receipt="ok_mid"),
        ],
    )
    disp, store, _ = _make_dispatcher(tmp_path, adapter)
    result = await disp.send(OutboundMessage(text="hi", channel="wechat-main"))
    assert result.ok is True
    assert result.provider_receipt == "ok_mid"
    assert len(adapter.sends) == 3
    assert store.dead_letter_entries() == []


@pytest.mark.asyncio
async def test_broadcast_partial_failure_does_not_crash(tmp_path) -> None:
    a_ok = _FakeOutAdapter("a")
    a_bad = _FakeOutAdapter(
        "b",
        send_result=ChannelSendResult.nonretryable_error(
            "b", message="x", category=ErrorCategory.AUTH
        ),
    )
    reg = ChannelAdapterRegistry()
    reg.register(a_ok)
    reg.register(a_bad)
    store = ReliabilityStore(tmp_path)
    config = GatewayConfig(state_dir=str(tmp_path), reliability=ReliabilityConfig())
    disp = OutboundDispatcher(reg, CapabilityGate(reg), store, config)
    results = await disp.broadcast(OutboundMessage(text="hi", channel="a"))
    assert results["a"].ok is True
    assert results["b"].ok is False


@pytest.mark.asyncio
async def test_send_unknown_channel_returns_not_found(tmp_path) -> None:
    adapter = _FakeOutAdapter("a")
    disp, _store, _ = _make_dispatcher(tmp_path, adapter)
    result = await disp.send(OutboundMessage(text="hi", channel="missing"))
    assert result.ok is False
    assert result.error_category is ErrorCategory.NOT_FOUND


@pytest.mark.asyncio
async def test_send_long_message_is_truncated_with_notice(tmp_path) -> None:
    adapter = _FakeOutAdapter("wechat-main", supports_markdown=False)
    disp, _, _ = _make_dispatcher(tmp_path, adapter)
    long_text = "\n".join(["word" * 200] * 40)  # far beyond 4 chunks of 4000 chars

    result = await disp.send(OutboundMessage(text=long_text, channel="wechat-main"))

    assert result.ok is True
    # truncated into a single body chunk carrying the truncation notice
    assert len(adapter.sends) == 1
    assert "已截断" in adapter.sends[0].text


# -- text helpers ----------------------------------------------------------


def test_strip_markdown_removes_code_fence_and_bold() -> None:
    src = "## Title\n\n**bold** and _italic_ and `code`.\n\n```python\nprint(1)\n```"
    out = strip_markdown(src)
    assert "##" not in out
    assert "**" not in out
    assert "_" not in out
    assert "`" not in out
    assert "print(1)" in out
    assert "Title" in out


def test_strip_markdown_links_to_text() -> None:
    src = "see [docs](https://example.com/x) and ![alt](https://example.com/i.png)"
    out = strip_markdown(src)
    assert "docs" in out
    assert "alt" in out
    assert "https://example.com" not in out


def test_strip_markdown_list_markers() -> None:
    src = "- one\n- two\n1. first\n2. second\n> quote"
    out = strip_markdown(src)
    assert out.startswith("one")
    assert "first" in out
    assert "quote" in out
    assert "- " not in out


def test_split_text_short() -> None:
    assert split_text("hello", 4000) == ["hello"]


def test_split_text_long_on_boundary() -> None:
    text = "\n".join(f"line {i}" for i in range(1000))  # ~7000 chars
    chunks = split_text(text, 1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    # reconstruct loses only whitespace
    rejoined = " ".join(c.replace("\n", " ") for c in chunks)
    for i in range(1000):
        assert f"line {i}" in rejoined


def test_maybe_truncate_with_liveview_keeps_short() -> None:
    text = "x" * 100
    out = maybe_truncate_with_liveview(text, chunk_size=4000, max_chunks=4)
    assert out == [text]


def test_maybe_truncate_with_liveview_truncates_with_link() -> None:
    text = "\n".join(["word" * 200] * 40)  # very long
    out = maybe_truncate_with_liveview(
        text, chunk_size=4000, max_chunks=2, liveview_url="https://lv/x"
    )
    assert len(out) == 1
    assert "已截断" in out[0]
    assert "https://lv/x" in out[0]


def test_maybe_truncate_default_max_chunks() -> None:
    assert DEFAULT_MAX_CHUNKS == 4
    text = "\n".join(["word" * 200] * 40)
    out = maybe_truncate_with_liveview(text, chunk_size=1000, max_chunks=DEFAULT_MAX_CHUNKS)
    assert len(out) == 1
    assert "已截断" in out[0]
