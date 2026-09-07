"""OrchestratorGatewayClient — orchestrator opt-in IM dispatch (P5).

Registered per issue/run session via the gateway UDS. The orchestratord
gateway surface is commands + event reports only:

  * whitelisted slash commands (``/server status``, ``/issue ...``) → the
    existing orchestrator CLI / control-verb entry points; replies go back
    to the SAME channel/user that issued the command (the delivery's
    concrete origin — never the wildcard, so a Feishu command is answered
    on Feishu even when a WeChat channel is also connected).
  * event reports (lifecycle / issue status / run results) → OUTBOUND to
    the wildcard origin (``im:direct:*:*`` by default), which the gateway
    resolves to the authorized recipient(s).

Both the gateway and this client reject semantic chat and non-whitelisted
commands. The client requires a concrete origin authenticated by the gateway.
Legacy semantic enums remain available for wire compatibility only.

The client is a pure dispatcher with injectable handlers so it is
unit-testable without a live orchestrator. The daemon wiring binds the
real handlers. Production ``orchestrator_cli`` commands run in a bounded
child process, so a timeout can terminate the command without mutating the
daemon's process-global ``sys.argv`` or stdio. The injectable synchronous
runner remains available for deterministic unit tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orchestratord.im_gateway.origin_utils import is_concrete_im_origin
from orchestratord.im_gateway.repl_command_gate import check_orchestrator_command
from orchestratord.im_gateway.semantics import (
    CommandRouter,
    ControlBridge,
    MessageClassifier,
)
from orchestratord.ipc.models import InboundMessage, MessageSemantics

logger = logging.getLogger(__name__)

_CLI_CHILD_CODE = (
    "import sys; "
    "from orchestratord.cli.main import app; "
    "sys.argv = ['orchestratord', *sys.argv[1:]]; "
    "app()"
)


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
        # Commands serialize on one lock. Production execution uses a
        # terminable subprocess; the tracked task only protects the injected
        # synchronous test runner, whose worker thread cannot be cancelled.
        self._cli_lock = asyncio.Lock()
        self._cli_timeout_seconds = max(0.01, cli_timeout_seconds)
        self._cli_run_task: asyncio.Task[tuple[int, str, str]] | None = None
        self._pending_outbound: deque[str] = deque()
        # A parallel envelope queue keeps metadata/routing aligned with each
        # queued message. It intentionally permits identical text for
        # different origins or deliveries, avoiding cross-channel reply loss.
        self._pending_outbound_extras: deque[dict[str, Any]] = deque()
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
                origin=getattr(message, "origin", "") or "",
                text=getattr(message, "text", "") or "",
                message_id=getattr(message, "delivery_id", "") or "",
                channel_type="gateway",
                semantic=getattr(message, "semantic", None),
                context_token=getattr(message, "context_token", None),
                semantic_tags=list(getattr(message, "semantic_tags", []) or []),
                metadata=dict(getattr(message, "metadata", {}) or {}),
            )
        semantic = None
        if message.semantic is not None:
            try:
                semantic = MessageSemantics(message.semantic)
            except ValueError:
                await self._complete_processing(message.message_id, "failure", "invalid semantic")
                return
        # Keep the normalized message supplied by GatewayIpcClient.  Its
        # metadata/context token are part of the routing contract and must not
        # be discarded while crossing the IPC boundary.
        if semantic is None:
            message.semantic = self._classify(message)
            semantic = message.semantic
        # Track the in-flight delivery so replies queued during dispatch can
        # thread in_reply_to back to the triggering IM message.
        self._current_delivery_id = message.message_id
        try:
            status = await self.dispatch(message, semantic)
            # Drain replies independently so provider latency does not delay
            # the next command. Command completion records execution outcome;
            # delivery success is reported separately by OUTBOUND ACK/NACK.
            self._schedule_deliver_flush()
            # Yield once so the scheduled flush starts before this delivery
            # callback returns.
            await asyncio.sleep(0)
            await self._complete_processing(
                message.message_id,
                "success" if status.startswith("orchestrator_cli_") and status not in {
                    "orchestrator_cli_invalid", "orchestrator_cli_failed",
                } else "failure",
                status,
            )
            logger.info(
                "orchestrator IM push dispatched: delivery_id=%s status=%s",
                message.message_id[:16],
                status,
            )
        except asyncio.CancelledError:
            await self._complete_processing(message.message_id, "cancelled", "connection closed")
            raise
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
        origin: str | None = None,
    ) -> None:
        """Send a reply / event back to the IM origin via the OUTBOUND frame.

        The origin defaults to the opt-in origin (``im:direct:*:*`` for
        orchestrator event reports); the gateway resolves the wildcard to
        the authorized recipient at OUTBOUND time. Command replies pass the
        DELIVER's concrete ``origin`` instead, so the answer lands on the
        channel that asked (no cross-channel misrouting with multiple
        channels connected). ``metadata`` (event envelope: issue_id /
        event_type / level / markdown) and ``in_reply_to`` (the triggering
        delivery id) are forwarded when the bound IPC client supports
        them; replies sent while a DELIVER is being dispatched default
        ``in_reply_to`` to that delivery's id. The event is queued only
        when the send cannot start right now (the IPC socket is not open
        yet) or when the gateway explicitly NACKs the send. If the IPC ACK
        times out, delivery is ambiguous: the gateway may already have
        sent the IM message but returned its ACK too late. In that case we
        do not auto-retry, because duplicate chat messages are worse than a
        best-effort dropped event.
        """
        target_origin = origin or self._origin
        if self._ipc is None or not target_origin:
            return
        if not in_reply_to and self._current_delivery_id:
            in_reply_to = self._current_delivery_id
        if self._pending_outbound:
            self._queue_pending_outbound(text, metadata, in_reply_to, target_origin)
            await self._flush_pending_outbound()
            return
        sent = await self._send_to_origin(target_origin, text, metadata, in_reply_to)
        if not sent:
            self._queue_pending_outbound(text, metadata, in_reply_to, target_origin)

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
        origin: str | None = None,
    ) -> None:
        # Skip an exact duplicate already waiting in the queue — e.g. the
        # orchestrator emits "orchestratord: IM notifications
        # enabled" on every reconnect, and if the gateway can't resolve
        # the wildcard origin (operator hasn't messaged recently), each
        # copy would queue and all would flush at once when the operator
        # finally sends a message. Origin and in_reply_to are part of the
        # identity: equal reply text from two channels must remain two sends.
        extra = self._pending_extra(metadata, in_reply_to, origin)
        for index, (pending_text, pending_extra) in enumerate(
            zip(self._pending_outbound, self._pending_outbound_extras, strict=True)
        ):
            if (
                pending_text == text
                and pending_extra.get("origin") == extra.get("origin")
                and pending_extra.get("in_reply_to") == extra.get("in_reply_to")
            ):
                self._pending_outbound_extras[index] = extra
                logger.debug("orchestrator IM outbound deduped: %r already queued", text[:60])
                return
        if len(self._pending_outbound) >= self._pending_outbound_limit:
            self._pending_outbound.popleft()
            self._pending_outbound_extras.popleft()
            logger.warning("orchestrator IM outbound pending queue full; dropped oldest event")
        self._pending_outbound.append(text)
        self._pending_outbound_extras.append(extra)
        logger.info("orchestrator IM outbound queued (pending connection or send retry)")

    @staticmethod
    def _pending_extra(
        metadata: dict[str, Any] | None,
        in_reply_to: str | None,
        origin: str | None = None,
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if metadata:
            extra["metadata"] = metadata
        if in_reply_to:
            extra["in_reply_to"] = in_reply_to
        if origin:
            extra["origin"] = origin
        return extra

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
            # Entries carry their own origin (command replies: the concrete
            # channel that issued the command). A missing wildcard origin only
            # blocks entries that did not capture one.
            head_extra = self._pending_outbound_extras[0]
            if not self._origin and not head_extra.get("origin"):
                return
            if force:
                self._reset_pending_flush_backoff()
            elif self._pending_next_flush_at > self._clock():
                logger.debug(
                    "orchestrator IM pending outbound flush deferred for %.1fs",
                    self._pending_next_flush_at - self._clock(),
                )
                return
            # Queued entries keep their own origin: command replies carry the
            # concrete channel they must return to; event reports flush to the
            # wildcard origin, which the gateway resolves to the authorized
            # recipient at OUTBOUND time.
            while self._pending_outbound:
                text = self._pending_outbound[0]
                extra = self._pending_outbound_extras[0]
                origin = extra.get("origin") or self._origin
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
                self._pending_outbound_extras.popleft()

    async def dispatch(self, message: InboundMessage, semantic: MessageSemantics) -> str:
        """Execute only an authenticated, explicitly allowed IM command."""
        if (
            not is_concrete_im_origin(message.origin)
            or (message.metadata or {}).get("authenticated_origin") != message.origin
        ):
            return "origin_unauthenticated"
        allowed, _ = check_orchestrator_command(message.text)
        if semantic is not MessageSemantics.COMMAND or not allowed:
            return "command_rejected"
        route = self._commands.route(message)
        if route is None or route.kind != "orchestrator_cli":
            return "command_unroutable"
        # The CLI requires explicit --id too; enforce it here for direct
        # lifecycle handlers and bounded /issue tail replies as well.
        argv = list(route.argv)
        if argv[0] == "issue" and argv[1] != "list" and not self._arg_value(argv, "--id"):
            self._queue_command_reply(
                route.payload, 2, "", "error: --id is required",
                origin=message.origin, in_reply_to=message.message_id,
            )
            return "orchestrator_cli_invalid"
        return await self._dispatch_orchestrator_cli(
            route, reply_origin=message.origin, in_reply_to=message.message_id
        )

    async def _dispatch_orchestrator_cli(
        self,
        route,
        *,
        reply_origin: str | None = None,
        in_reply_to: str | None = None,
    ) -> str:
        argv = list(route.argv)
        if len(argv) < 2:
            self._queue_command_reply(
                route.payload,
                2,
                "",
                "error: invalid orchestrator command",
                origin=reply_origin,
                in_reply_to=in_reply_to,
            )
            return "orchestrator_cli_invalid"

        noun, verb = argv[0], argv[1]
        if noun == "issue" and verb in {"stop", "pause", "resume"}:
            issue_id = route.issue_hint or self._arg_value(argv, "--id")
            if not issue_id:
                self._queue_command_reply(
                    route.payload,
                    2,
                    "",
                    "error: --id is required",
                    origin=reply_origin,
                    in_reply_to=in_reply_to,
                )
                return "orchestrator_cli_invalid"
            self._h.control_verb(verb, issue_id)
            self._queue_command_reply(
                route.payload,
                0,
                f"Control command '{verb}' sent for issue {issue_id}",
                "",
                origin=reply_origin,
                in_reply_to=in_reply_to,
            )
            return f"orchestrator_cli_issue_{verb}"

        if noun == "issue" and verb == "tail":
            self._queue_command_reply(
                route.payload,
                0,
                self._tail_notice(argv),
                "",
                origin=reply_origin,
                in_reply_to=in_reply_to,
            )
            return "orchestrator_cli_issue_tail"

        rc, stdout, stderr = await self._run_cli_isolated(argv)
        self._queue_command_reply(
            route.payload,
            rc,
            stdout,
            stderr,
            origin=reply_origin,
            in_reply_to=in_reply_to,
        )
        return f"orchestrator_cli_{noun}_{verb}" if rc == 0 else "orchestrator_cli_failed"

    async def _run_cli_isolated(self, argv: list[str]) -> tuple[int, str, str]:
        """Run one CLI command serially with a real execution timeout.

        Production commands execute in a child Python process. Timeout or
        cancellation terminates that process, so the daemon read loop stays
        responsive and no command can retain process-global argv/stdio.
        Injected synchronous runners use a worker thread solely as a test seam;
        their unkillable task remains serialized until it actually finishes.
        """
        async with self._cli_lock:
            if self._cli_runner is not None:
                return await self._run_injected_cli(argv)
            return await self._run_cli_subprocess(argv)

    async def _run_injected_cli(self, argv: list[str]) -> tuple[int, str, str]:
        prior = self._cli_run_task
        if prior is not None and not prior.done():
            with contextlib.suppress(Exception):
                await prior
        task: asyncio.Task[tuple[int, str, str]] = asyncio.create_task(
            asyncio.to_thread(self._run_orchestrator_cli, list(argv))
        )
        self._cli_run_task = task
        task.add_done_callback(self._on_cli_run_done)
        try:
            return await asyncio.wait_for(
                asyncio.shield(task), timeout=self._cli_timeout_seconds
            )
        except TimeoutError:
            return self._cli_timeout_result(argv)

    async def _run_cli_subprocess(self, argv: list[str]) -> tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _CLI_CHILD_CODE,
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return 1, "", f"error: unable to start orchestrator command: {exc}"
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._cli_timeout_seconds
            )
        except TimeoutError:
            await self._terminate_cli_process(process)
            return self._cli_timeout_result(argv)
        except asyncio.CancelledError:
            await self._terminate_cli_process(process)
            raise
        return (
            int(process.returncode or 0),
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    async def _terminate_cli_process(self, process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    def _cli_timeout_result(self, argv: list[str]) -> tuple[int, str, str]:
        timeout = f"{self._cli_timeout_seconds:g}"
        logger.warning(
            "orchestrator IM command timed out after %ss: %s",
            timeout,
            " ".join(argv)[:64],
        )
        return 124, "", f"error: command timed out after {timeout}s"

    def _on_cli_run_done(self, task: asyncio.Task[tuple[int, str, str]]) -> None:
        """Clear the tracked CLI task and absorb its (unused) outcome."""
        if self._cli_run_task is task:
            self._cli_run_task = None
        if not task.cancelled() and task.exception() is not None:
            logger.debug(
                "orchestrator IM command worker failed after timeout",
                exc_info=task.exception(),
            )

    def _run_orchestrator_cli(self, argv: list[str]) -> tuple[int, str, str]:
        if self._cli_runner is None:
            raise RuntimeError("the in-process CLI runner is disabled")
        return self._cli_runner(list(argv))

    def _queue_command_reply(
        self,
        command_text: str,
        rc: int,
        stdout: str,
        stderr: str,
        *,
        origin: str | None = None,
        in_reply_to: str | None = None,
    ) -> None:
        text = self._format_command_reply(command_text, rc, stdout, stderr)
        # Route the reply to the channel that issued the command (the
        # DELIVER's concrete origin) and thread it back to the triggering
        # IM message (in_reply_to). Never the wildcard: with WeChat and
        # Feishu both connected, the wildcard resolves wechat-first and a
        # Feishu command's answer would land on WeChat.
        self._queue_pending_outbound(
            text,
            in_reply_to=in_reply_to or self._current_delivery_id or None,
            origin=origin,
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
