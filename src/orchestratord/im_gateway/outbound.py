"""Outbound dispatcher.

Gateway → CapabilityGate → ChannelAdapterRegistry → OutboundCapability.send().
Writes an outbox entry (with idempotency_key) before every send, records
the provider receipt on success, and moves non-retryable failures to the
dead-letter store. Markdown→plain-text fallback and long-message
LiveView fallback are applied per the channel's capability descriptor.

The full retry loop, storm aggregation, and compaction land in P4; v1
does a single attempt (plus the platform-rejection plain-text retry)
and records the result for P4 to replay.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from orchestratord.channels.authorization import permits_target
from orchestratord.channels.capabilities import (
    CapabilityNotDeclaredError,
    ChannelCapability,
)
from orchestratord.channels.models import ChannelMessage
from orchestratord.channels.results import ChannelSendResult, ErrorCategory, SendStatus
from orchestratord.ipc.models import OutboundMessage

from .capability_gate import CapabilityGate
from .config import GatewayConfig
from .store import ReliabilityStore
from .text import maybe_truncate_with_liveview, strip_markdown

logger = logging.getLogger(__name__)


class OutboundDispatcher:
    def __init__(
        self,
        registry,
        gate: CapabilityGate,
        store: ReliabilityStore,
        config: GatewayConfig,
        *,
        sleep=asyncio.sleep,
    ) -> None:
        self._registry = registry
        self._gate = gate
        self._store = store
        self._config = config
        self._sleep = sleep

    def _resolve_descriptor(self, channel: str):
        adapter = self._registry.get(channel)
        if adapter is None:
            return None, None
        cap = ChannelCapability.OUTBOUND_TEXT
        if not adapter.capabilities.has(cap):
            return adapter, None
        return adapter, adapter.capabilities.descriptor(cap)

    def _prepare_text(self, channel: str, text: str) -> tuple[list[str], bool]:
        """Return (chunks, stripped) after markdown/long-message handling."""
        _adapter, descriptor = self._resolve_descriptor(channel)
        supports_markdown = bool(descriptor and descriptor.supports_markdown)
        max_chunks = self._config.reliability.long_message_threshold_chunks
        effective = text
        stripped = False
        if self._config.reliability.markdown_fallback and not supports_markdown:
            effective = strip_markdown(effective)
            stripped = True
        chunks = maybe_truncate_with_liveview(effective, max_chunks=max_chunks)
        return chunks, stripped

    async def send(self, message: OutboundMessage) -> ChannelSendResult:
        idem = message.idempotency_key or f"out:{uuid.uuid4()}"
        channel = message.channel

        adapter = self._registry.get(channel)
        if adapter is None:
            self._store.append_outbox(
                {
                    "idempotency_key": idem,
                    "channel": channel,
                    "status": "dead",
                    "error": "channel_not_found",
                    "at": time.time(),
                }
            )
            return ChannelSendResult.nonretryable_error(
                channel,
                message=f"channel {channel!r} not registered",
                category=ErrorCategory.NOT_FOUND,
            )
        logger.debug(
            "outbound send: channel=%s idem=%s len=%d", channel, idem[:16], len(message.text)
        )

        # fail-closed capability gate
        try:
            adapter = self._gate.require_outbound(channel)
        except Exception as exc:  # noqa: BLE001
            self._store.append_outbox(
                {
                    "idempotency_key": idem,
                    "channel": channel,
                    "status": "dead",
                    "error": f"capability_gate: {exc}",
                    "at": time.time(),
                }
            )
            logger.warning("outbound send: capability gate rejected channel=%s: %s", channel, exc)
            return ChannelSendResult.unsupported(channel, message=str(exc))

        chunks, stripped = self._prepare_text(channel, message.text)
        last_result: ChannelSendResult | None = None
        for idx, chunk in enumerate(chunks):
            chunk_idem = idem if len(chunks) == 1 else f"{idem}#{idx}"
            # The pending record carries the full delivery parameters so the
            # startup replay worker can rebuild this exact send after a crash
            # (outbox recovery). Status transitions are append-only NDJSON:
            # a later delivered/failed/dead record for the same
            # idempotency_key supersedes this one on read.
            self._store.append_outbox(
                {
                    "idempotency_key": chunk_idem,
                    "channel": channel,
                    "target": message.target,
                    "text": chunk,
                    "context_token": message.context_token,
                    "title": message.title,
                    "markdown": message.markdown,
                    "metadata": message.metadata,
                    "semantic_tags": list(message.semantic_tags or []),
                    "level": message.level,
                    "payload_size": len(chunk),
                    "chunk_index": idx,
                    "chunks_total": len(chunks),
                    "status": "pending",
                    "at": time.time(),
                }
            )
            last_result = await self._send_chunk_with_retry(
                chunk,
                message,
                chunk_idem,
                stripped,
                channel,
            )
            if not last_result.ok:
                logger.warning(
                    "outbound send: chunk %d failed channel=%s status=%s error=%s",
                    idx,
                    channel,
                    last_result.status.value if last_result.status else "?",
                    last_result.message or "",
                )
                return last_result
        success_text = "ok" if last_result and last_result.ok else "empty"
        logger.info("outbound send: %s channel=%s", success_text, channel)
        return last_result or ChannelSendResult.success(channel)

    async def _send_chunk_with_retry(
        self,
        chunk: str,
        message: OutboundMessage,
        chunk_idem: str,
        stripped: bool,
        channel: str,
    ) -> ChannelSendResult:
        """Send one chunk with retry-policy backoff; dead-letter on terminal failure."""
        from orchestratord.channels.retry import compute_backoff

        policy = self._adapter_retry_policy(channel)
        max_attempts = policy.max_attempts
        attempt = 1
        payload = ChannelMessage(
            text=chunk,
            title=message.title,
            markdown=message.markdown and not stripped,
            metadata=message.metadata,
        )
        while True:
            result = await self._send_authorized(channel, payload, message)
            # Platform rejected the stripped text — retry once as plain text.
            if (
                not result.ok
                and stripped
                and result.status is SendStatus.NONRETRYABLE_ERROR
                and result.error_category is not ErrorCategory.AUTH
                and attempt == 1
            ):
                plain = strip_markdown(chunk) if chunk != strip_markdown(chunk) else chunk
                payload = ChannelMessage(text=plain, markdown=False, metadata=message.metadata)
                result = await self._send_authorized(channel, payload, message)
            if result.ok:
                self._store.append_outbox(
                    {
                        "idempotency_key": chunk_idem,
                        "channel": channel,
                        "status": "delivered",
                        "provider_receipt": result.provider_receipt,
                        "attempts": attempt,
                        "at": time.time(),
                    }
                )
                return result
            if result.status is SendStatus.RATE_LIMITED:
                self._store.append_outbox(
                    {
                        "idempotency_key": chunk_idem,
                        "channel": channel,
                        "status": "failed",
                        "error_category": result.error_category.value,
                        "message": result.message,
                        "attempts": attempt,
                        "at": time.time(),
                    }
                )
                return result
            # terminal non-retryable → dead-letter
            if not result.retryable or attempt >= max_attempts:
                self._store.append_outbox(
                    {
                        "idempotency_key": chunk_idem,
                        "channel": channel,
                        "status": "failed",
                        "error_category": result.error_category.value,
                        "message": result.message,
                        "attempts": attempt,
                        "at": time.time(),
                    }
                )
                self._store.append_dead_letter(
                    {
                        "idempotency_key": chunk_idem,
                        "channel": channel,
                        "target": message.target,
                        "error_category": result.error_category.value,
                        "message": result.message,
                        "attempts": attempt,
                        "at": time.time(),
                    }
                )
                if not result.retryable:
                    logger.warning(
                        "outbound dead-letter: channel=%s idem=%s category=%s message=%s",
                        channel,
                        chunk_idem[:16],
                        result.error_category.value,
                        result.message or "",
                    )
                else:
                    logger.warning(
                        "outbound exhausted: channel=%s idem=%s attempts=%d/%d last=%s",
                        channel,
                        chunk_idem[:16],
                        attempt,
                        max_attempts,
                        result.message or "",
                    )
                return result
            # retryable + under budget → backoff and retry
            self._store.append_outbox(
                {
                    "idempotency_key": chunk_idem,
                    "channel": channel,
                    "status": "retry_pending",
                    "error_category": result.error_category.value,
                    "attempt": attempt,
                    "at": time.time(),
                }
            )
            delay = compute_backoff(attempt, policy)
            logger.info(
                "outbound retry: channel=%s idem=%s attempt=%d/%d delay=%.1fs category=%s",
                channel,
                chunk_idem[:16],
                attempt,
                max_attempts,
                delay,
                result.error_category.value,
            )
            await self._sleep(delay)
            attempt += 1

    async def _send_authorized(
        self, channel: str, payload: ChannelMessage, message: OutboundMessage
    ) -> ChannelSendResult:
        # Refresh on every attempt/chunk: a reload may revoke the recipient
        # while a prior send is waiting on provider I/O or retry backoff.
        try:
            adapter = self._gate.require_outbound(channel)
        except (KeyError, CapabilityNotDeclaredError):
            return ChannelSendResult.unsupported(channel, message="channel unavailable")
        if not permits_target(adapter, message.target):
            self._store.audit("outbound_recipient_rejected", channel=channel)
            return ChannelSendResult.nonretryable_error(
                channel, message="recipient is not authorized", category=ErrorCategory.AUTH
            )
        context_token = message.context_token
        config = getattr(adapter, "config", None)
        if (
            getattr(getattr(config, "type", None), "value", None) == "feishu"
            and callable(getattr(adapter, "authorized_recipients", None))
        ):
            # Feishu's context token is a chat id and overrides an open_id.
            # Address the authorized recipient directly instead of allowing
            # stale/arbitrary context to redirect a queued private report.
            context_token = None
        return await adapter.send(
            payload, target=message.target, context_token=context_token
        )

    def _adapter_retry_policy(self, channel: str):
        adapter = self._registry.get(channel)
        if adapter is not None:
            return adapter.retry_policy
        from orchestratord.channels.retry import DEFAULT_RETRY_POLICY

        return DEFAULT_RETRY_POLICY

    def deferred_outbound_count(self) -> int:
        """Count outbox records without a terminal status (pending/retry_pending).

        These are the recoverable sends: replayed at gateway startup and
        reported through ``health()``. Records for sends currently in flight
        also count until their outcome record lands.
        """
        return len(self._store.outbox_pending())

    def clear_deferred(self) -> None:
        """No-op: pending outbox records are durable and must survive restarts.

        The replay worker at gateway startup re-sends them; clearing them on
        stop would defeat the outbox recovery semantics.
        """

    async def broadcast(
        self, message: OutboundMessage, *, channels: list[str] | None = None
    ) -> dict[str, ChannelSendResult]:
        names = channels or self._registry.names()
        results: dict[str, ChannelSendResult] = {}
        for name in names:
            try:
                results[name] = await self.send(
                    OutboundMessage(
                        text=message.text,
                        channel=name,
                        target=message.target,
                        context_token=message.context_token,
                        level=message.level,
                        title=message.title,
                        markdown=message.markdown,
                        metadata=message.metadata,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("outbound broadcast failed: channel=%s: %s", name, exc)
                results[name] = ChannelSendResult.nonretryable_error(
                    name, message=f"broadcast raised: {exc}", category=ErrorCategory.UNKNOWN
                )
        return results


__all__ = ["OutboundDispatcher"]
