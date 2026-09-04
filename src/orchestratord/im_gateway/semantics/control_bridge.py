"""ControlBridge — map ``interrupt`` / control verbs to existing surfaces (P5).

REPL/remote/direct-connect → bridge subtype ``interrupt``.
Orchestrator → control verbs ``pause/resume/inject/stop/detach/takeover``.
``inject`` / ``contextOnly`` must NOT rely on the control socket no-op;
they bridge to ``issue inject`` / ``.operator_hints.md``.

The bridge is a pure mapper: it resolves a :class:`CommandRoute` (or an
explicit ``interrupt`` semantic) to a target surface + payload. The
actual dispatch (calling the control socket, issue CLI, or bridge) is
done by the runtime router / opt-in clients, which own those handles.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestratord.ipc.models import MessageSemantics

from .command_router import CommandRoute

# Verbs that go to the orchestrator control socket.
_CONTROL_SOCKET_VERBS = frozenset({"pause", "resume", "stop", "detach", "takeover"})
# Verbs that must bridge to issue inject / operator hints (control socket
# inject is a no-op, so do not route inject there).
_INJECT_VERBS = frozenset({"inject"})


@dataclass
class ControlTarget:
    surface: str  # "bridge_interrupt" | "control_socket" | "issue_inject" | "operator_hints"
    verb: str
    payload: str = ""
    issue_hint: str | None = None


class ControlBridge:
    def resolve(
        self, semantic: MessageSemantics, route: CommandRoute | None
    ) -> ControlTarget | None:
        # Explicit interrupt semantic (structured) → bridge interrupt.
        if semantic is MessageSemantics.INTERRUPT:
            return ControlTarget(
                surface="bridge_interrupt", verb="interrupt", payload=route.payload if route else ""
            )
        if route is None:
            return None
        verb = route.verb
        if verb in _INJECT_VERBS:
            # inject must bridge issue inject / operator hints, NOT control socket no-op.
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
        # clarify/review/feedback → issue CLI surface (operator action verbs)
        return ControlTarget(
            surface="issue_cli", verb=verb, payload=route.payload, issue_hint=route.issue_hint
        )

    def context_only_target(self, route: CommandRoute | None) -> ControlTarget:
        """contextOnly routes to operator hints (no run trigger)."""
        return ControlTarget(
            surface="operator_hints",
            verb="contextOnly",
            payload=route.payload if route else "",
            issue_hint=route.issue_hint if route else None,
        )


__all__ = ["ControlBridge", "ControlTarget"]
