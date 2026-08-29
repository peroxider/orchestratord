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
real handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
import shlex
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from orchestratord.ipc.models import InboundMessage, MessageSemantics

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local CommandRouter / ControlBridge / MessageClassifier
# ---------------------------------------------------------------------------


@dataclass
class CommandRoute:
    """Parsed command route from a user message."""

    kind: str = ""  # "orchestrator_cli" | "agent_intent" | "control_verb"
    verb: str = ""
    issue_hint: str | None = None
    payload: str = ""
    argv: tuple[str, ...] = ()


# Regex for loose issue-id matching (e.g. AGENTSDK-15, PROJ-128).
_ISSUE_ID_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")

# Recognized orchestrator issue subcommands.
_ORCHESTRATOR_ISSUE_COMMANDS = frozenset(
    {
        "list", "show", "tail", "stop", "pause", "resume",
        "clarify", "inject", "feedback", "review", "retry",
        "workspace", "rebase",
    }
)

# Agent-intent verbs routed through the agent_intent handler.
_AGENT_CMD_RE = re.compile(
    r"^\s*/agent\s+(retry|follow-up|unblock)\b[^\n]*", re.IGNORECASE,
)

# Control verbs that map directly to control_socket operations.
_CONTROL_VERB_RE = re.compile(
    r"^\s*/(?P<verb>pause|resume|stop|takeover|inject|detach|clarify|review|feedback)\b[^\n]*",
    re.IGNORECASE,
)

# Control verbs routed to the control socket.
_CONTROL_SOCKET_VERBS = frozenset({"pause", "resume", "stop", "detach", "takeover"})

# Verbs that bridge to issue inject (NOT control socket no-op).
_INJECT_VERBS = frozenset({"inject"})


class CommandRouter:
    """Parse inbound text into a :class:`CommandRoute`.

    Provides a self-contained
    implementation covering the same dispatch surface.
    """

    def route(self, message: InboundMessage) -> CommandRoute | None:
        text = (message.text or "").strip()

        # Orchestrator CLI commands (/issue <sub>, /server status).
        argv = self._orchestrator_argv(text)
        if argv is not None:
            return CommandRoute(
                kind="orchestrator_cli",
                verb=argv[0],
                issue_hint=self._extract_issue(text, argv),
                payload=text,
                argv=tuple(argv),
            )

        # /agent retry|follow-up|unblock
        m = _AGENT_CMD_RE.match(text)
        if m:
            verb = m.group(1).lower()
            issue_hint = self._extract_issue(text)
            return CommandRoute(
                kind="agent_intent", verb=verb, issue_hint=issue_hint, payload=text,
            )

        # Control verbs: /pause, /resume, /stop, /takeover, /inject, etc.
        m = _CONTROL_VERB_RE.match(text)
        if m:
            verb = m.group("verb").lower()
            issue_hint = self._extract_issue(text)
            return CommandRoute(
                kind="control_verb", verb=verb, issue_hint=issue_hint, payload=text,
            )

        return None

    @staticmethod
    def _orchestrator_argv(text: str) -> list[str] | None:
        tokens = _split_command_tokens(text)
        if not tokens:
            return None
        command = tokens[0].lower()
        if command == "issue" and len(tokens) >= 2:
            issue_subcommand = tokens[1].lower()
            if issue_subcommand in _ORCHESTRATOR_ISSUE_COMMANDS:
                return ["issue", issue_subcommand, *tokens[2:]]
        if command == "server" and len(tokens) >= 2 and tokens[1].lower() == "status":
            return ["server", "status", *tokens[2:]]
        return None

    @staticmethod
    def _extract_issue(
        text: str, argv: list[str] | None = None,
    ) -> str | None:
        if argv:
            for idx, token in enumerate(argv):
                if token == "--id" and idx + 1 < len(argv):
                    return argv[idx + 1]
                if token.startswith("--id="):
                    return token.split("=", 1)[1]
        m = _ISSUE_ID_RE.search(text)
        return m.group(1) if m else None


@dataclass
class ControlTarget:
    """Resolved control target from semantic + route."""

    surface: str = ""  # "bridge_interrupt" | "control_socket" | "issue_inject" | "operator_hints" | "issue_cli"
    verb: str = ""
    issue_hint: str | None = None
    payload: str = ""


class ControlBridge:
    """Map ``MessageSemantics`` + ``CommandRoute`` to a :class:`ControlTarget`.

    Provides a self-contained
    implementation.
    """

    def resolve(
        self,
        semantic: MessageSemantics,
        route: CommandRoute | None,
    ) -> ControlTarget | None:
        if semantic is MessageSemantics.INTERRUPT:
            return ControlTarget(
                surface="bridge_interrupt",
                verb="interrupt",
                payload=route.payload if route else "",
            )

        if route is None:
            return None

        verb = route.verb

        if verb in _INJECT_VERBS:
            return ControlTarget(
                surface="issue_inject",
                verb=verb,
                payload=route.payload,
                issue_hint=route.issue_hint,
            )

        if verb in _CONTROL_SOCKET_VERBS:
            return ControlTarget(
                surface="control_socket",
                verb=verb,
                payload=route.payload,
                issue_hint=route.issue_hint,
            )

        # clarify/review/feedback/retry → issue CLI surface
        return ControlTarget(
            surface="issue_cli",
            verb=verb,
            payload=route.payload,
            issue_hint=route.issue_hint,
        )

    def context_only_target(
        self, route: CommandRoute | None,
    ) -> ControlTarget:
        """contextOnly routes to operator hints (no run trigger)."""
        return ControlTarget(
            surface="operator_hints",
            verb="contextOnly",
            payload=route.payload if route else "",
            issue_hint=route.issue_hint if route else None,
        )


# DeliverAs metadata → MessageSemantics mapping.
_DELIVER_AS_MAP = {
    "newPrompt": "NEW_PROMPT",
    "command": "COMMAND",
    "followUp": "FOLLOW_UP",
    "approval": "APPROVAL",
    "interrupt": "INTERRUPT",
    "contextOnly": "CONTEXT_ONLY",
}

# Singleton CommandRouter for MessageClassifier reuse.
_COMMAND_ROUTER: CommandRouter | None = None


class MessageClassifier:
    """Classify an :class:`InboundMessage` into :class:`MessageSemantics`.

    Provides a self-contained
    implementation covering the same classification rules:
    1. Structured ``deliverAs`` metadata takes precedence.
    2. Explicit slash commands recognized by ``CommandRouter`` → COMMAND.
    3. Approval only via structured metadata or bound wait-point.
    4. Plain text while session busy → FOLLOW_UP.
    5. Otherwise → NEW_PROMPT.
    """

    def classify(
        self,
        message: InboundMessage,
        *,
        is_busy: bool = False,
        has_pending_wait: bool = False,
    ) -> MessageSemantics:
        # 1. Structured deliverAs wins (explicit, no NL guessing).
        deliver_as = self._deliver_as(message)
        if deliver_as is not None:
            return deliver_as

        # 2. Explicit slash commands.
        if _get_command_router().route(message) is not None:
            return MessageSemantics.COMMAND

        # 3. Approval only via structured metadata or bound wait-point.
        if (
            has_pending_wait
            and message.semantic_tags
            and "approval" in message.semantic_tags
        ):
            return MessageSemantics.APPROVAL

        # 4. Busy ordinary text → queue-as-followUp.
        if is_busy:
            return MessageSemantics.FOLLOW_UP

        # 5. Default.
        return MessageSemantics.NEW_PROMPT

    def _deliver_as(
        self, message: InboundMessage,
    ) -> MessageSemantics | None:
        raw = None
        if message.raw and isinstance(message.raw, dict):
            raw = message.raw.get("deliverAs")
        if raw is None:
            for tag in message.semantic_tags or []:
                if tag in _DELIVER_AS_MAP:
                    return MessageSemantics[_DELIVER_AS_MAP[tag]]
        if isinstance(raw, str) and raw in _DELIVER_AS_MAP:
            return MessageSemantics[_DELIVER_AS_MAP[raw]]
        return None


def _get_command_router() -> CommandRouter:
    global _COMMAND_ROUTER
    if _COMMAND_ROUTER is None:
        _COMMAND_ROUTER = CommandRouter()
    return _COMMAND_ROUTER


def _split_command_tokens(text: str) -> list[str]:
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return []
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    if not tokens:
        return []
    tokens[0] = tokens[0].lstrip("/")
    return tokens


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
    ) -> None:
        self._h = handlers
        self._commands = command_router or CommandRouter()
        self._control = control_bridge or ControlBridge()
        self._ipc = ipc_client
        self._origin = origin
        self._cli_runner = cli_runner
        self._pending_outbound: deque[str] = deque()
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
        try:
            status = self.dispatch(message, semantic)
            await self._flush_pending_outbound(force=True)
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
        except Exception:  # noqa: BLE001
            await self._complete_processing(
                message.message_id,
                "failure",
                "orchestrator dispatch failed",
            )
            logger.exception("orchestrator IM dispatch failed")

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
            except Exception:  # noqa: BLE001
                logger.warning(
                    "orchestrator processing completion failed: %s",
                    message_id[:16],
                    exc_info=True,
                )

    def _classify(self, message):
        return MessageClassifier().classify(message)

    async def send_outbound(self, text: str) -> None:
        """Send a reply / event back to the IM origin via the OUTBOUND frame.

        The origin is the opt-in origin (``im:direct:*:*`` by default for
        orchestrator); the gateway resolves the wildcard to a concrete
        sender at OUTBOUND time. The event is queued only when the send
        cannot start right now (the IPC socket is not open yet) or when the
        gateway explicitly NACKs the send. If the IPC ACK times out, delivery
        is ambiguous: the gateway may already have sent the IM message but
        returned its ACK too late. In that case we do not auto-retry, because
        duplicate chat messages are worse than a best-effort dropped event.
        """
        if self._ipc is None or not self._origin:
            return
        if text in self._pending_outbound:
            logger.debug("orchestrator IM outbound deduped before send: %r", text[:60])
            await self._flush_pending_outbound()
            return
        if self._pending_outbound:
            self._queue_pending_outbound(text)
            await self._flush_pending_outbound()
            return
        sent = await self._send_to_origin(self._origin, text)
        if not sent:
            self._queue_pending_outbound(text)

    async def _send_to_origin(self, origin: str, text: str) -> bool:
        if self._ipc is None:
            return False
        try:
            response = await self._ipc.send_outbound(origin=origin, text=text)
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

    def _queue_pending_outbound(self, text: str) -> None:
        # Skip exact duplicates already waiting in the queue — e.g. the
        # orchestrator emits "orchestratord: IM notifications
        # enabled" on every reconnect, and if the gateway can't resolve
        # the wildcard origin (operator hasn't messaged recently), each
        # copy would queue and all would flush at once when the operator
        # finally sends a message.
        if text in self._pending_outbound:
            logger.debug("orchestrator IM outbound deduped: %r already queued", text[:60])
            return
        if len(self._pending_outbound) >= self._pending_outbound_limit:
            self._pending_outbound.popleft()
            logger.warning("orchestrator IM outbound pending queue full; dropped oldest event")
        self._pending_outbound.append(text)
        logger.info("orchestrator IM outbound queued (pending connection or send retry)")

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
                try:
                    sent = await self._send_to_origin(origin, text)
                    if not sent:
                        return
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "orchestrator IM pending outbound flush failed origin=%s",
                        origin[:32],
                        exc_info=True,
                    )
                    return
                self._pending_outbound.popleft()

    def dispatch(self, message: InboundMessage, semantic: MessageSemantics) -> str:
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
                return self._dispatch_orchestrator_cli(route)
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

    def _dispatch_orchestrator_cli(self, route) -> str:
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

        rc, stdout, stderr = self._run_orchestrator_cli(argv)
        self._queue_command_reply(route.payload, rc, stdout, stderr)
        return f"orchestrator_cli_{noun}_{verb}"

    def _run_orchestrator_cli(self, argv: list[str]) -> tuple[int, str, str]:
        if self._cli_runner is not None:
            return self._cli_runner(list(argv))

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                from orchestratord.cli.main import app
                import sys as _sys

                _sys.argv = ["orchestratord"] + list(argv)
                app()
                rc = 0
            except SystemExit as exc:
                code = exc.code
                rc = code if isinstance(code, int) else 1
            except Exception as exc:  # noqa: BLE001
                print(f"error: {exc}", file=sys.stderr)
                rc = 1
        return rc, stdout.getvalue(), stderr.getvalue()

    def _queue_command_reply(self, command_text: str, rc: int, stdout: str, stderr: str) -> None:
        text = self._format_command_reply(command_text, rc, stdout, stderr)
        self._queue_pending_outbound(text)

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


__all__ = ["OrchestratorGatewayClient", "OrchestratorHandlers"]
