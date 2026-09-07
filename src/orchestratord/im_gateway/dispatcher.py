"""Inbound dispatcher.

Pipeline: dedupe → classify (six-class) → route → handle.

Classification uses :class:`MessageClassifier` (P5): structured
``deliverAs`` wins; ``/agent`` + control verbs → ``command``; busy
ordinary text → ``followUp`` (queue-as-followUp); idle plain text →
``newPrompt``. ``interrupt``/``contextOnly`` are never guessed from
natural language — only structured metadata or existing control/bridge
entry points.

The dispatcher delegates execution to a registered handler. Follow-up
semantics for unbound origins fall through to the default handler (stub
agent); opt-in hosts own their own queueing.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from orchestratord.channels.capabilities import ProcessingOutcome
from orchestratord.im_gateway.semantics import MessageClassifier

logger = logging.getLogger(__name__)

from orchestratord.ipc.models import (
    AckLayer,
    AckReceipt,
    InboundMessage,
    MessageSemantics,
)

from .config import CommandAllowlistConfig
from .repl_command_gate import (
    PLAIN_TEXT_NOTICE,
    check_orchestrator_command,
    check_repl_command,
)
from .router import SessionRouter
from .store import ReliabilityStore

if TYPE_CHECKING:
    from .processing_status import ProcessingStatusManager

InboundHandler = Callable[[InboundMessage], Awaitable[AckReceipt | None]]
PushHandler = Callable[[InboundMessage], Awaitable[bool]]

# host_types that route to an opt-in peer over IPC push instead of the
# default in-process handler.
_OPT_IN_HOST_TYPES = frozenset({"repl", "orchestrator", "opt_in"})


class InboundDispatcher:
    def __init__(
        self,
        store: ReliabilityStore,
        router: SessionRouter,
        *,
        classifier: MessageClassifier | None = None,
        command_allowlists: CommandAllowlistConfig | None = None,
        processing_status: ProcessingStatusManager | None = None,
    ) -> None:
        self._store = store
        self._router = router
        self._classifier = classifier or MessageClassifier()
        effective_allowlists = command_allowlists or CommandAllowlistConfig()
        self._repl_allowed_commands = frozenset(effective_allowlists.repl)
        self._orchestrator_allowed_commands = frozenset(effective_allowlists.orchestrator)
        self._processing_status = processing_status
        self._handler: InboundHandler | None = None
        self._push_handler: PushHandler | None = None

    def set_handler(self, handler: InboundHandler) -> None:
        self._handler = handler

    def set_push_handler(self, handler: PushHandler) -> None:
        """Register the IPC push callback used for opt-in origins.

        When an origin is bound to an opt-in peer (REPL/orchestrator), the
        dispatcher pushes the message over IPC instead of calling the
        default handler. ``handler`` returns True if a live peer received it.
        """
        self._push_handler = handler

    def classify(
        self, message: InboundMessage, *, is_busy: bool = False, has_pending_wait: bool = False
    ) -> MessageSemantics:
        return self._classifier.classify(
            message, is_busy=is_busy, has_pending_wait=has_pending_wait
        )

    async def process(self, message: InboundMessage) -> AckReceipt:
        delivery_id = str(uuid.uuid4())
        # 1. dedupe
        key = message.message_id or f"{message.origin}:{message.text}"
        if not self._store.check_and_record(key, message_id=message.message_id):
            logger.debug("im_gateway: duplicate inbound skipped origin=%s", message.origin[:32])
            return AckReceipt(delivery_id, AckLayer.ACCEPTED, message="duplicate; skipped")
        # 2. classify — honor a caller-supplied semantic (e.g. from a
        # busy-aware handler re-dispatch), else classify fresh.
        if message.semantic is None:
            message.semantic = self.classify(message)
        # 3. route — reject if opt-in target is offline (no offline payload store)
        if self._router.is_offline(message.origin):
            self._store.audit(
                "target_offline",
                delivery_id=delivery_id,
                origin=message.origin,
                message_id=message.message_id,
            )
            logger.info(
                "im_gateway: target offline origin=%s; accepted but not delivered",
                message.origin[:32],
            )
            return AckReceipt(
                delivery_id,
                AckLayer.ACCEPTED,
                message="target_offline; rebind or use default session",
            )
        target = self._router.route(message.origin)
        logger.info(
            "im_gateway: route origin=%s semantic=%s target=%s host_type=%s",
            message.origin[:32],
            message.semantic.value if message.semantic else None,
            target.session_id[:32],
            target.host_type,
        )
        # 3.1 plain-text isolation (P1-2): the gateway is command-only.
        # Ordinary text — from ANY host target (REPL peer, orchestrator
        # peer, or the default agent handler) — is rejected with a bounded
        # notice and never dispatched onward. Only slash commands reach a
        # host; the per-host allowlist gates below then decide which ones.
        if not (message.text or "").strip().startswith("/"):
            self._store.audit(
                "plain_text_rejected",
                delivery_id=delivery_id,
                origin=message.origin,
                message_id=message.message_id,
            )
            logger.info(
                "im_gateway: plain text rejected origin=%s len=%d",
                message.origin[:32],
                len(message.text or ""),
            )
            return AckReceipt(
                delivery_id,
                AckLayer.ACCEPTED,
                message=PLAIN_TEXT_NOTICE,
                notify_user=True,
            )
        # 3.5 opt-in runtime 白名单门禁：只放行白名单内的斜杠命令，
        # 其余斜杠命令在网关层直接拒绝（不 push、不入队）。
        if target.host_type == "repl":
            allowed, reason = check_repl_command(
                message.text or "",
                allowed_commands=self._repl_allowed_commands,
            )
            if not allowed:
                self._store.audit(
                    "repl_command_blocked",
                    delivery_id=delivery_id,
                    origin=message.origin,
                    command=(message.text or "")[:64],
                    reason=reason,
                )
                logger.info(
                    "im_gateway: REPL command blocked origin=%s cmd=%s",
                    message.origin[:32],
                    (message.text or "")[:32],
                )
                return AckReceipt(
                    delivery_id,
                    AckLayer.ACCEPTED,
                    message=reason,
                    notify_user=True,
                )
        elif target.host_type == "orchestrator":
            allowed, reason = check_orchestrator_command(
                message.text or "",
                allowed_commands=self._orchestrator_allowed_commands,
            )
            if allowed and message.semantic is not MessageSemantics.COMMAND:
                allowed, reason = False, PLAIN_TEXT_NOTICE
            if not allowed:
                self._store.audit(
                    "orchestrator_command_blocked",
                    delivery_id=delivery_id,
                    origin=message.origin,
                    command=(message.text or "")[:64],
                    reason=reason,
                )
                logger.info(
                    "im_gateway: orchestrator command blocked origin=%s cmd=%s",
                    message.origin[:32],
                    (message.text or "")[:32],
                )
                return AckReceipt(
                    delivery_id,
                    AckLayer.ACCEPTED,
                    message=reason,
                    notify_user=True,
                )
        if self._processing_status is not None:
            await self._processing_status.start(message)
        # 4. opt-in origin (REPL/orchestrator bound over IPC) → push the whole
        # message to the peer; the peer owns its own queueing/semantics. This
        # overrides the default in-process handler.
        if target.host_type in _OPT_IN_HOST_TYPES and self._push_handler is not None:
            try:
                delivered = await self._push_handler(message)
            except Exception:
                logger.exception("im_gateway: push_handler error origin=%s", message.origin[:32])
                delivered = False
            if delivered:
                return AckReceipt(delivery_id, AckLayer.ENQUEUED, message="pushed to opt-in peer")
            # push failed (peer offline) → fall through to default handler
            logger.warning(
                "im_gateway: opt-in push failed origin=%s; falling back to default",
                message.origin[:32],
            )
        # 5. dispatch → handler
        if self._handler is not None:
            try:
                result = await self._handler(message)
            except Exception:
                await self._complete_processing(message, ProcessingOutcome.FAILURE)
                raise
            if result is not None:
                outcome = (
                    ProcessingOutcome.SUCCESS
                    if result.layer is AckLayer.PROCESSED
                    else ProcessingOutcome.FAILURE
                )
                await self._complete_processing(message, outcome)
                return result
        await self._complete_processing(message, ProcessingOutcome.FAILURE)
        return AckReceipt(delivery_id, AckLayer.ACCEPTED, message="accepted")

    async def _complete_processing(
        self,
        message: InboundMessage,
        outcome: ProcessingOutcome,
    ) -> None:
        if self._processing_status is None:
            return
        await self._processing_status.complete(
            message.message_id,
            outcome,
            origin=message.origin,
        )


__all__ = ["InboundDispatcher", "InboundHandler"]
