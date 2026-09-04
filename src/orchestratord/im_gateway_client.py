"""OrchestratorGatewayClient — orchestrator opt-in IM dispatch (P5).

Registered per issue/run session via the gateway UDS. Splits inbound
semantics to existing orchestrator entry points — never invents new
synonyms:

  * ``followUp`` → ``queue_pending_message`` (the existing pending queue)
  * ``pause/resume/stop`` → control socket verbs
  * ``inject`` / ``contextOnly`` → ``issue inject`` / ``.operator_hints.md``
    (NOT the control-socket no-op)
  * ``command`` (``/agent retry|follow-up|unblock``) → existing
    ``parse_agent_command`` path

The client is a pure dispatcher with injectable handlers so it is
unit-testable without a live orchestrator. The daemon wiring binds the
real handlers. ``orchestrator_cli`` commands run serialized in a worker
thread with a timeout and ``sys.argv`` restored afterwards (P2-6), so a
slow command never wedges the IPC read loop or leaks process-global argv.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import logging
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orchestratord.im_gateway.semantics import (
    CommandRouter,
    ControlBridge,
    MessageClassifier,
)
from orchestratord.ipc.models import InboundMessage, MessageSemantics

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorHandlers:
    queue_pending_message: Callable[[str, str], None]  # (issue_id, text) -> None
    control_verb: Callable[[str, str], None]  # (verb, issue_id) -> None
    issue_inject: Callable[[str, str], None]  # (issue_id, hint) -> None
    operator_hints: Callable[[str, str], None]  # (issue_id, text) -> None
    agent_intent: Callable[[str, str], None]  # (verb, issue_id) -> None
    issue_cli: Callable[[str, str, str], None]  # (verb, issue_id, payload) -> None
    bridge_interrupt: Callable[[str, str], None]  # (issue_id, payload) -> None


class OrchestratorGatewayClient:
    def __init__(
        self,
        handlers: OrchestratorHandlers,
        *,
        ipc_client=None,
        origin: str = "",
        command_router: CommandRouter | None = None,
        control_bridge: ControlBridge | None = None,
        pending_outbound_limit: int = 20,
        pending_retry_base_seconds: float = 60.0,
        pending_retry_max_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        cli_runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
        cli_timeout_seconds: float = 60.0,
    ) -> None:
        self._h = handlers
        self._commands = command_router or CommandRouter()
        self._control = control_bridge or ControlBridge()
        self._ipc = ipc_client
        self._origin = origin
        self._cli_runner = cli_runner
        # P2-6: commands serialize on one lock and run bounded — a slow
        # status/list must not wedge the IPC read loop forever.
        self._cli_lock = asyncio.Lock()
        self._cli_timeout_seconds = max(1.0, cli_timeout_seconds)
        self._pending_outbound: deque[str] = deque()
        # Per-text outbound envelope (metadata / in_reply_to) so queued
        # texts keep their event context across deferred flushes.
        self._pending_outbound_extra: dict[str, dict[str, Any]] = {}
        self._pending_outbound_limit = max(1, pending_outbound_limit)
        self._flush_lock = asyncio.Lock()
        self._clock = clock
        self._pending_retry_base_seconds = max(0.0, pending_retry_base_seconds)
        self._pending_retry_max_seconds = max(
            self._pending_retry_base_seconds,
            pending_retry_max_seconds,
        )
        self._pending_retry_delay = self._pending_retry_base_seconds
        self._pending_next_flush_at = 0.0
        # DELIVER-triggered flush task (see _schedule_deliver_flush) and the
        # delivery currently being dispatched (for in_reply_to threading).
        self._deliver_flush_task: asyncio.Task[None] | None = None
        self._current_delivery_id: str = ""
        # Signature probe cache for the bound IPC client's send_outbound.
        self._ipc_send_params: dict[str, bool] = {}
        if ipc_client is not None:
            # Route server-pushed DELIVER frames through dispatch.
            ipc_client.on_deliver = self._on_pushed_deliver

    async def _on_pushed_deliver(self, message: InboundMessage) -> None:
        """Server-pushed DELIVER (gateway→orchestrator): classify + dispatch."""
        # ``GatewayIpcClient`` always supplies ``InboundMessage``.  Accept a
        # frame-shaped object as well for compatibility with integrations that
        # previously invoked this private callback directly.
        if not isinstance(message, InboundMessage):
            message = InboundMessage(
                origin=getattr(message, "origin", "") or self._origin,
                text=getattr(message, "text", "") or "",
                message_id=getattr(message, "delivery_id", "") or "",
                channel_type="gateway",
                semantic=getattr(message, "semantic", None),
                context_token=getattr(message, "context_token", None),
                semantic_tags=list(getattr(message, "semantic_tags", []) or []),
                metadata=dict(getattr(message, "metadata", {}) or {}),
            )
        semantic = None
        if message.semantic:
            with contextlib.suppress(ValueError):
                semantic = MessageSemantics(message.semantic)
        # Keep the normalized message supplied by GatewayIpcClient.  Its
        # metadata/context token are part of the routing contract and must not
        # be discarded while crossing the IPC boundary.
        if not message.origin:
            message.origin = self._origin
        if semantic is None:
            message.semantic = self._classify(message)
            semantic = message.semantic
        # Track the in-flight delivery so replies queued during dispatch can
        # thread in_reply_to back to the triggering IM message.
        self._current_delivery_id = message.message_id
        try:
            status = await self.dispatch(message, semantic)
            # Flush DELIVER-triggered outbound replies OFF the read-loop
            # chain: the IPC read loop awaits on_deliver sequentially, and
            # awaiting the flush here would wait for an OUTBOUND ACK that
            # only that same read loop can read — a deadlock until
            # reply_timeout. Fire-and-forget instead; complete_processing
            # (fire-and-forget itself) still goes out reliably.
            self._schedule_deliver_flush()
            # Yield once so the scheduled flush starts before this delivery
            # callback returns.
            await asyncio.sleep(0)
            await self._complete_processing(
                message.message_id,
                "failure" if status in {"not_dispatched", "command_unroutable"} else "success",
                status,
            )
            logger.info(
                "orchestrator IM push dispatched: delivery_id=%s status=%s",
                message.message_id[:16],
                status,
            )
        except Exception:
            await self._complete_processing(
                message.message_id,
                "failure",
                "orchestrator dispatch failed",
            )
            logger.exception("orchestrator IM dispatch failed")
        finally:
            self._current_delivery_id = ""

    def _schedule_deliver_flush(self) -> None:
        """Schedule the DELIVER-triggered pending-outbound flush as a task.

        At most one flush task exists at a time: while one is draining,
        consecutive DELIVERs skip scheduling because the running drain loop
        re-checks the queue after every send and picks up newly queued
        items. The task reference is kept (GC) and its failures logged.
        """
        task = self._deliver_flush_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._deliver_flush_runner())
        self._deliver_flush_task = task
        task.add_done_callback(self._on_deliver_flush_done)

    def _on_deliver_flush_done(self, task: asyncio.Task[None]) -> None:
        if self._deliver_flush_task is task:
            self._deliver_flush_task = None
        if not task.cancelled() and task.exception() is not None:
            logger.warning(
                "orchestrator IM deliver flush task failed",
                exc_info=task.exception(),
            )

    async def _deliver_flush_runner(self) -> None:
        # Drain until a full pass makes no progress (empty queue, or a
        # NACK/deferred/error that must respect the retry backoff instead
        # of hot-looping with force=True).
        while self._pending_outbound:
            before = len(self._pending_outbound)
            await self._flush_pending_outbound(force=True)
            if len(self._pending_outbound) >= before:
                return

    async def _complete_processing(
        self,
        message_id: str,
        outcome: str,
        reason: str,
    ) -> None:
        complete = getattr(self._ipc, "complete_processing", None)
        if message_id and callable(complete):
            try:
                await complete(message_id=message_id, outcome=outcome, reason=reason)
            except (ConnectionError, RuntimeError, OSError):
                logger.debug(
                    "orchestrator processing completion skipped while disconnected: %s",
                    message_id[:16],
                )
            except Exception:
                logger.warning(
                    "orchestrator processing completion failed: %s",
                    message_id[:16],
                    exc_info=True,
                )

    def _classify(self, message):
        return MessageClassifier().classify(message)

    async def send_outbound(
        self,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
        in_reply_to: str | None = None,
    ) -> None:
        """Send a reply / event back to the IM origin via the OUTBOUND frame.

        The origin is the opt-in origin (``im:direct:*:*`` by default for
        orchestrator); the gateway resolves the wildcard to a concrete
        sender at OUTBOUND time. ``metadata`` (event envelope: issue_id /
        event_type / level / markdown) and ``in_reply_to`` (the triggering
        delivery id) are forwarded when the bound IPC client supports them;
        replies sent while a DELIVER is being dispatched default
        ``in_reply_to`` to that delivery's id. The event is queued only
        when the send cannot start right now (the IPC socket is not open
        yet) or when the gateway explicitly NACKs the send. If the IPC ACK
        times out, delivery is ambiguous: the gateway may already have
        sent the IM message but returned its ACK too late. In that case we
        do not auto-retry, because duplicate chat messages are worse than a
        best-effort dropped event.
        """
        if self._ipc is None or not self._origin:
            return
        if not in_reply_to and self._current_delivery_id:
            in_reply_to = self._current_delivery_id
        if text in self._pending_outbound:
            logger.debug("orchestrator IM outbound deduped before send: %r", text[:60])
            self._remember_pending_extra(text, metadata, in_reply_to)
            await self._flush_pending_outbound()
            return
        if self._pending_outbound:
            self._queue_pending_outbound(text, metadata, in_reply_to)
            await self._flush_pending_outbound()
            return
        sent = await self._send_to_origin(self._origin, text, metadata, in_reply_to)
        if not sent:
            self._queue_pending_outbound(text, metadata, in_reply_to)

    def _ipc_accepts(self, param: str) -> bool:
        """Whether the bound IPC client's ``send_outbound`` accepts ``param``.

        The real :class:`~orchestratord.ipc.client.GatewayIpcClient`
        supports the full OUTBOUND frame (metadata / in_reply_to); simpler
        stubs only accept ``origin``/``text``. Probe once and cache.
        """
        if param in self._ipc_send_params:
            return self._ipc_send_params[param]
        try:
            sig = inspect.signature(self._ipc.send_outbound)
        except (TypeError, ValueError):
            supported = False
        else:
            params = sig.parameters
            supported = param in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        self._ipc_send_params[param] = supported
        return supported

    async def _send_to_origin(
        self,
        origin: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        in_reply_to: str | None = None,
    ) -> bool:
        if self._ipc is None:
            return False
        kwargs: dict[str, Any] = {"origin": origin, "text": text}
        if metadata and self._ipc_accepts("metadata"):
            kwargs["metadata"] = metadata
        if in_reply_to and self._ipc_accepts("in_reply_to"):
            kwargs["in_reply_to"] = in_reply_to
        try:
            response = await self._ipc.send_outbound(**kwargs)
        except RuntimeError as exc:
            # Not connected yet (heartbeat loop hasn't run / reconnected).
            # Queue so the post-register flush delivers it; never propagate
            # — IM must not break the orchestrator main loop.
            logger.debug(
                "orchestrator IM outbound not connected; queueing origin=%s (%s)", origin[:32], exc
            )
            return False
        if response is None:
            logger.warning(
                "orchestrator IM outbound ACK timed out origin=%s; not retrying to avoid duplicate IM delivery",
                origin[:32],
            )
            self._reset_pending_flush_backoff()
            return True
        response_type = getattr(getattr(response, "type", None), "value", None)
        if response_type == "nack":
            reason = getattr(response, "reason", "") or ""
            logger.warning(
                "orchestrator IM outbound rejected origin=%s reason=%s",
                origin[:32],
                reason,
            )
            self._defer_pending_flush(reason)
            return False
        self._reset_pending_flush_backoff()
        return True

    def _queue_pending_outbound(
        self,
        text: str,
        metadata: dict[str, Any] | None = None,
        in_reply_to: str | None = None,
    ) -> None:
        # Skip exact duplicates already waiting in the queue — e.g. the
        # orchestrator emits "orchestratord: IM notifications
        # enabled" on every reconnect, and if the gateway can't resolve
        # the wildcard origin (operator hasn't messaged recently), each
        # copy would queue and all would flush at once when the operator
        # finally sends a message.
        if text in self._pending_outbound:
            logger.debug("orchestrator IM outbound deduped: %r already queued", text[:60])
            self._remember_pending_extra(text, metadata, in_reply_to)
            return
        if len(self._pending_outbound) >= self._pending_outbound_limit:
            dropped = self._pending_outbound.popleft()
            self._pending_outbound_extra.pop(dropped, None)
            logger.warning("orchestrator IM outbound pending queue full; dropped oldest event")
        self._pending_outbound.append(text)
        self._remember_pending_extra(text, metadata, in_reply_to)
        logger.info("orchestrator IM outbound queued (pending connection or send retry)")

    def _remember_pending_extra(
        self,
        text: str,
        metadata: dict[str, Any] | None,
        in_reply_to: str | None,
    ) -> None:
        extra: dict[str, Any] = {}
        if metadata:
            extra["metadata"] = metadata
        if in_reply_to:
            extra["in_reply_to"] = in_reply_to
        if extra:
            self._pending_outbound_extra[text] = extra
        else:
            self._pending_outbound_extra.pop(text, None)

    def _defer_pending_flush(self, reason: str) -> None:
        delay = self._pending_retry_delay
        self._pending_next_flush_at = self._clock() + delay
        self._pending_retry_delay = min(
            delay * 2 if delay > 0 else self._pending_retry_base_seconds,
            self._pending_retry_max_seconds,
        )
        logger.info(
            "orchestrator IM outbound retry deferred: delay=%.1fs reason=%s",
            delay,
            reason[:120],
        )

    def _reset_pending_flush_backoff(self) -> None:
        self._pending_retry_delay = self._pending_retry_base_seconds
        self._pending_next_flush_at = 0.0

    async def _flush_pending_outbound(self, *, force: bool = False) -> None:
        # Serialise flush calls — the heartbeat loop and the inbound push
        # handler can both call this concurrently; without a lock, both
        # peek self._pending_outbound[0] before an await, then both try
        # popleft(), causing IndexError: pop from an empty deque.
        async with self._flush_lock:
            if not self._pending_outbound:
                return
            origin = self._origin
            if not origin:
                return
            if force:
                self._reset_pending_flush_backoff()
            elif self._pending_next_flush_at > self._clock():
                logger.debug(
                    "orchestrator IM pending outbound flush deferred for %.1fs",
                    self._pending_next_flush_at - self._clock(),
                )
                return
            # Wildcard origins are flushed too: the gateway resolves them at
            # OUTBOUND time (recent sender, else persisted context tokens).
            while self._pending_outbound:
                text = self._pending_outbound[0]
                extra = self._pending_outbound_extra.get(text) or {}
                try:
                    sent = await self._send_to_origin(
                        origin,
                        text,
                        extra.get("metadata"),
                        extra.get("in_reply_to"),
                    )
                    if not sent:
                        return
                except Exception:
                    logger.warning(
                        "orchestrator IM pending outbound flush failed origin=%s",
                        origin[:32],
                        exc_info=True,
                    )
                    return
                self._pending_outbound.popleft()
                self._pending_outbound_extra.pop(text, None)

    async def dispatch(self, message: InboundMessage, semantic: MessageSemantics) -> str:
        """Route ``message`` to the right existing orchestrator entry.

        Returns a short status string describing the dispatch (for ack).
        """
        issue_id = self._issue_id(message)
        if semantic is MessageSemantics.FOLLOW_UP:
            self._h.queue_pending_message(issue_id, message.text)
            return "followup_queued"
        if semantic is MessageSemantics.CONTEXT_ONLY:
            self._h.operator_hints(issue_id, message.text)
            return "context_only_recorded"
        if semantic is MessageSemantics.INTERRUPT:
            # interrupt maps to control verbs via the bridge
            ctrl = self._control.resolve(MessageSemantics.INTERRUPT, None)
            if ctrl is not None:
                self._h.bridge_interrupt(issue_id, ctrl.payload)
            return "interrupt_dispatched"
        if semantic is MessageSemantics.COMMAND:
            route = self._commands.route(message)
            if route is None:
                return "command_unroutable"
            if route.kind == "orchestrator_cli":
                return await self._dispatch_orchestrator_cli(route)
            if route.kind == "agent_intent":
                self._h.agent_intent(route.verb, route.issue_hint or issue_id)
                return f"agent_{route.verb}"
            # control_verb
            ctrl = self._control.resolve(semantic, route)
            if ctrl is None:
                return "command_unroutable"
            if ctrl.surface == "control_socket":
                self._h.control_verb(ctrl.verb, ctrl.issue_hint or issue_id)
                return f"control_{ctrl.verb}"
            if ctrl.surface == "issue_inject":
                self._h.issue_inject(ctrl.issue_hint or issue_id, route.payload)
                return "inject_delivered"
            if ctrl.surface == "issue_cli":
                self._h.issue_cli(ctrl.verb, ctrl.issue_hint or issue_id, ctrl.payload)
                return f"issue_cli_{ctrl.verb}"
            return f"issue_cli_{ctrl.verb}"
        # newPrompt / approval → leave to the host agent / approval binding
        return "not_dispatched"

    async def _dispatch_orchestrator_cli(self, route) -> str:
        argv = list(route.argv)
        if len(argv) < 2:
            self._queue_command_reply(route.payload, 2, "", "error: invalid orchestrator command")
            return "orchestrator_cli_invalid"

        noun, verb = argv[0], argv[1]
        if noun == "issue" and verb in {"stop", "pause", "resume"}:
            issue_id = route.issue_hint or self._arg_value(argv, "--id")
            if not issue_id:
                self._queue_command_reply(route.payload, 2, "", "error: --id is required")
                return f"orchestrator_cli_issue_{verb}"
            self._h.control_verb(verb, issue_id)
            self._queue_command_reply(
                route.payload,
                0,
                f"Control command '{verb}' sent for issue {issue_id}",
                "",
            )
            return f"orchestrator_cli_issue_{verb}"

        if noun == "issue" and verb == "tail":
            self._queue_command_reply(route.payload, 0, self._tail_notice(argv), "")
            return "orchestrator_cli_issue_tail"

        rc, stdout, stderr = await self._run_cli_isolated(argv)
        self._queue_command_reply(route.payload, rc, stdout, stderr)
        return f"orchestrator_cli_{noun}_{verb}"

    async def _run_cli_isolated(self, argv: list[str]) -> tuple[int, str, str]:
        """Run one orchestrator CLI command serialized, bounded, off-loop.

        P2-6 transitional isolation: commands hold ``_cli_lock`` (one at a
        time, FIFO), execute in a worker thread via :func:`asyncio.to_thread`
        so the IPC read loop's event loop stays responsive to heartbeats and
        interleaved traffic, and are bounded by ``cli_timeout_seconds``.
        A timeout reports rc 124 back to the IM sender.
        """
        async with self._cli_lock:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._run_orchestrator_cli, list(argv)),
                    timeout=self._cli_timeout_seconds,
                )
            except TimeoutError:
                logger.warning(
                    "orchestrator IM command timed out after %.0fs: %s",
                    self._cli_timeout_seconds,
                    " ".join(argv)[:64],
                )
                return (
                    124,
                    "",
                    f"error: command timed out after {self._cli_timeout_seconds:.0f}s",
                )

    def _run_orchestrator_cli(self, argv: list[str]) -> tuple[int, str, str]:
        if self._cli_runner is not None:
            return self._cli_runner(list(argv))

        # Transitional in-process runner (typed command service comes later):
        # patch sys.argv only for the duration of the call and ALWAYS restore
        # it — the process-wide argv belongs to the host orchestrator.
        argv_backup = sys.argv
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                sys.argv = ["orchestratord"] + list(argv)
                try:
                    from orchestratord.cli.main import app

                    app()
                    rc = 0
                except SystemExit as exc:
                    code = exc.code
                    rc = code if isinstance(code, int) else 1
                except Exception as exc:  # noqa: BLE001
                    print(f"error: {exc}", file=sys.stderr)
                    rc = 1
        finally:
            sys.argv = argv_backup
        return rc, stdout.getvalue(), stderr.getvalue()

    def _queue_command_reply(self, command_text: str, rc: int, stdout: str, stderr: str) -> None:
        text = self._format_command_reply(command_text, rc, stdout, stderr)
        # Thread the reply back to the IM message that triggered the
        # command (in_reply_to), when the IPC client supports it.
        self._queue_pending_outbound(
            text, in_reply_to=self._current_delivery_id or None
        )

    @staticmethod
    def _format_command_reply(command_text: str, rc: int, stdout: str, stderr: str) -> str:
        command = (command_text or "").strip() or "<empty>"
        prefix = "命令已执行" if rc == 0 else f"命令执行失败({rc})"
        output = "\n".join(part.strip() for part in (stdout, stderr) if part and part.strip())
        if not output:
            return f"{prefix}：{command}"
        if len(output) > 6000:
            output = output[:6000].rstrip() + "\n..."
        return f"{prefix}：{command}\n\n{output}"

    @staticmethod
    def _tail_notice(argv: list[str]) -> str:
        issue_id = OrchestratorGatewayClient._arg_value(argv, "--id") or "<issue-id>"
        return (
            f"/issue tail --id {issue_id} is a streaming command. "
            "IM returns this bounded notice instead of holding the gateway connection. "
            "Run `orchestratord issue tail --id "
            f"{issue_id}` locally for live tailing."
        )

    @staticmethod
    def _arg_value(argv: list[str], flag: str) -> str | None:
        for idx, token in enumerate(argv):
            if token == flag and idx + 1 < len(argv):
                return argv[idx + 1]
            if token.startswith(f"{flag}="):
                return token.split("=", 1)[1]
        return None

    @staticmethod
    def _issue_id(message: InboundMessage) -> str:
        if message.raw and isinstance(message.raw, dict):
            iid = message.raw.get("issue_id")
            if iid:
                return str(iid)
        return ""


__all__ = [
    "CommandRouter",
    "ControlBridge",
    "MessageClassifier",
    "OrchestratorGatewayClient",
    "OrchestratorHandlers",
]
